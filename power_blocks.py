# power_blocks.py - добавим статистику и рандомизацию
"""
Модуль для автоматической расстановки фигурок на поле power-blocks
и активации промокодов.

Логика расстановки:
1. Получаем текущее состояние поля (/api/power-blocks/current) -> занятые клетки.
2. Получаем свои свободные фигурки (/api/figures) -> фильтруем по state == "IDLE".
3. Смотрим сколько своих фигурок уже стоит на поле (owner == "me" в placements),
   лимит - 5 штук за раунд.
4. Для каждой свободной фигурки (начиная с самых мощных) размер блока
   (ширина x высота) определяется по её СИЛЕ (power) через таблицу
   POWER_SIZE_TABLE, ищем свободное место под этот размер (пробуя обе
   ориентации), желательно впритык к уже занятым клеткам.
5. Отправляем POST /api/power-blocks/place.
"""
import uuid
import random
import re
import asyncio

from browser_actions import dismiss_popups, ensure_on_site

MAX_ON_BOARD = 5

# Таблица размеров по СИЛЕ фигурки (power), а НЕ по редкости - так теперь
# реально работает игра. Диапазоны и размеры - как на сайте:
# (power_min включительно, power_max НЕ включительно, ширина, высота).
# "Power cost" из таблицы сайта не хранится отдельно - он всегда равен
# width*height (площади), это и есть цена постановки.
POWER_SIZE_TABLE = [
    (0, 448, 1, 1),
    (448, 672, 1, 2),
    (672, 896, 1, 3),
    (896, 1344, 2, 2),
    (1344, 1792, 2, 3),
    (1792, 2016, 2, 4),
    (2016, 2688, 3, 3),
    (2688, 3584, 3, 4),
    (3584, 4480, 4, 4),
    (4480, 5600, 4, 5),
    (5600, 6720, 5, 5),
    (6720, 8064, 5, 6),
    (8064, 9408, 6, 6),
    (9408, 10976, 6, 7),
    (10976, float("inf"), 7, 7),
]


def get_figure_footprint(power: int):
    """Возвращает (width, height) - размер, который займёт на поле
    фигурка с такой силой (power). Чем больше силы - тем крупнее блок."""
    for lo, hi, w, h in POWER_SIZE_TABLE:
        if lo <= power < hi:
            return w, h
    return 7, 7  # на всякий случай, если power окажется огромным


class FarmStats:
    """Статистика фарма за всё время работы бота."""
    def __init__(self):
        self.total_rounds = 0
        self.total_figures_placed = 0
        self.total_cells_occupied = 0
        self.total_cells_placed_by_us = 0
        self.figures_placed_by_round = {}
        self.cells_placed_by_round = {}
        self.errors_by_account = {}

    def add_round_stats(self, round_id, account_stats):
        """Добавить статистику по раунду."""
        self.total_rounds += 1
        figures_in_round = 0
        cells_in_round = 0
        
        for acc_name, stats in account_stats.items():
            if stats.get("figures_placed", 0) > 0:
                figures_in_round += stats["figures_placed"]
                cells_in_round += stats.get("cells_occupied", 0)
            
            # Запоминаем ошибки
            if stats.get("error"):
                if acc_name not in self.errors_by_account:
                    self.errors_by_account[acc_name] = 0
                self.errors_by_account[acc_name] += 1
        
        self.total_figures_placed += figures_in_round
        self.total_cells_placed_by_us += cells_in_round
        self.figures_placed_by_round[round_id] = figures_in_round
        self.cells_placed_by_round[round_id] = cells_in_round

    def get_summary(self):
        """Получить краткую сводку статистики."""
        return {
            "rounds": self.total_rounds,
            "figures": self.total_figures_placed,
            "cells": self.total_cells_placed_by_us,
        }


# Глобальный объект статистики
farm_stats = FarmStats()


async def api_get(page, url):
    await ensure_on_site(page)
    
    result = await page.evaluate(
        """
        async (url) => {
            try {
                const authKey = Object.keys(localStorage)
                    .find(k => k.endsWith("-auth-token"));
                
                if (!authKey) {
                    throw new Error("No auth token found in localStorage");
                }
                
                const token = JSON.parse(localStorage.getItem(authKey));
                
                if (!token || !token.access_token) {
                    throw new Error("Invalid auth token");
                }
                
                const resp = await fetch(url, {
                    credentials: "include",
                    headers: {
                        Authorization: `Bearer ${token.access_token}`
                    }
                });
                
                const text = await resp.text();
                let data;
                try {
                    data = JSON.parse(text);
                } catch {
                    data = text;
                }
                
                return {
                    status: resp.status,
                    data
                };
            } catch (e) {
                return {
                    status: 401,
                    data: { error: e.message }
                };
            }
        }
        """,
        url,
    )
    
    return result


