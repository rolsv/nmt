import asyncio

from playwright.async_api import async_playwright

from config import HEADLESS, ACCOUNTS, WATCHER_ACCOUNT, ADMIN_CHAT_ID
from browser_actions import login, logged_in, get_balance
from state import AppState, AccountState
from context_manager import account_context
from health_checker import health_check_loop
from promo_listener import start_promo_listener
from bot import start_bot


async def setup_account(browser, acc):
    """
    Проверяет/обновляет сессию аккаунта через ВРЕМЕННЫЙ контекст - он
    открывается на время проверки/логина и сразу закрывается (сессия
    сохраняется на диск). Не поднимает исключение наружу - если ОДИН
    аккаунт сломан (например, Google-аккаунт без подготовленной сессии),
    это не должно ронять запуск всего бота и остальных аккаунтов.

    Возвращает (имя, баланс_или_None, ошибка_или_None).
    """
    try:
        async with account_context(browser, acc) as page:
            if not await logged_in(page):
                if acc.email and acc.password:
                    await login(page, acc.email, acc.password, acc.session_file)
                else:
                    # Аккаунт без email/password - например, вход только
                    # через Google. Автологин для такого не делаем (Google
                    # палит автоматизацию) - сессия должна быть подготовлена
                    # заранее через manual_login.py.
                    return acc.name, None, (
                        f"нет сохранённой сессии и не задан email/password "
                        f"(похоже, это Google-аккаунт) - выполни: "
                        f"python manual_login.py {acc.name}"
                    )

            balance = await get_balance(page)

        print(f"[{acc.name}] Баланс: {balance} NMT")
        return acc.name, balance, None

    except Exception as e:
        print(f"[{acc.name}] ⚠️ Ошибка при старте: {e}")
        return acc.name, None, str(e)

async def main():

    if not ACCOUNTS:
        raise RuntimeError("В config.py пустой список ACCOUNTS - добавь хотя бы один аккаунт.")

    async with async_playwright() as p:

        browser = await p.chromium.launch(headless=HEADLESS)

        accounts = [
            AccountState(
                name=acc_cfg["name"],
                email=acc_cfg.get("email"),
                password=acc_cfg.get("password"),
                session_file=acc_cfg["session_file"],
                proxy=acc_cfg.get("proxy"),
            )
            for acc_cfg in ACCOUNTS
        ]

        # Проверяем/обновляем сессию каждого аккаунта ПО ОЧЕРЕДИ. Контекст
        # каждого аккаунта открывается и сразу закрывается внутри
        # setup_account - в памяти одновременно висит максимум один
        # открытый контекст, а не все сразу. Один сломанный аккаунт не
        # обрывает запуск остальных.
        startup_results = []
        for acc in accounts:
            result = await setup_account(browser, acc)
            startup_results.append(result)
            if result[2] is not None:
                acc.last_known_healthy = False

        # Общее состояние для телеграм-бота: браузер (для создания
        # временных контекстов по требованию), список аккаунтов, кто из
        # них "наблюдатель" за раундами и т.д. (см. state.py)
        state = AppState(browser=browser, accounts=accounts, watcher_name=WATCHER_ACCOUNT, admin_chat_id=ADMIN_CHAT_ID)

        application = await start_bot(state)

        ok_count = sum(1 for _, _, error in startup_results if error is None)
        report_lines = [f"🚀 Бот запущен. Аккаунтов готово: {ok_count}/{len(startup_results)}"]
        for name, balance, error in startup_results:
            if error is None:
                report_lines.append(f"  ✅ {name}: {balance} NMT")
            else:
                report_lines.append(f"  ⚠️ {name}: {error}")

        try:
            await application.bot.send_message(ADMIN_CHAT_ID, "\n".join(report_lines))
        except Exception as e:
            print(f"⚠️ Не удалось отправить стартовый отчёт в Telegram: {e}")

        # Фоновая периодическая проверка здоровья аккаунтов - работает
        # независимо от автофарма.
        health_task = asyncio.create_task(health_check_loop(state, application.bot))

        # Промо-слушалка (мониторинг Telegram-каналов) - подключается один
        # раз и остаётся подключенной, вкл/выкл через кнопку в меню бота.
        # Если не удалось подключиться (например, проблема с Telethon-
        # сессией) - не роняем весь бот, просто предупреждаем.
        promo_client = None
        try:
            promo_client = await start_promo_listener(state, application.bot)
        except Exception as e:
            print(f"⚠️ Не удалось запустить промо-слушалку: {e}")
            try:
                await application.bot.send_message(
                    ADMIN_CHAT_ID, f"⚠️ Промо-слушалка не запустилась: {e}"
                )
            except Exception:
                pass

        print(
            f"Telegram-бот запущен. Аккаунтов: {len(accounts)} "
            f"(наблюдатель: {state.watcher().name}). "
            f"Напиши /start в чат с ботом для меню."
        )

        try:
            # Дальше вся логика управляется из Telegram
            await asyncio.Event().wait()
        except KeyboardInterrupt:
            print("\n🛑 Получен сигнал остановки...")
        finally:
            print("🔄 Останавливаем health check...")
            health_task.cancel()
            try:
                await health_task
            except asyncio.CancelledError:
                pass

            if promo_client is not None:
                print("🔄 Отключаем промо-слушалку...")
                try:
                    await promo_client.disconnect()
                except Exception as e:
                    print(f"⚠️ Ошибка при отключении промо-слушалки: {e}")

            if state.farming:
                print("🔄 Останавливаем автофарм...")
                state.stop_event.set()
                if state.farm_task:
                    await state.farm_task

            print("🔄 Останавливаем Telegram бота...")
            await application.updater.stop()
            await application.stop()
            await application.shutdown()

            # Даем время на закрытие всех соединений
            await asyncio.sleep(0.5)
            
            print("🔄 Закрываем браузер...")
            try:
                await browser.close()
            except Exception as e:
                print(f"⚠️ Ошибка при закрытии браузера: {e}")

if __name__ == "__main__":
    asyncio.run(main())