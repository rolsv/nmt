# bot.py - обновлённая версия с исправлениями и новой кнопкой статистики
"""
Telegram-бот управления фармом на нескольких аккаунтах.

Контексты браузера временные: под каждое действие (баланс/промокод/
расстановка фигурок/проверка логина) открывается свежий BrowserContext
из сохранённой сессии аккаунта (context_manager.account_context) и сразу
закрывается после действия - сессия при этом пересохраняется на диск.
В памяти между действиями не висит ничего, кроме путей к файлам сессий
(см. state.AccountState).

Кнопки:
  - Автофарм (старт/стоп) - бесконечный цикл. За раундами следит ТОЛЬКО
    аккаунт-наблюдатель (state.watcher()), остальные аккаунты "спят" и
    включаются лишь когда стартует новый раунд - чтобы расставить свои
    фигурки. Аккаунты обрабатываются ПО ОЧЕРЕДИ с паузой FARM_DELAY_RANGE
    между ними (не одновременно).
  - Баланс - разово показывает баланс NMT со всех аккаунтов одним
    сообщением (аккаунты опрашиваются параллельно, т.к. это просто чтение
    страницы, а не действие).
  - Промокод - просит ввести код и активирует его на ВСЕХ аккаунтах ПО
    ОЧЕРЕДИ с паузой PROMO_DELAY_RANGE между ними, результат по каждому -
    в одном сообщении.
  - Статистика - показывает накопленную статистику фарма.

У каждого аккаунта свой page_lock (см. state.AccountState) - защищает
работу именно с этим аккаунтом (открытие контекста, действие, сохранение
сессии), поэтому операции над разными аккаунтами не блокируют друг друга.
"""
import asyncio
import random

from promo_listener import test_promo_text
from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    ContextTypes,
    filters,
)

from config import TELEGRAM_BOT_TOKEN, BALANCE_CONCURRENCY, FAIL_THRESHOLD, COOLDOWN_MINUTES, PROMO_EXPIRE_FAIL_STREAK
from browser_actions import login, logged_in, get_balance
from power_blocks import get_board_state, fill_board, activate_promo, farm_stats, open_all_boxes_for_account, list_dead_figures_for_account, join_all_giveaways_for_account
from context_manager import account_context

# Паузы между аккаунтами (сек), чтобы не дёргать все аккаунты в одну
# секунду - выглядит менее подозрительно для сайта.
PROMO_DELAY_RANGE = (5, 10)
FARM_DELAY_RANGE = (3, 5)

# Ограничение на число одновременных проверок баланса - полностью
# параллельная проверка всех аккаунтов разом даёт резкий скачок нагрузки
# и провоцирует таймауты.
_balance_semaphore = asyncio.Semaphore(BALANCE_CONCURRENCY)

# Отдельный лок НА КАЖДЫЙ ФИЗИЧЕСКИЙ ПРОКСИ (по адресу сервера) - даже если
# BALANCE_CONCURRENCY позволяет 2+ одновременных проверки, два аккаунта,
# сидящих за ОДНИМ и тем же прокси-IP, не должны долбить его вдвоём разом -
# это удваивает нагрузку именно там, где и так уже тесно.
_proxy_locks = {}


def _get_proxy_lock(acc):
    if not acc.proxy:
        return None
    key = acc.proxy.get("server")
    if key not in _proxy_locks:
        _proxy_locks[key] = asyncio.Lock()
    return _proxy_locks[key]


def build_menu(state):
    if state.farming:
        farm_btn = InlineKeyboardButton("⏹ Стоп автофарма", callback_data="farm_stop")
    else:
        farm_btn = InlineKeyboardButton("▶️ Автофарм", callback_data="farm_start")

    if state.promo_listening:
        listen_btn = InlineKeyboardButton("⏹ Остановить промо-слушалку", callback_data="promo_listen_stop")
    else:
        listen_btn = InlineKeyboardButton("🔎 Включить промо-слушалку", callback_data="promo_listen_start")

    keyboard = [
        [farm_btn],
        [InlineKeyboardButton("💰 Баланс (все аккаунты)", callback_data="balance")],
        [InlineKeyboardButton("🎁 Промокод (все аккаунты)", callback_data="promo_start")],
        [InlineKeyboardButton("📦 Открыть все кейсы", callback_data="open_boxes")],
        [InlineKeyboardButton("🏷️ Выставить мёртвые на маркет", callback_data="list_dead")],
        [InlineKeyboardButton("🎲 Зайти в Giveaways", callback_data="join_giveaways")],
        [listen_btn],
        [InlineKeyboardButton("📊 Статистика", callback_data="stats")],
    ]
    return InlineKeyboardMarkup(keyboard)

