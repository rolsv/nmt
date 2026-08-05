# health_checker.py - обновлённая версия с последовательной проверкой
"""
Фоновая периодическая проверка "здоровья" аккаунтов - работает
НЕЗАВИСИМО от того, запущен ли автофарм (просто крутится всё время, пока
жив бот). Раз в HEALTH_CHECK_INTERVAL_MIN минут по очереди проверяет
сессию каждого аккаунта и:

  - если сессия невалидна и есть email/password - сразу пробует
    перелогиниться автоматически;
  - шлёт уведомление в Telegram ТОЛЬКО когда статус аккаунта поменялся
    (был живой -> сломался, или наоборот ожил), чтобы не спамить одним и
    тем же сообщением каждую проверку.
"""
import asyncio
import random

from config import HEALTH_CHECK_INTERVAL_MIN
from browser_actions import logged_in, login
from context_manager import account_context


async def check_one_account(state, acc, bot):
    try:
        async with acc.page_lock:
            async with account_context(state.browser, acc) as page:
                healthy = await logged_in(page)
    except Exception as e:
        healthy = False
        print(f"[{acc.name}] health-check ошибка: {e}")

    was_healthy = acc.last_known_healthy

    if healthy and not was_healthy:
        await bot.send_message(state.admin_chat_id, f"✅ [{acc.name}] Аккаунт снова в порядке.")
    elif not healthy and was_healthy:
        if acc.email and acc.password:
            note = "Попробую перелогиниться автоматически."
        else:
            note = f"Это Google-аккаунт - нужен ручной перелогин: python manual_login.py {acc.name}"
        await bot.send_message(
            state.admin_chat_id,
            f"⚠️ [{acc.name}] Сессия невалидна.\n{note}",
        )

    acc.last_known_healthy = healthy

    # Если сессия невалидна и логин можно сделать автоматически - пробуем
    # сразу, не дожидаясь следующей проверки/действия.
    if not healthy and acc.email and acc.password:
        try:
            async with acc.page_lock:
                async with account_context(state.browser, acc) as page:
                    await login(page, acc.email, acc.password, acc.session_file)
            acc.last_known_healthy = True
            acc.mark_success()
            await bot.send_message(state.admin_chat_id, f"✅ [{acc.name}] Перелогинился автоматически.")
        except Exception as e:
            await bot.send_message(state.admin_chat_id, f"⚠️ [{acc.name}] Не удалось перелогиниться: {e}")


async def health_check_loop(state, bot):
    """Бесконечный цикл фоновой проверки. Отменяется через
    asyncio.Task.cancel() при остановке бота (main.py)."""
    while True:
        await asyncio.sleep(HEALTH_CHECK_INTERVAL_MIN * 60)

        # Перемешиваем аккаунты для рандомизации порядка проверки
        accounts_to_check = list(state.accounts)
        random.shuffle(accounts_to_check)

        for acc in accounts_to_check:
            await check_one_account(state, acc, bot)
            # Случайная пауза между аккаунтами (2-5 секунд)
            await asyncio.sleep(random.uniform(2.0, 5.0))