async def api_post(page, url, payload):
    await ensure_on_site(page)

    idempotency_key = str(uuid.uuid4())

    return await page.evaluate(
        """
        async ([url, payload, idempotencyKey]) => {

            const token = JSON.parse(
                localStorage.getItem("sb-pcvxsbskvyaematzgyfe-auth-token")
            );

            const resp = await fetch(url, {
                method: "POST",
                credentials: "include",
                headers: {
                    "Content-Type": "application/json",
                    "Authorization": `Bearer ${token.access_token}`,
                    "Idempotency-Key": idempotencyKey
                },
                body: JSON.stringify(payload)
            });

            const text = await resp.text();

            let data;

            try {
                data = JSON.parse(text);
            } catch {
                data = text;
            }

            return {
                status: resp.status,
                data
            };
        }
        """,
        [url, payload, idempotency_key],
    )


async def get_board_state(page):
    """
    Получить занятость поля и сколько своих фигурок уже стоит.

    Возвращает: (round_id, board_size, occupied, my_on_board_count, endsInSeconds)
    """
    result = await api_get(page, "https://nmt.gg/api/power-blocks/current")

    if result["status"] != 200:
        raise RuntimeError(f"Ошибка получения состояния поля: {result['status']} {result['data']}")

    data = result["data"]

    if not isinstance(data, dict) or "boardSize" not in data:
        raise RuntimeError(f"Неожиданный ответ /api/power-blocks/current: {data!r}")

    board_size = data["boardSize"]
    occupied = [[False] * board_size for _ in range(board_size)]
    my_on_board = 0

    for p in data.get("placements", []):
        x, y = p["x"], p["y"]
        if "side" in p:
            w = h = p["side"]
        else:
            w = p.get("width", 1)
            h = p.get("height", 1)
        for i in range(x, min(x + w, board_size)):
            for j in range(y, min(y + h, board_size)):
                occupied[i][j] = True
        if p.get("owner") == "me":
            my_on_board += 1

    return data["id"], board_size, occupied, my_on_board, data.get("endsInSeconds", 0)


async def get_free_figures(page):
    """Получить свои фигурки, которые ещё не стоят на поле (state == IDLE),
    отсортированные по мощности (power) по убыванию - сильные ставим первыми."""
    result = await api_get(
        page,
        "https://nmt.gg/api/figures?sort=power&dir=desc&skip=0&take=100",
    )

    if result["status"] != 200:
        raise RuntimeError(f"Ошибка получения фигурок: {result['status']} {result['data']}")

    data = result["data"]

    items = data.get("items") if isinstance(data, dict) else None
    if items is None:
        # Сервер вернул что-то неожиданное (например, ошибку авторизации).
        # Не падаем - просто сообщаем и возвращаем пустой список,
        # чтобы код выше мог попробовать ещё раз на следующей итерации.
        print(f"Неожиданный ответ /api/figures, пропускаю: {data!r}")
        return []

    return [f for f in items if f.get("state") == "IDLE"]


def find_spot(occupied, board_size, width, height):
    """
    Находит случайное свободное место для прямоугольника width x height.
    Пробует обе ориентации (width x height и height x width) - так
    прямоугольная фигурка чаще находит место в узких пустотах поля.
    Возвращает (x, y, w, h) или None, если места не нашлось ни в одной
    ориентации.
    """
    if not occupied or not isinstance(occupied, list):
        return None

    orientations = {(width, height), (height, width)}  # set убирает дубль при width == height
    spots = []

    for w, h in orientations:
        for x in range(board_size - w + 1):
            for y in range(board_size - h + 1):
                free = True
                for i in range(w):
                    for j in range(h):
                        if x + i >= board_size or y + j >= board_size:
                            free = False
                            break
                        if occupied[x + i][y + j]:
                            free = False
                            break
                    if not free:
                        break
                if free:
                    spots.append((x, y, w, h))

    if not spots:
        return None

    return random.choice(spots)


