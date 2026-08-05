"""
Промо-слушалка: мониторинг Telegram-каналов И чата на промокоды NMT.gg.

Два независимых источника:
  - КАНАЛЫ (PROMO_CHANNELS) - каждый новый пост целиком прогоняется через
    GPT, который решает "по теме ли пост про NMT.gg" и "есть ли промокод".
  - ЧАТ (PROMO_CHAT) - тут GPT НЕ дёргается на каждое сообщение (дорого и
    шумно). Сначала дешёвый фильтр по формату без ИИ (looks_like_promo_
    code_format - похоже на код: буквы/буквы+цифры без пробелов), и
    только для подходящих кандидатов спрашиваем GPT, действительно ли это
    промокод, дав ему для контекста последние 10 сообщений чата.

Найденный код активируется на ВСЕХ аккаунтах ПОСЛЕДОВАТЕЛЬНО (та же
функция и та же пауза, что и у ручного ввода промокода из меню бота, см.
bot.activate_promo_for_account). Если код подряд не срабатывает на
PROMO_EXPIRE_FAIL_STREAK аккаунтах - считаем его истёкшим и не тратим
время на оставшиеся.

Дедупликация - через promo_cache (персистентный, переживает перезапуск
бота, каждая запись "протухает" через PROMO_CODE_EXPIRE_HOURS часов).
"""
import asyncio
import json
import random
import re
import time
import requests
from typing import Optional, Tuple
from urllib.parse import urlparse

from telethon import TelegramClient, events

import promo_cache
from config import (
    TG_API_ID, TG_API_HASH, TG_SESSION_NAME, PROMO_CHANNELS, PROMO_CHAT,
    GPT_API_KEY, GPT_API_URL, GPT_MODEL, PROMO_EXPIRE_FAIL_STREAK,
)

# --- Промпт для постов из КАНАЛОВ ---
SYSTEM_PROMPT = (
    "Ты — фильтр сообщений Telegram-каналов про криптоигру NMT.gg.\n\n"
    "Твоя задача: определить, относится ли пост к игре NMT.gg, и найти промокод.\n\n"
    "Правила:\n"
    "1. Если пост НЕ про NMT.gg (не упоминает NMT, кейсы, фигурки, промокоды для NMT) — верни {\"on_topic\": false, \"promo_code\": null}\n"
    "2. Если пост про NMT.gg, но промокода нет — верни {\"on_topic\": true, \"promo_code\": null}\n"
    "3. Если есть промокод — верни {\"on_topic\": true, \"promo_code\": \"КОД\"}\n\n"
    "Важно: промокод — это строка из заглавных букв и цифр, обычно 5-15 символов.\n"
    "Примеры: WELCOME100, MMXLAS236, NMT2024, COINFLIPVIP\n\n"
    "ОТВЕТЬ ТОЛЬКО JSON, БЕЗ ПОЯСНЕНИЙ И ДОПОЛНИТЕЛЬНОГО ТЕКСТА!\n"
    'Формат ответа: {"on_topic": true/false, "promo_code": "код или null"}'
)

# --- Промпт для кандидатов из ЧАТА (уже прошедших дешёвый regex-фильтр) ---
CHAT_CHECK_PROMPT_TEMPLATE = (
    "Ты помогаешь определить, является ли конкретное сообщение из Telegram-чата "
    "промокодом для игры NMT.gg (nmt.gg).\n\n"
    "Кандидат (само сообщение): \"{candidate}\"\n\n"
    "Последние сообщения этого чата для контекста (от старых к новым, кандидат "
    "может быть среди них последним):\n{context}\n\n"
    "ОТВЕТЬ ТОЛЬКО JSON, БЕЗ ПОЯСНЕНИЙ:\n"
    '{{"is_promo_code": true/false}}\n\n'
    "true - если кандидат похож на промокод (строка из букв/цифр, 5-15 символов).\n"
    "false - если это просто обычное слово/ник/сообщение не похожее на промокод.\n\n"
    "ВАЖНО: Не требуй упоминания NMT или nmt.gg в контексте - просто определи, "
    "является ли сам кандидат промокодом по своему формату."
)

# Более точный regex для промокодов NMT (используется как fallback, если GPT не ответил)
PROMO_REGEX = re.compile(r'\b[A-Z]{3,}[0-9]{2,}\b|\b[0-9]{2,}[A-Z]{3,}\b|\b[A-Z0-9]{5,15}\b')
# Исключаем слова, которые часто ложно срабатывают
PROMO_BLACKLIST = {'NMT', 'NMTGG', 'TELEGRAM', 'CHANNEL', 'COINFLIP', 'VIP', 'FREE', 'BONUS', 'HTTP', 'HTTPS', 'WWW'}