async def handle_giveaway_code(state, message, code: str):
    """Обрабатывает введённый код розыгрыша."""
    chat_id = message.chat_id
    code = code.strip().upper()
    
    await message.reply_text(f"🎲 Ищу розыгрыш с кодом `{code}`...")
    
    # Перемешиваем аккаунты для рандомизации
    accounts_to_process = list(state.accounts)
    random.shuffle(accounts_to_process)
    
    results = []
    total_joined = 0
    total_found = 0
    
    for i, acc in enumerate(accounts_to_process):
        email_info = f" ({acc.email})" if acc.email else ""
        
        try:
            async with acc.page_lock:
                async with account_context(state.browser, acc) as page:
                    name, result, error = await join_giveaway_with_code(
                        page, acc.name, acc.email or "", code
                    )
            
            if error is not None:
                results.append(f"  ⚠️ {name}{email_info}: ошибка - {error}")
                continue
            
            # Считаем успешные
            joined = sum(1 for r in result if r.get("success") and r.get("reason") == "joined")
            already = sum(1 for r in result if r.get("reason") == "already_joined")
            total_joined += joined
            total_found += len(result)
            
            # Формируем отчёт для аккаунта
            if not result:
                results.append(f"  ℹ️ {name}{email_info}: розыгрыш с кодом {code} не найден")
            else:
                status = []
                if joined > 0:
                    status.append(f"✅ {joined} новых")
                if already > 0:
                    status.append(f"⏭️ {already} уже участвовали")
                results.append(f"  📊 {name}{email_info}: {', '.join(status)} ({len(result)} всего)")
                
        except Exception as e:
            results.append(f"  ⚠️ {acc.name}{email_info}: критическая ошибка - {e}")
        
        # Пауза между аккаунтами
        if i < len(accounts_to_process) - 1:
            delay = random.uniform(2.0, 5.0)
            await asyncio.sleep(delay)
    
    # Итоговый отчёт
    final_lines = ["🎲 Результаты участия в розыгрыше:"]
    final_lines.extend(results)
    
    if total_found > 0:
        final_lines.append(f"\n📊 Итого: присоединилось {total_joined} аккаунтов")
    else:
        final_lines.append("\n📊 Розыгрыш с таким кодом не найден на сайте")
    
    await message.reply_text("\n".join(final_lines))

async def wait_or_stop(stop_event, seconds):
    """Ждёт seconds секунд, но выходит раньше, если пришла команда стоп."""
    try:
        await asyncio.wait_for(stop_event.wait(), timeout=seconds)
    except asyncio.TimeoutError:
        pass


async def relogin_if_needed(state, acc, bot):
    """Проверяет сессию одного аккаунта (через временный контекст) и
    перелогинивает при необходимости. Для аккаунтов без email/password
    (например, вход только через Google) автологин не делаем - просто
    предупреждаем в Telegram, что нужно вручную обновить сессию через
    manual_login.py."""
    async with acc.page_lock:
        async with account_context(state.browser, acc) as page:
            if await logged_in(page):
                return

            if not (acc.email and acc.password):
                await bot.send_message(
                    state.admin_chat_id,
                    f"⚠️ [{acc.name}] Сессия протухла, а это Google-аккаунт - "
                    f"автологин невозможен. Останови автофарм и выполни на сервере:\n"
                    f"    python manual_login.py {acc.name}",
                )
                return

            await bot.send_message(state.admin_chat_id, f"⚠️ [{acc.name}] Сессия протухла, перелогиниваюсь...")
            try:
                await login(page, acc.email, acc.password, acc.session_file)
            except Exception as e:
                await bot.send_message(state.admin_chat_id, f"⚠️ [{acc.name}] Не удалось перелогиниться: {e}")