async def fill_board(page, account_name, email="", max_retries=3):
    """
    Расставляет фигурки с детальным логированием.
    account_name - имя аккаунта для логирования
    email - email аккаунта для логирования
    max_retries - количество попыток при конфликте места
    """
    try:
        # Получаем состояние поля (конфиг сайта для размеров больше не
        # нужен - размер теперь считается по силе фигурки, см.
        # get_figure_footprint / POWER_SIZE_TABLE)
        round_id, board_size, occupied, my_on_board, ends_in = await get_board_state(page)
        
        email_info = f" ({email})" if email else ""
        slots_left = MAX_ON_BOARD - my_on_board
        print(f"[{account_name}{email_info}] Раунд {round_id}: {my_on_board}/{MAX_ON_BOARD} слотов занято, свободно: {slots_left}")
        
        if slots_left <= 0:
            print(f"[{account_name}{email_info}] ✅ Все 5 слотов уже заняты в этом раунде")
            return 0, 0, {"all_slots_filled": True}, None
        
        # Получаем свободные фигурки
        figures = await get_free_figures(page)
        if not figures:
            print(f"[{account_name}{email_info}] ⚠️ Нет свободных фигурок в инвентаре")
            return 0, 0, {"no_figures": True}, None
        
        # Сортируем по мощности
        figures.sort(key=lambda f: f.get("power", 0), reverse=True)
        
        placed_count = 0
        total_cells = 0
        errors = []
        skipped_by_power = []  # Фигурки, пропущенные из-за нехватки энергии
        skipped_by_space = []  # Фигурки, пропущенные из-за места
        
        print(f"[{account_name}{email_info}] Найдено свободных фигурок: {len(figures)}")
        
        for fig in figures:
            if placed_count >= slots_left:
                break
            
            fig_name = fig.get("name", "Unknown")
            fig_power = fig.get("power", 0)
            fig_id = fig["id"]
            
            # Проверяем, хватает ли энергии
            if fig_power <= 0:
                error_msg = f"нет энергии (power=0)"
                print(f"[{account_name}{email_info}] ⚠️ {fig_name}: {error_msg} - пропускаю")
                skipped_by_power.append(f"{fig_name}: {error_msg}")
                continue
            
            rarity = fig["rarity"]
            width, height = get_figure_footprint(fig_power)
            needed_power = width * height
            
            # Проверяем, хватит ли энергии для постановки (по идее всегда
            # хватает, т.к. размер сам подобран под текущую силу фигурки -
            # но оставляем проверку на случай пограничных значений)
            if fig_power < needed_power:
                error_msg = f"недостаточно энергии (нужно {needed_power}, есть {fig_power})"
                print(f"[{account_name}{email_info}] ⚠️ {fig_name}: {error_msg}")
                skipped_by_power.append(f"{fig_name}: {error_msg}")
                continue
            
            # Ищем место с повторными попытками при конфликте
            placed_successfully = False
            
            for attempt in range(max_retries):
                # Если это не первая попытка - обновляем состояние поля
                if attempt > 0:
                    _, board_size, occupied, _, _ = await get_board_state(page)
                    await asyncio.sleep(random.uniform(0.3, 1.0))
                
                spot = find_spot(occupied, board_size, width, height)
                if spot is None:
                    if attempt == 0:
                        error_msg = f"нет места {width}x{height}"
                        print(f"[{account_name}{email_info}] ⚠️ {fig_name}: {error_msg}")
                        skipped_by_space.append(f"{fig_name}: {error_msg}")
                    break
                
                x, y, w, h = spot
                
                try:
                    result = await place_figure(page, fig_id, x, y)
                    
                    # Успешно!
                    cells_this = w * h
                    total_cells += cells_this
                    placed_count += 1
                    placed_successfully = True
                    
                    # Обновляем occupied
                    mark_occupied(occupied, board_size, x, y, w, h)
                    
                    print(f"[{account_name}{email_info}] ✅ Поставил '{fig_name}' (rarity={rarity}, {w}x{h}) на ({x},{y}) - энергии: {fig_power}")
                    
                    # Пауза между фигурками
                    if placed_count < slots_left:
                        await asyncio.sleep(random.uniform(0.5, 2.0))
                    
                    break  # Успешно, выходим из retry
                    
                except RuntimeError as e:
                    error_msg = str(e)
                    
                    if "Overlaps another block" in error_msg and attempt < max_retries - 1:
                        print(f"[{account_name}{email_info}] 🔄 {fig_name}: конфликт места, пробую найти другое...")
                        continue
                    else:
                        if "Not enough power" in error_msg:
                            print(f"[{account_name}{email_info}] ❌ {fig_name}: недостаточно энергии!")
                            skipped_by_power.append(f"{fig_name}: недостаточно энергии")
                        else:
                            print(f"[{account_name}{email_info}] ❌ {fig_name}: ошибка - {error_msg[:100]}")
                            errors.append(f"{fig_name}: {error_msg[:100]}")
                        break
            
            if not placed_successfully:
                # Если не поставили - обновляем occupied для след. фигурок
                if spot:
                    _, board_size, occupied, _, _ = await get_board_state(page)
        
        # Формируем детальный отчёт
        report = {
            "placed": placed_count,
            "total_cells": total_cells,
            "skipped_power": skipped_by_power,
            "skipped_space": skipped_by_space,
            "errors": errors
        }
        
        # Логируем результат
        print(f"[{account_name}{email_info}] 📊 Итого: поставлено {placed_count} фигурок, занято {total_cells} клеток")
        
        if skipped_by_power:
            print(f"[{account_name}{email_info}] ⚠️ Пропущено по энергии: {len(skipped_by_power)}")
            for item in skipped_by_power[:3]:
                print(f"  - {item}")
            if len(skipped_by_power) > 3:
                print(f"  ... и ещё {len(skipped_by_power) - 3}")
        
        if skipped_by_space:
            print(f"[{account_name}{email_info}] ⚠️ Пропущено по месту: {len(skipped_by_space)}")
            for item in skipped_by_space[:3]:
                print(f"  - {item}")
            if len(skipped_by_space) > 3:
                print(f"  ... и ещё {len(skipped_by_space) - 3}")
        
        if errors:
            print(f"[{account_name}{email_info}] ❌ Ошибок: {len(errors)}")
            for err in errors[:3]:
                print(f"  - {err}")
        
        return placed_count, total_cells, report, None
        
    except Exception as e:
        email_info = f" ({email})" if email else ""
        print(f"[{account_name}{email_info}] ❌ Критическая ошибка: {e}")
        return 0, 0, None, str(e)