# Дешёвый фильтр "похоже ли сообщение из ЧАТА на промокод" (без ИИ) -
# промокоды обычно выглядят как одно "слово" из латинских букв и/или
# букв+цифр, без пробелов, разумной длины.
CANDIDATE_REGEX = re.compile(r'^[A-Za-z0-9]{4,20}$')


def looks_like_promo_code_format(text: str) -> bool:
    """Дешёвая проверка БЕЗ обращения к GPT: подходит ли сообщение целиком
    под формат промокода (буквы/буквы+цифры без пробелов, разумная длина,
    хотя бы одна буква - чтобы не ловить голые числа/суммы)."""
    candidate = text.strip()
    if not CANDIDATE_REGEX.match(candidate):
        return False
    if not re.search(r'[A-Za-z]', candidate):
        return False
    return True


# ============================ Запросы к GPT ============================

def _extract_raw_text(data: dict) -> Tuple[str, float]:
    """Достаёт (текст_ответа, стоимость) из ответа API - формат может
    отличаться в зависимости от того, какой именно вариант структуры
    вернул конкретный запрос."""
    raw_text = ""
    cost = 0.0

    if "response" in data:
        if isinstance(data["response"], dict):
            raw_text = data["response"].get("text", "") or data["response"].get("content", "")
            cost = data.get("cost", 0.0)
        elif isinstance(data["response"], str):
            raw_text = data["response"]
            cost = data.get("cost", 0.0)

    if not raw_text and "data" in data:
        if isinstance(data["data"], dict):
            raw_text = data["data"].get("response", "") or data["data"].get("text", "")
            if data["data"].get("response"):
                cost = data["data"].get("cost", 0.0)

    if not raw_text:
        raw_text = data.get("text", "") or data.get("content", "")
        cost = data.get("cost", 0.0)

    return raw_text, cost


def _poll_for_result(data: dict, headers: dict, cost: float) -> Tuple[str, float]:
    """API иногда асинхронный (возвращает request_id) - опрашиваем статус,
    пока не будет готов результат."""
    request_id = data["request_id"]
    initial_wait = data.get("wait", 1)
    print(f"[promo-listener] API вернул request_id={request_id}, жду результат...")

    parsed_api_url = urlparse(GPT_API_URL)
    base_url = f"{parsed_api_url.scheme}://{parsed_api_url.netloc}"
    status_url = f"{base_url}/api/v2/status/{request_id}"

    time.sleep(max(initial_wait, 1))

    max_attempts = 60
    poll_interval = 2

    for attempt in range(1, max_attempts + 1):
        try:
            status_resp = requests.get(status_url, headers=headers, timeout=30)
            status_resp.raise_for_status()
            result_data = status_resp.json()
        except Exception as e:
            print(f"[promo-listener] Попытка {attempt}/{max_attempts}: ошибка опроса статуса: {e}")
            time.sleep(poll_interval)
            continue

        if result_data.get("error"):
            print(f"[promo-listener] API вернул ошибку: {result_data['error']}")
            break

        status = result_data.get("status")
        if status != "success":
            time.sleep(poll_interval)
            continue

        raw_text, new_cost = _extract_raw_text(result_data)
        return raw_text, (new_cost or cost)

    return "", cost


def _request_gpt_raw_sync(full_prompt: str) -> Tuple[str, float]:
    """Отправляет full_prompt в GPT API (синхронно - только через
    asyncio.to_thread снаружи) и возвращает (сырой_текст_ответа, стоимость).
    Понимает и мгновенный, и асинхронный (request_id + опрос) варианты API."""
    payload = {
        "model": GPT_MODEL,
        "prompt": full_prompt,
        "chat_history": [],
        "file_base64": "",
        "internet_access": False,
        "mime_type": "",
    }
    headers = {"Content-Type": "application/json", "api-key": GPT_API_KEY}

    try:
        resp = requests.post(GPT_API_URL, json=payload, headers=headers, timeout=60)
        resp.raise_for_status()
        data = resp.json()
        print(f"[promo-listener] API ответ: {json.dumps(data, ensure_ascii=False)[:300]}")
    except Exception as e:
        print(f"[promo-listener] Ошибка запроса к API: {e}")
        return "", 0.0

    raw_text, cost = _extract_raw_text(data)

    if not raw_text and "request_id" in data:
        raw_text, cost = _poll_for_result(data, headers, cost)

    return raw_text, cost