async def list_dead_for_account(state, acc):
    """Выставляет мёртвые фигуры одного аккаунта на маркет."""
    if acc.in_cooldown():
        return acc.name, [], "в отдыхе после серии ошибок (cooldown)"

    try:
        async with acc.page_lock:
            async with account_context(state.browser, acc) as page:
                name, results, error = await list_dead_figures_for_account(
                    page, acc.name, acc.email or ""
                )
        acc.mark_success()
        return name, results, error
    except Exception as e:
        acc.mark_failure(FAIL_THRESHOLD, COOLDOWN_MINUTES)
        return acc.name, [], str(e)

async def join_giveaways_for_account(state, acc):
    """Заходит во все доступные розыгрыши для одного аккаунта."""
    if acc.in_cooldown():
        return acc.name, [], "в отдыхе после серии ошибок (cooldown)"

    try:
        async with acc.page_lock:
            async with account_context(state.browser, acc) as page:
                name, results, error = await join_all_giveaways_for_account(
                    page, acc.name, acc.email or ""
                )
        acc.mark_success()
        return name, results, error
    except Exception as e:
        acc.mark_failure(FAIL_THRESHOLD, COOLDOWN_MINUTES)
        return acc.name, [], str(e)

async def get_balance_for_account(state, acc):
    if acc.in_cooldown():
        return acc.name, None, "в отдыхе после серии ошибок (cooldown)"

    async def _do_check():
        async with acc.page_lock:
            async with account_context(state.browser, acc) as page:
                return await get_balance(page)

    async with _balance_semaphore:
        try:
            proxy_lock = _get_proxy_lock(acc)
            if proxy_lock:
                async with proxy_lock:
                    balance = await _do_check()
            else:
                balance = await _do_check()
            acc.mark_success()
            return acc.name, balance, None
        except Exception as e:
            acc.mark_failure(FAIL_THRESHOLD, COOLDOWN_MINUTES)
            return acc.name, None, str(e)


async def activate_promo_for_account(state, acc, code):
    if acc.in_cooldown():
        return acc.name, None, False, "в отдыхе после серии ошибок (cooldown)"

    try:
        async with acc.page_lock:
            async with account_context(state.browser, acc) as page:
                result_text, ok = await activate_promo(page, code)
        acc.mark_success()
        return acc.name, result_text, ok, None
    except Exception as e:
        acc.mark_failure(FAIL_THRESHOLD, COOLDOWN_MINUTES)
        return acc.name, None, False, str(e)

async def open_boxes_for_account(state, acc):
    """Открывает все кейсы на одном аккаунте. Возвращает результат."""
    if acc.in_cooldown():
        return acc.name, 0, [], "в отдыхе после серии ошибок (cooldown)"
    
    try:
        async with acc.page_lock:
            async with account_context(state.browser, acc) as page:
                name, opened, results, error = await open_all_boxes_for_account(
                    page, acc.name, acc.email or ""
                )
        acc.mark_success()
        return name, opened, results, error
    except Exception as e:
        acc.mark_failure(FAIL_THRESHOLD, COOLDOWN_MINUTES)
        return acc.name, 0, [], str(e)

async def place_for_account(state, acc):
    """Расставляет фигурки одного аккаунта в текущем раунде. Ошибки не
    поднимаются наружу - возвращаются как часть результата, чтобы одна
    сломавшаяся сессия не обрывала работу с остальными аккаунтами.
    Аккаунт, у которого недавно было подряд несколько ошибок, "отдыхает"
    (cooldown) и пропускается, чтобы не долбить его каждый раунд впустую.
    Возвращает: (имя, число_фигурок, число_клеток, report, ошибка_или_None)"""
    if acc.in_cooldown():
        return acc.name, 0, 0, None, "в отдыхе после серии ошибок (cooldown)"

    try:
        async with acc.page_lock:
            async with account_context(state.browser, acc) as page:
                placed, cells, report, error = await fill_board(page, acc.name, acc.email)
        acc.mark_success()
        return acc.name, placed, cells, report, None
    except Exception as e:
        acc.mark_failure(FAIL_THRESHOLD, COOLDOWN_MINUTES)
        return acc.name, 0, 0, None, str(e)


async def get_round_state(state):
    """Опрашивает текущий раунд через аккаунт-наблюдателя (временный
    контекст, сразу закрывается после запроса)."""
    watcher = state.watcher()
    async with watcher.page_lock:
        async with account_context(state.browser, watcher) as page:
            return await get_board_state(page)


# bot.py - ОБНОВЛЁННЫЙ farm_loop с сохранением всей логики