def mark_occupied(occupied, board_size, x, y, width, height):
    for i in range(x, min(x + width, board_size)):
        for j in range(y, min(y + height, board_size)):
            occupied[i][j] = True

# power_blocks.py - добавить в конец файла

async def get_inventory_figures(page):
    """
    Получает все фигуры из инвентаря (включая BOX).
    Возвращает список фигур.
    """
    result = await api_get(
        page,
        "https://nmt.gg/api/figures?sort=power&dir=desc&skip=0&take=100",
    )

    if result["status"] != 200:
        raise RuntimeError(f"Ошибка получения инвентаря: {result['status']} {result['data']}")

    data = result["data"]
    items = data.get("items") if isinstance(data, dict) else None
    
    if items is None:
        print(f"Неожиданный ответ /api/figures, пропускаю: {data!r}")
        return []
    
    return items


async def open_box(page, box_id: str):
    """
    Открывает один кейс по его ID.
    Возвращает (успех, список полученных фигур, сообщение_об_ошибке).
    """
    result = await api_post(
        page,
        f"https://nmt.gg/api/boxes/items/{box_id}/open",
        {},  # пустой payload
    )
    
    if result["status"] not in (200, 201):
        error_msg = f"Не удалось открыть кейс {box_id}: {result['status']} {result['data']}"
        raise RuntimeError(error_msg)
    
    data = result["data"]
    figures = data.get("figures", [])
    box_name = data.get("boxName", "Unknown")
    
    return box_name, figures, None