def extract_json_from_response(text: str) -> Optional[dict]:
    """Пытается извлечь JSON из ответа модели."""
    cleaned = text.strip()
    cleaned = re.sub(r'^```(?:json)?\s*', '', cleaned)
    cleaned = re.sub(r'\s*```$', '', cleaned)

    json_match = re.search(r'\{[^{}]*\}', cleaned, re.DOTALL)
    if json_match:
        cleaned = json_match.group(0)

    try:
        return json.loads(cleaned)
    except Exception:
        pass

    try:
        if "'" in cleaned:
            cleaned_fixed = cleaned.replace("'", '"')
            return json.loads(cleaned_fixed)
    except Exception:
        pass

    return None


def call_gpt_sync(post_text: str) -> Tuple[dict, float]:
    """Для КАНАЛОВ: спрашивает GPT про целый пост (on_topic + promo_code)."""
    full_prompt = f"{SYSTEM_PROMPT}\n\nТекст поста:\n{post_text}"
    raw_text, cost = _request_gpt_raw_sync(full_prompt)

    if not raw_text:
        print(f"[promo-listener] Пустой ответ от API")
        return {"on_topic": True, "promo_code": None, "_empty": True}, cost

    parsed = extract_json_from_response(raw_text)
    if parsed is None:
        print(f"[promo-listener] Не удалось распарсить JSON: {raw_text[:200]!r}")
        return {"on_topic": True, "promo_code": None, "_parse_error": True}, cost

    return parsed, cost


def call_gpt_chat_check_sync(candidate: str, context: str) -> Tuple[dict, float]:
    """Для ЧАТА: спрашивает GPT, является ли конкретный кандидат промокодом,
    учитывая контекст последних сообщений."""
    full_prompt = CHAT_CHECK_PROMPT_TEMPLATE.format(candidate=candidate, context=context)
    raw_text, cost = _request_gpt_raw_sync(full_prompt)

    if not raw_text:
        print(f"[promo-listener][chat] Пустой ответ от API")
        return {"is_promo_code": False, "_empty": True}, cost

    parsed = extract_json_from_response(raw_text)
    if parsed is None:
        print(f"[promo-listener][chat] Не удалось распарсить JSON: {raw_text[:200]!r}")
        return {"is_promo_code": False, "_parse_error": True}, cost

    return parsed, cost


def fallback_regex(text: str) -> Tuple[bool, Optional[str]]:
    """Fallback: ищем промокод через regex (когда GPT недоступен/сломан)."""
    if "nmt" not in text.lower():
        return False, None

    matches = PROMO_REGEX.findall(text.upper())

    valid_codes = []
    for code in matches:
        if code in PROMO_BLACKLIST:
            continue
        if len(code) < 5:
            continue
        if code.isalpha() and len(code) < 6:
            continue
        valid_codes.append(code)

    if valid_codes:
        return True, valid_codes[0]

    return True, None