async def farm_loop(state, bot):
    """
    Бесконечный цикл автофарма. За сменой раунда следит ТОЛЬКО
    аккаунт-наблюдатель. Когда раунд меняется - по очереди (с паузой)
    расставляем фигурки на всех аккаунтах и шлём один общий отчёт.
    """
    watcher = state.watcher()
    last_round_id = None
    iters_since_login_check = 0

    await bot.send_message(
        state.admin_chat_id,
        f"🟢 Автофарм запущен. Наблюдатель: {watcher.name}. Аккаунтов всего: {len(state.accounts)}.",
    )

    while not state.stop_event.is_set():

        # Периодически проверяем, что сессии не протухли - у ВСЕХ аккаунтов
        if iters_since_login_check >= 5:
            iters_since_login_check = 0
            accounts_to_check = list(state.accounts)
            random.shuffle(accounts_to_check)
            
            for acc in accounts_to_check:
                if state.stop_event.is_set():
                    break
                await relogin_if_needed(state, acc, bot)
                await asyncio.sleep(random.uniform(1.0, 3.0))

        try:
            round_id, _board_size, _occupied, _my_on_board, ends_in = await get_round_state(state)
        except Exception as e:
            await bot.send_message(state.admin_chat_id, f"⚠️ [{watcher.name}] Ошибка получения состояния раунда: {e}")
            await wait_or_stop(state.stop_event, 10)
            iters_since_login_check += 1
            continue

        if round_id != last_round_id:
            initial_delay = random.uniform(1.0, 4.0)
            await wait_or_stop(state.stop_event, initial_delay)
            
            accounts_to_place = list(state.accounts)
            random.shuffle(accounts_to_place)
            
            report_lines = [f"✅ Раунд {round_id}:"]
            round_stats = {}
            account_errors = []  # Для сбора ошибок с email

            for i, acc in enumerate(accounts_to_place):
                if state.stop_event.is_set():
                    report_lines.append("  (остановлено раньше времени)")
                    break

                name, placed, cells, report, error = await place_for_account(state, acc)
                
                email_info = f" ({acc.email})" if acc.email else ""
                
                round_stats[name] = {
                    "figures_placed": placed,
                    "cells_occupied": cells,
                    "error": error if error else None
                }
                
                if error is not None:
                    report_lines.append(f"  ⚠️ {name}{email_info}: ошибка - {error}")
                    account_errors.append(f"{name}{email_info}: {error}")
                elif placed == 0:
                    # Если 0 фигурок поставлено - показываем причину
                    if report and report.get("no_figures"):
                        report_lines.append(f"  ⚡ {name}{email_info}: 0 фигурок - нет свободных в инвентаре")
                    elif report and report.get("all_slots_filled"):
                        report_lines.append(f"  ⚡ {name}{email_info}: 0 фигурок - все слоты уже заняты")
                    else:
                        # Проверяем причины пропуска
                        skipped_reasons = []
                        if report:
                            if report.get("skipped_power"):
                                skipped_reasons.append(f"недостаточно энергии ({len(report['skipped_power'])} фигурок)")
                            if report.get("skipped_space"):
                                skipped_reasons.append(f"нет места ({len(report['skipped_space'])} фигурок)")
                        
                        if skipped_reasons:
                            report_lines.append(f"  ⚡ {name}{email_info}: 0 фигурок - {', '.join(skipped_reasons)}")
                        else:
                            report_lines.append(f"  ⚡ {name}{email_info}: 0 фигурок поставлено")
                else:
                    report_lines.append(f"  ✅ {name}{email_info}: поставлено {placed} фигурок, занято {cells} клеток")

                is_last = i == len(accounts_to_place) - 1
                if not is_last:
                    delay = random.uniform(*FARM_DELAY_RANGE) * random.uniform(0.9, 1.1)
                    await wait_or_stop(state.stop_event, delay)

            farm_stats.add_round_stats(round_id, round_stats)
            
            total_figures = sum(s["figures_placed"] for s in round_stats.values())
            total_cells = sum(s["cells_occupied"] for s in round_stats.values())
            report_lines.append(f"\n📊 Итого в раунде: {total_figures} фигурок, {total_cells} клеток")
            
            # Если были ошибки - добавляем отдельный блок
            if account_errors:
                report_lines.append("\n⚠️ Ошибки:")
                for err in account_errors[:5]:
                    report_lines.append(f"  • {err}")
                if len(account_errors) > 5:
                    report_lines.append(f"  ... и ещё {len(account_errors) - 5} ошибок")

            await bot.send_message(state.admin_chat_id, "\n".join(report_lines))

            last_round_id = round_id

        wait_seconds = 120 if ends_in > 60 else 20
        await wait_or_stop(state.stop_event, wait_seconds)
        iters_since_login_check += 1

    await bot.send_message(state.admin_chat_id, "🔴 Автофарм остановлен.")
    state.farming = False