async def open_all_boxes_for_account(page, account_name, email=""):
    """
    Открывает все кейсы (BOX) в инвентаре аккаунта.
    Возвращает: (имя, открыто_кейсов, список_результатов, ошибка)
    """
    try:
        email_info = f" ({email})" if email else ""
        print(f"[{account_name}{email_info}] Проверяю инвентарь на наличие кейсов...")
        
        # Получаем все фигуры
        figures = await get_inventory_figures(page)
        
        if not figures:
            print(f"[{account_name}{email_info}] Инвентарь пуст")
            return account_name, 0, [], None
        
        # Фильтруем только BOX
        boxes = [f for f in figures if f.get("kind") == "BOX"]
        
        if not boxes:
            print(f"[{account_name}{email_info}] Нет кейсов в инвентаре")
            return account_name, 0, [], None
        
        print(f"[{account_name}{email_info}] Найдено кейсов: {len(boxes)}")
        
        opened_count = 0
        results = []
        total_figures_received = 0
        
        for box in boxes:
            box_id = box["id"]
            box_name = box.get("name", "Unknown")
            box_rarity = box.get("rarity", 0)
            
            try:
                print(f"[{account_name}{email_info}] Открываю кейс: {box_name} (ID: {box_id[:8]}...)")
                
                # Открываем кейс
                opened_name, received_figures, error = await open_box(page, box_id)
                
                if error:
                    results.append({
                        "box_name": box_name,
                        "success": False,
                        "error": error,
                        "figures": []
                    })
                    print(f"[{account_name}{email_info}] ❌ Ошибка открытия {box_name}: {error}")
                else:
                    fig_count = len(received_figures)
                    total_figures_received += fig_count
                    opened_count += 1
                    
                    # Формируем список полученных фигур для отчёта
                    fig_names = [f.get("name", "Unknown") for f in received_figures[:5]]
                    fig_names_str = ", ".join(fig_names)
                    if len(received_figures) > 5:
                        fig_names_str += f" ... и ещё {len(received_figures) - 5}"
                    
                    results.append({
                        "box_name": opened_name or box_name,
                        "success": True,
                        "error": None,
                        "figures": received_figures,
                        "figures_count": fig_count,
                        "figures_names": fig_names_str
                    })
                    
                    print(f"[{account_name}{email_info}] ✅ Открыт кейс {opened_name or box_name}: +{fig_count} фигурок")
                    
                    # Небольшая пауза между открытиями
                    await asyncio.sleep(random.uniform(1.0, 2.5))
                    
            except Exception as e:
                error_msg = str(e)
                results.append({
                    "box_name": box_name,
                    "success": False,
                    "error": error_msg,
                    "figures": []
                })
                print(f"[{account_name}{email_info}] ❌ Ошибка открытия {box_name}: {error_msg}")
        
        return account_name, opened_count, results, None
        
    except Exception as e:
        email_info = f" ({email})" if email else ""
        print(f"[{account_name}{email_info}] ❌ Критическая ошибка: {e}")
        return account_name, 0, [], str(e)


async def place_figure(page, figure_id, x, y):
    result = await api_post(
        page,
        "https://nmt.gg/api/power-blocks/place",
        {"figureId": figure_id, "x": x, "y": y},
    )
    if result["status"] not in (200, 201):
        raise RuntimeError(f"Не удалось поставить фигурку {figure_id}: {result['status']} {result['data']}")
    return result["data"]


async def activate_promo(page, code: str):
    """
    Активирует промокод через UI сайта (по описанию из твоего скриншота/html):

    1. Кликаем на аватар (.user-btn) - открывается меню пользователя.
    2. В меню (.umenu__list) кликаем на пункт "Промокод".
    3. В появившейся мини-модалке вводим код в input и жмём
       кнопку "Активировать" (.promo-modal__cta).
    4. Читаем результат из бейджа (.promo-modal__badge) - если у него
       класс "is-err", значит промокод не сработал, иначе - успех.

    Возвращает (текст_результата, успех: bool).
    """
    await page.goto("https://nmt.gg", wait_until="domcontentloaded")
    await dismiss_popups(page)

    # 1. Открываем меню пользователя по аватару
    await page.locator(".user-btn").click()

    # 2. Ждём список меню и кликаем на пункт "Промокод"/"Promo"
    # (сайт может показывать текст на разных языках, поэтому ищем и
    # русский, и английский варианты, без учёта регистра)
    menu = page.locator(".umenu__list")
    await menu.wait_for()

    promo_item = menu.get_by_text(re.compile(r"промокод|promo", re.IGNORECASE))
    if await promo_item.count() == 0:
        menu_text = await menu.inner_text()
        raise RuntimeError(
            f"Не нашёл пункт меню 'Промокод/Promo'. Содержимое меню: {menu_text!r}"
        )
    await promo_item.first.click()

    # 3. Вводим код в поле модалки и жмём "Активировать"
    promo_input = page.locator('input[placeholder="WELCOME100"]')
    await promo_input.wait_for()
    await promo_input.fill(code)

    submit_btn = page.locator(".promo-modal__cta")
    await submit_btn.click()

    # 4. Читаем результат
    badge = page.locator(".promo-modal__badge")
    await badge.wait_for(timeout=10000)

    text = (await badge.inner_text()).strip()
    badge_class = await badge.get_attribute("class") or ""
    is_success = "is-err" not in badge_class

    return text, is_success

# power_blocks.py - добавляем новые функции в конец файла

async def get_inventory_figures(page):
    """
    Получает все фигуры из инвентаря (включая BOX и LISTED).
    Возвращает список фигур.
    """
    result = await api_get(
        page,
        "https://nmt.gg/api/figures?sort=power&dir=desc&skip=0&take=100",
    )

    if result["status"] != 200:
        raise RuntimeError(f"Ошибка получения инвентаря: {result['status']} {result['data']}")

    data = result["data"]
    items = data.get("items") if isinstance(data, dict) else None
    
    if items is None:
        print(f"Неожиданный ответ /api/figures, пропускаю: {data!r}")
        return []
    
    return items