class PromoListener:
    def __init__(self, state, bot):
        self.state = state
        self.bot = bot
        self.client = None
        # Быстрый in-memory кэш на этот процесс (доп. к персистентному
        # promo_cache - чтобы не дёргать диск на каждую проверку подряд).
        self.seen_codes = set()

    async def start(self):
        """Запускает Telethon клиент и регистрирует обработчики каналов и
        (если настроен) чата."""
        self.client = TelegramClient(
            TG_SESSION_NAME,
            TG_API_ID,
            TG_API_HASH,
            connection_retries=None,
            retry_delay=5,
            auto_reconnect=True,
        )

        @self.client.on(events.NewMessage(chats=PROMO_CHANNELS))
        async def channel_handler(event):
            await self.handle_new_post(event)

        if PROMO_CHAT:
            @self.client.on(events.NewMessage(chats=PROMO_CHAT))
            async def chat_handler(event):
                await self.handle_chat_message(event)

        await self.client.start()

        chat_note = f", чат: {PROMO_CHAT}" if PROMO_CHAT else " (чат не настроен - PROMO_CHAT is None)"
        print(f"[promo-listener] Подключен. Каналы: {', '.join(PROMO_CHANNELS)}{chat_note}")
        return self.client

    # ------------------------ Дедупликация кодов ------------------------

    async def is_code_seen(self, code_key: str) -> bool:
        if code_key in self.seen_codes:
            return True
        return await promo_cache.is_seen(code_key)

    async def mark_code_seen(self, code_key: str):
        self.seen_codes.add(code_key)
        await promo_cache.mark_seen(code_key)

    # --------------------------- Канал: посты ----------------------------

    async def analyze_with_gpt(self, text: str) -> Tuple[bool, Optional[str]]:
        """Анализирует текст поста через GPT (для каналов)."""
        try:
            result, cost = await asyncio.to_thread(call_gpt_sync, text)

            if result.get("_error") or result.get("_empty") or result.get("_parse_error"):
                print(f"[promo-listener] GPT не сработал, пробую regex")
                return fallback_regex(text)

            on_topic = result.get("on_topic", False)
            promo_code = result.get("promo_code")

            if promo_code:
                promo_code = promo_code.strip().upper()
                if promo_code not in text.upper():
                    alt = fallback_regex(text)
                    if alt[0] and alt[1]:
                        promo_code = alt[1]
                    else:
                        print(f"[promo-listener] Код {promo_code} не найден в тексте")
                        promo_code = None

            print(f"[promo-listener] Результат: on_topic={on_topic}, promo={promo_code}, cost={cost:.8f}")
            return on_topic, promo_code

        except Exception as e:
            print(f"[promo-listener] Ошибка анализа: {e}")
            return fallback_regex(text)

    async def handle_new_post(self, event):
        """Обработчик нового поста в канале."""
        if not self.state.promo_listening:
            return

        channel_name = getattr(event.chat, "username", None) or getattr(event.chat, "title", "unknown")
        post_text = event.message.message or ""

        if not post_text.strip():
            return

        print(f"[promo-listener][{channel_name}] Новый пост: {post_text[:100]}...")

        on_topic, promo_code = await self.analyze_with_gpt(post_text)

        if not on_topic:
            print(f"[promo-listener][{channel_name}] Пост не по теме NMT.gg")
            return

        if not promo_code:
            print(f"[promo-listener][{channel_name}] Пост по теме, промокод не найден")
            return

        code_key = promo_code.strip().upper()
        if await self.is_code_seen(code_key):
            print(f"[promo-listener][{channel_name}] Код {code_key} уже обработан - пропускаю.")
            return

        await self.mark_code_seen(code_key)
        print(f"[promo-listener][{channel_name}] Найден промокод: {code_key}")

        await self.activate_code_everywhere(code_key, channel_name)

    async def handle_chat_message(self, event):
        """Обработчик нового сообщения в мониторимом ЧАТЕ. GPT дёргается
        ТОЛЬКО если сообщение прошло дешёвый regex-фильтр по формату -
        основная масса обычных сообщений отсеивается бесплатно."""
        if not self.state.promo_listening:
            return

        text = (event.message.message or "").strip()
        if not text:
            return

        # Проверяем формат - похоже ли на промокод
        if not looks_like_promo_code_format(text):
            return  # обычное сообщение, явно не похоже на код - тихо пропускаем

        chat_name = getattr(event.chat, "username", None) or getattr(event.chat, "title", "chat")
        print(f"[promo-listener][chat:{chat_name}] Похоже на код: {text!r} - уточняю у GPT с контекстом...")

        code_key = text.strip().upper()
        if await self.is_code_seen(code_key):
            print(f"[promo-listener][chat:{chat_name}] Код {code_key} уже обработан - пропускаю.")
            return

        # Последние 5 сообщений чата для контекста (вместо 10)
        try:
            history = await event.client.get_messages(event.chat_id, limit=5)
        except Exception as e:
            print(f"[promo-listener][chat:{chat_name}] Не удалось получить историю чата: {e}")
            history = []

        context_lines = []
        for m in reversed(list(history)):  # от старых к новым
            if m and getattr(m, "message", None):
                context_lines.append(m.message.strip().replace("\n", " "))
        context_text = "\n".join(context_lines[-5:]) if context_lines else "(нет истории)"

        try:
            result, cost = await asyncio.to_thread(call_gpt_chat_check_sync, text, context_text)
        except Exception as e:
            print(f"[promo-listener][chat:{chat_name}] Ошибка запроса к GPT: {e}")
            # Даже если GPT ошибся - пробуем активировать код, если он похож на промокод
            if looks_like_promo_code_format(text):
                print(f"[promo-listener][chat:{chat_name}] GPT ошибся, но код похож на промокод - пробую активировать...")
                await self.mark_code_seen(code_key)
                await self.activate_code_everywhere(code_key, f"chat:{chat_name} (GPT-fallback)")
            return

        is_promo = bool(result.get("is_promo_code"))
        print(f"[promo-listener][chat:{chat_name}] GPT: is_promo_code={is_promo}, cost={cost:.8f}")

        # ДАЖЕ ЕСЛИ GPT сказал False - если код очень похож на промокод (буквы+цифры, 5-15 символов)
        # всё равно пробуем активировать - не доверяем GPT на 100%
        if not is_promo:
            # Дополнительная проверка: если код содержит и буквы, и цифры - скорее всего промокод
            import re
            if re.search(r'[A-Z]', text) and re.search(r'[0-9]', text) and len(text) >= 5:
                print(f"[promo-listener][chat:{chat_name}] GPT сказал False, но код содержит буквы+цифры - пробую активировать...")
                await self.mark_code_seen(code_key)
                await self.activate_code_everywhere(code_key, f"chat:{chat_name} (forced)")
            return

        await self.mark_code_seen(code_key)
        print(f"[promo-listener][chat:{chat_name}] Промокод подтверждён: {code_key}")

        await self.activate_code_everywhere(code_key, f"chat:{chat_name}")

    # ------------------------------ Активация ------------------------------

    async def activate_code_everywhere(self, code: str, source: str):
        """Активирует промокод на всех аккаунтах ПОСЛЕДОВАТЕЛЬНО. Если код
        подряд не срабатывает на PROMO_EXPIRE_FAIL_STREAK аккаунтах -
        считаем его истёкшим/невалидным и не тратим время на остальные.

        Использует тот же state.promo_activation_lock, что и ручной ввод
        промокода из меню (bot.py) - если что-то уже активируется, эта
        активация просто дождётся своей очереди, вместо того чтобы
        побежать параллельно по тем же 25 аккаунтам."""
        from bot import activate_promo_for_account, PROMO_DELAY_RANGE

        if self.state.promo_activation_lock.locked():
            await self.bot.send_message(
                self.state.admin_chat_id,
                f"⏳ [{source}] Нашёл код `{code}`, но сейчас уже идёт другая "
                f"активация - подожду очереди.",
                parse_mode="Markdown",
            )

        async with self.state.promo_activation_lock:
            self.state.pending_promocodes = True

            try:
                await self.bot.send_message(
                    self.state.admin_chat_id,
                    f"🔎 [{source}] Найден промокод: `{code}`\nАктивирую на всех аккаунтах...",
                    parse_mode="Markdown",
                )

                accounts = list(self.state.accounts)
                random.shuffle(accounts)

                lines = [f"🎁 Автопромокод `{code}` (источник: {source}):"]
                consecutive_fails = 0
                stopped_early = False

                for i, acc in enumerate(accounts):
                    name, result_text, ok, error = await activate_promo_for_account(self.state, acc, code)

                    if error is not None:
                        lines.append(f"  ⚠️ {name}: ошибка - {error}")
                        consecutive_fails += 1
                    else:
                        prefix = "✅" if ok else "❌"
                        lines.append(f"  {prefix} {name}: {result_text}")
                        consecutive_fails = 0 if ok else consecutive_fails + 1

                    if consecutive_fails >= PROMO_EXPIRE_FAIL_STREAK:
                        lines.append(
                            f"  ⏹ Похоже, код истёк/невалиден ({consecutive_fails} неудач подряд) "
                            f"- дальше не пробую (осталось {len(accounts) - i - 1} акк.)."
                        )
                        stopped_early = True
                        break

                    is_last = i == len(accounts) - 1
                    if not is_last:
                        delay = random.uniform(*PROMO_DELAY_RANGE) * random.uniform(0.9, 1.1)
                        await asyncio.sleep(delay)

                await self.bot.send_message(self.state.admin_chat_id, "\n".join(lines))

            finally:
                self.state.pending_promocodes = False


# ---- Глобальные функции ----

_promo_listener: Optional[PromoListener] = None


async def start_promo_listener(state, bot):
    """Запускает промо-слушалку."""
    global _promo_listener

    listener = PromoListener(state, bot)
    _promo_listener = listener
    return await listener.start()


async def test_promo_text(state, bot, chat_id, text: str):
    """Тестовая функция для /test_promo."""
    listener = PromoListener(state, bot)
    on_topic, promo_code = await listener.analyze_with_gpt(text)

    lines = [
        "📋 Результат анализа текста:",
        f"  • По теме NMT.gg: {'✅' if on_topic else '❌'}",
    ]

    if promo_code:
        lines.append(f"  • Промокод: `{promo_code}`")
        if promo_code.upper() in text.upper():
            lines.append("  • Код подтверждён в тексте: ✅")
        else:
            lines.append("  • Код НЕ НАЙДЕН в тексте ⚠️")
    else:
        lines.append("  • Промокод: ❌ не найден")

    await bot.send_message(chat_id, "\n".join(lines), parse_mode="Markdown")