# ---------------------- Хендлеры ----------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.application.bot_data["state"]
    accounts_list = ", ".join(acc.name for acc in state.accounts)
    await update.message.reply_text(
        f"Меню управления (аккаунты: {accounts_list}):\n\n"
        f"📝 Для теста промокода отправь:\n"
        f"`/test_promo текст поста`\n"
        f"(проверит, найдёт ли GPT промокод в тексте)",
        reply_markup=build_menu(state),
        parse_mode="Markdown",
    )


async def on_button(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.application.bot_data["state"]
    query = update.callback_query
    await query.answer()

    if query.data == "farm_start":
        if state.farming:
            await query.edit_message_text("Автофарм уже запущен.", reply_markup=build_menu(state))
            return

        state.farming = True
        state.stop_event = asyncio.Event()
        state.farm_task = asyncio.create_task(farm_loop(state, context.bot))
        await query.edit_message_text("Запускаю автофарм...", reply_markup=build_menu(state))

    elif query.data == "farm_stop":
        if not state.farming:
            await query.edit_message_text("Автофарм не запущен.", reply_markup=build_menu(state))
            return

        state.stop_event.set()
        await query.edit_message_text("Останавливаю автофарм...", reply_markup=build_menu(state))

    elif query.data == "balance":
        results = await asyncio.gather(*(get_balance_for_account(state, acc) for acc in state.accounts))

        lines = ["💰 Баланс:"]
        for name, balance, error in results:
            if error is not None:
                lines.append(f"  ⚠️ {name}: ошибка - {error}")
            else:
                lines.append(f"  {name}: {balance} NMT")

        await query.message.reply_text("\n".join(lines))

    elif query.data == "promo_start":
        state.awaiting_promo[query.message.chat_id] = True
        await query.message.reply_text("Введите промокод - применю его на всех аккаунтах:")

    elif query.data == "promo_listen_start":
        state.promo_listening = True
        await query.edit_message_text(
            "🔎 Промо-слушалка включена - буду проверять новые посты в каналах "
            "и сам активирую найденные промокоды на всех аккаунтах.",
            reply_markup=build_menu(state),
        )

    elif query.data == "promo_listen_stop":
        state.promo_listening = False
        await query.edit_message_text("Промо-слушалка выключена.", reply_markup=build_menu(state))

    elif query.data == "stats":
        # Показываем статистику
        summary = farm_stats.get_summary()
        lines = [
            "📊 Статистика фарма (с момента запуска бота):",
            f"  • Обработано раундов: {summary['rounds']}",
            f"  • Всего поставлено фигурок: {summary['figures']}",
            f"  • Всего занято клеток: {summary['cells']}",
            "",
        ]
        
        # Если есть ошибки по аккаунтам - показываем
        if farm_stats.errors_by_account:
            lines.append("⚠️ Ошибки по аккаунтам:")
            for acc_name, count in sorted(farm_stats.errors_by_account.items()):
                lines.append(f"  • {acc_name}: {count} ошибок")
        
        # Если есть данные по последним раундам - показываем
        if farm_stats.figures_placed_by_round:
            last_rounds = sorted(farm_stats.figures_placed_by_round.keys())[-5:]
            lines.append("")
            lines.append("📈 Последние 5 раундов:")
            for rnd in last_rounds:
                figs = farm_stats.figures_placed_by_round.get(rnd, 0)
                cells = farm_stats.cells_placed_by_round.get(rnd, 0)
                lines.append(f"  • Раунд {rnd}: {figs} фигурок, {cells} клеток")
        
        await query.message.reply_text("\n".join(lines))

    elif query.data == "open_boxes":
            await query.edit_message_text("📦 Открываю все кейсы на всех аккаунтах...")
            
            results = []
            total_boxes_opened = 0
            total_figures_received = 0
            accounts_with_errors = []
            
            # Перемешиваем аккаунты для рандомизации
            accounts_to_process = list(state.accounts)
            random.shuffle(accounts_to_process)
            
            for i, acc in enumerate(accounts_to_process):
                name, opened, box_results, error = await open_boxes_for_account(state, acc)
                
                email_info = f" ({acc.email})" if acc.email else ""
                
                if error is not None:
                    results.append(f"  ⚠️ {name}{email_info}: ошибка - {error}")
                    accounts_with_errors.append(name)
                elif opened == 0:
                    results.append(f"  📭 {name}{email_info}: кейсов не найдено")
                else:
                    total_boxes_opened += opened
                    # Считаем полученные фигуры
                    for br in box_results:
                        if br.get("success"):
                            total_figures_received += br.get("figures_count", 0)
                    
                    # Краткий отчёт: только итог по аккаунту
                    success_count = sum(1 for br in box_results if br.get("success"))
                    fail_count = opened - success_count
                    results.append(f"  ✅ {name}{email_info}: открыто {opened} кейсов ({success_count} успешно, {fail_count} ошибок)")
                
                # Пауза между аккаунтами
                if i < len(accounts_to_process) - 1:
                    delay = random.uniform(2.0, 5.0)
                    await asyncio.sleep(delay)
            
            # Итоговое сообщение
            final_lines = ["📦 Результаты открытия кейсов:"]
            final_lines.extend(results)
            
            # Добавляем сводку
            if total_boxes_opened > 0:
                final_lines.append(f"\n📊 Итого: открыто {total_boxes_opened} кейсов, получено {total_figures_received} фигурок")
                if accounts_with_errors:
                    final_lines.append(f"⚠️ Аккаунты с ошибками: {', '.join(accounts_with_errors)}")
            else:
                final_lines.append("\n📊 Кейсов не найдено на всех аккаунтах")
            
            await query.message.reply_text("\n".join(final_lines))
            await query.delete_message()  # Удаляем сообщение "Открываю все кейсы..."

    elif query.data == "list_dead":  # <-- НОВЫЙ ОБРАБОТЧИК
        await query.edit_message_text("🏷️ Проверяю инвентарь аккаунтов на мёртвые фигуры...")
        
        results = []
        total_listed = 0
        total_figures_found = 0
        rarity_names = {0: "серых", 1: "синих", 2: "фиолетовых", 3: "золотых"}
        
        # Перемешиваем аккаунты для рандомизации
        accounts_to_process = list(state.accounts)
        random.shuffle(accounts_to_process)
        
        for i, acc in enumerate(accounts_to_process):
            name, dead_results, error = await list_dead_for_account(state, acc)
            
            email_info = f" ({acc.email})" if acc.email else ""
            
            if error is not None:
                results.append(f"  ⚠️ {name}{email_info}: ошибка - {error}")
                continue
            
            if not dead_results:
                results.append(f"  ✅ {name}{email_info}: мёртвых фигур не найдено")
                continue
            
            # Считаем успешные выставления
            success_count = sum(1 for r in dead_results if r.get("success"))
            total_listed += success_count
            total_figures_found += len(dead_results)
            
            # Формируем детальный отчёт для аккаунта
            lines = [f"  📊 {name}{email_info}: найдено {len(dead_results)}, выставлено {success_count}:"]
            
            # Группируем по редкости для красоты
            by_rarity = {}
            for r in dead_results:
                rarity = r.get("rarity", 0)
                if rarity not in by_rarity:
                    by_rarity[rarity] = {"found": 0, "listed": 0}
                by_rarity[rarity]["found"] += 1
                if r.get("success"):
                    by_rarity[rarity]["listed"] += 1
            
            for rarity, counts in sorted(by_rarity.items()):
                r_name = rarity_names.get(rarity, "неизвестных")
                lines.append(f"      • {r_name}: {counts['listed']}/{counts['found']} выставлено")
            
            # Если успешных мало - показываем детали ошибок
            if success_count < len(dead_results):
                errors = [r for r in dead_results if not r.get("success")]
                lines.append("      ⚠️ Ошибки:")
                for err in errors[:3]:
                    lines.append(f"          - {err.get('name')}: {err.get('error', 'неизвестная ошибка')[:80]}")
                if len(errors) > 3:
                    lines.append(f"          ... и ещё {len(errors) - 3} ошибок")
            
            results.append("\n".join(lines))
            
            # Пауза между аккаунтами
            if i < len(accounts_to_process) - 1:
                delay = random.uniform(2.0, 5.0)
                await asyncio.sleep(delay)
        
        # Итоговое сообщение
        final_lines = ["🏷️ Результаты выставления мёртвых фигур:"]
        final_lines.extend(results)
        
        if total_figures_found > 0:
            success_rate = int((total_listed / total_figures_found) * 100)
            final_lines.append(f"\n📊 Итого: {total_listed}/{total_figures_found} фигур выставлено ({success_rate}% успеха)")
        else:
            final_lines.append("\n📊 Мёртвых фигур не найдено на всех аккаунтах")
        
        await query.message.reply_text("\n".join(final_lines))
        await query.delete_message()  # Удаляем сообщение "Проверяю инвентарь..."

    elif query.data == "join_giveaways":
        # Устанавливаем флаг, что ждём ввод кода
        state.awaiting_giveaway_code[query.message.chat_id] = True
        await query.message.reply_text(
            "🎲 Введите код розыгрыша (entry code):",
            reply_markup=InlineKeyboardMarkup([
                [InlineKeyboardButton("❌ Отмена", callback_data="cancel_giveaway")]
            ])
        )

    # В функции on_text, перед проверкой промокода:

    # Проверяем, ждём ли ввод кода розыгрыша
    if state.awaiting_giveaway_code.get(chat_id):
        state.awaiting_giveaway_code[chat_id] = False
        await handle_giveaway_code(state, update.message, text)
        return


async def on_text(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.application.bot_data["state"]
    chat_id = update.effective_chat.id
    
    # Проверяем, есть ли сообщение
    if not update.message:
        return
        
    text = (update.message.text or "").strip()

    # Если ждём от этого чата промокод - активируем его на всех аккаунтах
    if state.awaiting_promo.get(chat_id):
        state.awaiting_promo[chat_id] = False

        if state.promo_activation_lock.locked():
            await update.message.reply_text(
                "⏳ Сейчас уже идёт активация другого промокода (например, "
                "автослушалка что-то нашла) - этот встанет в очередь и "
                "выполнится сразу после."
            )

        async with state.promo_activation_lock:
            lines = [f"🎁 Промокод '{text}':"]

            # Перемешиваем аккаунты для рандомизации
            accounts_to_activate = list(state.accounts)
            random.shuffle(accounts_to_activate)

            consecutive_fails = 0

            for i, acc in enumerate(accounts_to_activate):
                name, result_text, ok, error = await activate_promo_for_account(state, acc, text)
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
                        f"- дальше не пробую (осталось {len(accounts_to_activate) - i - 1} акк.)."
                    )
                    break

                is_last = i == len(accounts_to_activate) - 1
                if not is_last:
                    # Случайная пауза с джиттером
                    delay = random.uniform(*PROMO_DELAY_RANGE) * random.uniform(0.9, 1.1)
                    await asyncio.sleep(delay)

            await update.message.reply_text("\n".join(lines))
        return

    # Пока автофарм работает - можно остановить его, просто отправив
    # любую цифру (как ты и просил), без обязательного нажатия кнопки.
    if state.farming and text.isdigit():
        state.stop_event.set()
        await update.message.reply_text("Останавливаю автофарм...")
        return

    await update.message.reply_text("Не понял команду, используй /start для меню.")

async def cmd_test_promo(update: Update, context: ContextTypes.DEFAULT_TYPE):
    state = context.application.bot_data["state"]
    chat_id = update.effective_chat.id
    
    # Берём текст после команды
    text = " ".join(context.args)
    if not text:
        await update.message.reply_text(
            "📝 Использование: `/test_promo текст поста`\n\n"
            "Пример: `/test_promo Сегодня новый промокод для NMT: WELCOME2024`",
            parse_mode="Markdown"
        )
        return
    
    await update.message.reply_text("🔍 Анализирую текст...")
    await test_promo_text(state, context.bot, chat_id, text)

async def start_bot(state):
    """Создаёт Application, регистрирует хендлеры и запускает polling."""
    application = Application.builder().token(TELEGRAM_BOT_TOKEN).build()
    application.bot_data["state"] = state

    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("test_promo", cmd_test_promo))
    application.add_handler(CallbackQueryHandler(on_button))
    application.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, on_text))

    await application.initialize()
    await application.start()
    await application.updater.start_polling()

    return application