async def list_on_marketplace(page, figure_id: str, price: int):
    """
    Выставляет фигуру на маркетплейс с указанной ценой.
    Возвращает (успех, ответ_сервера, ошибка).
    """
    payload = {
        "figureId": figure_id,
        "priceNmt": price
    }
    
    result = await api_post(
        page,
        "https://nmt.gg/api/marketplace",
        payload,
    )
    
    if result["status"] not in (200, 201):
        error_msg = f"Не удалось выставить фигуру: {result['status']} {result['data']}"
        raise RuntimeError(error_msg)
    
    return True, result["data"], None


def should_list_figure(figure) -> bool:
    """
    Определяет, нужно ли выставлять фигуру на маркет.
    Критерии:
    - Серые (rarity=0): энергия <= 10
    - Синие (rarity=1): энергия <= 5
    - Фиолетовые (rarity=2): энергия <= 10
    - Золотые (rarity=3): энергия <= 16
    - Фигура не должна быть уже LISTED или ON_BOARD
    """
    rarity = figure.get("rarity", 0)
    power = figure.get("power", 0)
    state = figure.get("state", "IDLE")
    
    # Не выставляем уже выставленные или стоящие на поле
    if state in ("LISTED", "ON_BOARD"):
        return False
    
    # Не выставляем BOX
    if figure.get("kind") == "BOX":
        return False
    
    # Проверяем по редкости
    if rarity == 0:  # Серая
        return power <= 10
    elif rarity == 1:  # Синяя
        return power <= 5
    elif rarity == 2:  # Фиолетовая
        return power <= 10
    elif rarity == 3:  # Золотая
        return power <= 16
    
    return False


def get_price_for_figure(rarity: int) -> int:
    """
    Возвращает рандомную цену для фигуры в зависимости от редкости.
    """
    import random
    
    if rarity == 0:  # Серая
        return random.randint(5, 10)
    elif rarity == 1:  # Синяя
        return random.randint(11, 16)
    elif rarity == 2:  # Фиолетовая
        return random.randint(17, 24)
    elif rarity == 3:  # Золотая
        return random.randint(24, 29)
    else:
        return random.randint(5, 20)


async def list_dead_figures_for_account(page, account_name, email=""):
    """
    Находит все мёртвые фигуры в инвентаре аккаунта и выставляет их на маркет.
    Возвращает: (имя, список_результатов, ошибка)
    """
    try:
        email_info = f" ({email})" if email else ""
        print(f"[{account_name}{email_info}] Проверяю инвентарь на мёртвые фигуры...")
        
        # Получаем все фигуры
        figures = await get_inventory_figures(page)
        
        if not figures:
            print(f"[{account_name}{email_info}] Инвентарь пуст")
            return account_name, [], None
        
        # Фильтруем фигуры, которые можно выставить
        dead_figures = []
        for fig in figures:
            if should_list_figure(fig):
                dead_figures.append(fig)
        
        if not dead_figures:
            print(f"[{account_name}{email_info}] Нет мёртвых фигур для выставления")
            return account_name, [], None
        
        print(f"[{account_name}{email_info}] Найдено мёртвых фигур: {len(dead_figures)}")
        
        results = []
        listed_count = 0
        
        # Перемешиваем для рандомизации порядка
        random.shuffle(dead_figures)
        
        for fig in dead_figures:
            fig_id = fig["id"]
            fig_name = fig.get("name", "Unknown")
            rarity = fig.get("rarity", 0)
            power = fig.get("power", 0)
            
            # Определяем цвет по редкости
            rarity_names = {0: "серую", 1: "синюю", 2: "фиолетовую", 3: "золотую"}
            rarity_name = rarity_names.get(rarity, "неизвестную")
            
            # Получаем цену
            price = get_price_for_figure(rarity)
            
            try:
                print(f"[{account_name}{email_info}] Выставляю '{fig_name}' ({rarity_name}, энергия={power}) за {price} NMT...")
                
                success, data, error = await list_on_marketplace(page, fig_id, price)
                
                if error:
                    results.append({
                        "name": fig_name,
                        "rarity": rarity,
                        "power": power,
                        "price": price,
                        "success": False,
                        "error": error
                    })
                    print(f"[{account_name}{email_info}] ❌ Ошибка выставления {fig_name}: {error}")
                else:
                    results.append({
                        "name": fig_name,
                        "rarity": rarity,
                        "power": power,
                        "price": price,
                        "success": True,
                        "error": None,
                        "listing_id": data.get("id") if isinstance(data, dict) else None
                    })
                    listed_count += 1
                    print(f"[{account_name}{email_info}] ✅ {fig_name} выставлен за {price} NMT")
                    
                    # Пауза между выставлениями
                    await asyncio.sleep(random.uniform(0.5, 1.5))
                    
            except Exception as e:
                error_msg = str(e)
                results.append({
                    "name": fig_name,
                    "rarity": rarity,
                    "power": power,
                    "price": price,
                    "success": False,
                    "error": error_msg
                })
                print(f"[{account_name}{email_info}] ❌ Ошибка выставления {fig_name}: {error_msg}")
        
        print(f"[{account_name}{email_info}] 📊 Итого: выставлено {listed_count}/{len(dead_figures)} фигур")
        return account_name, results, None
        
    except Exception as e:
        email_info = f" ({email})" if email else ""
        print(f"[{account_name}{email_info}] ❌ Критическая ошибка: {e}")
        return account_name, [], str(e)


#================================================================================

async def join_giveaway_with_code(page, account_name, email="", target_code=""):
    """
    Находит розыгрыш с указанным entryCode и присоединяется к нему.
    Возвращает: (имя, список_результатов, ошибка)
    """
    try:
        email_info = f" ({email})" if email else ""
        print(f"[{account_name}{email_info}] Ищу розыгрыш с кодом: {target_code}")
        
        # Получаем все открытые розыгрыши
        giveaways = await get_open_giveaways(page)
        
        if not giveaways:
            print(f"[{account_name}{email_info}] Нет активных розыгрышей")
            return account_name, [], None
        
        # Ищем розыгрыш с нужным кодом
        target = None
        for g in giveaways:
            entry_code = g.get("entryCode", "")
            if entry_code.upper() == target_code.upper():
                target = g
                break
        
        if not target:
            print(f"[{account_name}{email_info}] Розыгрыш с кодом {target_code} не найден")
            return account_name, [], None
        
        giveaway_id = target["id"]
        entry_code = target["entryCode"]
        creator = target.get("creator", {}).get("name", "Unknown")
        
        print(f"[{account_name}{email_info}] Найден розыгрыш от {creator} (ID: {giveaway_id[:8]}...)")
        
        # Пробуем присоединиться
        try:
            success, error = await join_giveaway(page, giveaway_id, entry_code)
            
            if success:
                print(f"[{account_name}{email_info}] ✅ Участвую в розыгрыше от {creator}")
                return account_name, [{
                    "id": giveaway_id,
                    "creator": creator,
                    "success": True,
                    "reason": "joined",
                    "error": None,
                    "entry_code": entry_code
                }], None
            else:
                # Если ошибка "already_joined" - не считаем за ошибку
                if "already" in str(error).lower():
                    print(f"[{account_name}{email_info}] ⏭️ Уже участвую в розыгрыше")
                    return account_name, [{
                        "id": giveaway_id,
                        "creator": creator,
                        "success": True,
                        "reason": "already_joined",
                        "error": None,
                        "entry_code": entry_code
                    }], None
                else:
                    print(f"[{account_name}{email_info}] ❌ Ошибка: {error}")
                    return account_name, [{
                        "id": giveaway_id,
                        "creator": creator,
                        "success": False,
                        "reason": "error",
                        "error": error,
                        "entry_code": entry_code
                    }], None
                    
        except Exception as e:
            error_msg = str(e)
            if "already" in error_msg.lower():
                return account_name, [{
                    "id": giveaway_id,
                    "creator": creator,
                    "success": True,
                    "reason": "already_joined",
                    "error": None,
                    "entry_code": entry_code
                }], None
            else:
                return account_name, [{
                    "id": giveaway_id,
                    "creator": creator,
                    "success": False,
                    "reason": "error",
                    "error": error_msg,
                    "entry_code": entry_code
                }], None
        
    except Exception as e:
        email_info = f" ({email})" if email else ""
        print(f"[{account_name}{email_info}] ❌ Критическая ошибка: {e}")
        return account_name, [], str(e)

async def get_open_giveaways(page):
    """
    Получает список активных розыгрышей с их entryCode.
    """
    result = await api_get(
        page,
        "https://nmt.gg/api/giveaways?status=open&take=100",
    )

    if result["status"] != 200:
        raise RuntimeError(f"Ошибка получения розыгрышей: {result['status']} {result['data']}")

    data = result["data"]
    items = data.get("items") if isinstance(data, dict) else None
    
    if items is None:
        print(f"Неожиданный ответ /api/giveaways, пропускаю: {data!r}")
        return []
    
    return items


async def join_giveaway(page, giveaway_id: str, entry_code: str):
    """
    Участвует в розыгрыше через API с указанием entryCode.
    Возвращает (успех, сообщение_об_ошибке).
    """
    payload = {
        "entryCode": entry_code
    }
    
    result = await api_post(
        page,
        f"https://nmt.gg/api/giveaways/{giveaway_id}/join",
        payload,
    )
    
    if result["status"] not in (200, 201):
        error_msg = f"Не удалось присоединиться к розыгрышу: {result['status']} {result['data']}"
        raise RuntimeError(error_msg)
    
    return True, None


async def join_all_giveaways_for_account(page, account_name, email=""):
    """
    Проверяет и присоединяется ко ВСЕМ активным розыгрышам для аккаунта.
    Возвращает: (имя, список_результатов, ошибка)
    """
    try:
        email_info = f" ({email})" if email else ""
        print(f"[{account_name}{email_info}] Проверяю доступные розыгрыши...")
        
        # Получаем все открытые розыгрыши
        giveaways = await get_open_giveaways(page)
        
        if not giveaways:
            print(f"[{account_name}{email_info}] Нет активных розыгрышей")
            return account_name, [], None
        
        print(f"[{account_name}{email_info}] Найдено розыгрышей: {len(giveaways)}")
        
        results = []
        joined_count = 0
        already_count = 0
        error_count = 0
        
        for giveaway in giveaways:
            giveaway_id = giveaway["id"]
            entry_code = giveaway.get("entryCode", "")
            creator_name = giveaway.get("creator", {}).get("name", "Unknown")
            participant_count = giveaway.get("participantCount", 0)
            
            if not entry_code:
                print(f"[{account_name}{email_info}] ⚠️ Розыгрыш от {creator_name} без entryCode, пропускаю")
                results.append({
                    "id": giveaway_id,
                    "creator": creator_name,
                    "success": False,
                    "reason": "no_entry_code",
                    "error": "Розыгрыш не имеет кода для входа",
                    "participants": participant_count
                })
                error_count += 1
                continue
            
            try:
                # Пробуем присоединиться с кодом
                success, error = await join_giveaway(page, giveaway_id, entry_code)
                
                if success:
                    print(f"[{account_name}{email_info}] ✅ Присоединился к розыгрышу от {creator_name} (код: {entry_code})")
                    results.append({
                        "id": giveaway_id,
                        "creator": creator_name,
                        "success": True,
                        "reason": "joined",
                        "error": None,
                        "participants": participant_count,
                        "entry_code": entry_code
                    })
                    joined_count += 1
                else:
                    print(f"[{account_name}{email_info}] ❌ Ошибка: {error}")
                    results.append({
                        "id": giveaway_id,
                        "creator": creator_name,
                        "success": False,
                        "reason": "error",
                        "error": error,
                        "participants": participant_count,
                        "entry_code": entry_code
                    })
                    error_count += 1
                
                # Пауза между розыгрышами
                await asyncio.sleep(random.uniform(1.0, 2.0))
                
            except Exception as e:
                error_msg = str(e)
                print(f"[{account_name}{email_info}] ❌ Ошибка: {error_msg}")
                results.append({
                    "id": giveaway_id,
                    "creator": creator_name,
                    "success": False,
                    "reason": "error",
                    "error": error_msg,
                    "participants": participant_count,
                    "entry_code": entry_code
                })
                error_count += 1
        
        print(f"[{account_name}{email_info}] 📊 Итого: {joined_count} новых, {already_count} уже были, {error_count} ошибок")
        return account_name, results, None
        
    except Exception as e:
        email_info = f" ({email})" if email else ""
        print(f"[{account_name}{email_info}] ❌ Критическая ошибка: {e}")
        return account_name, [], str(e)

async def check_giveaway_joined(page, giveaway_id: str) -> bool:
    """
    Проверяет, участвует ли аккаунт в конкретном розыгрыше.
    """
    result = await api_get(
        page,
        f"https://nmt.gg/api/giveaways/{giveaway_id}",
    )

    if result["status"] != 200:
        raise RuntimeError(f"Ошибка проверки розыгрыша: {result['status']} {result['data']}")

    data = result["data"]
    return data.get("joined", False) if isinstance(data, dict) else False