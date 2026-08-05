"""
Менеджер временных контекстов браузера для одного аккаунта.

Вместо того чтобы держать в памяти открытый BrowserContext/Page на каждый
аккаунт постоянно (это дорого по RAM при большом числе аккаунтов),
контекст открывается ТОЛЬКО на время конкретного действия (проверить
баланс, ввести промокод, расставить фигурки) и сразу закрывается. Сессия
(куки/localStorage) при этом всегда сохраняется на диск перед закрытием -
даже если внутри действия случилась ошибка.
"""
import os
from contextlib import asynccontextmanager

from config import TIMEOUT

# Аккаунтам за прокси даём больше времени на ответ - прокси сам по себе
# добавляет задержку поверх обычной, и 30 сек (стандартный TIMEOUT) на
# всё (goto + рендер + запрос) иногда просто не хватает.
PROXY_TIMEOUT_MULTIPLIER = 2


@asynccontextmanager
async def account_context(browser, acc):
    """
    Открывает временный BrowserContext+Page для аккаунта acc (используя
    сохранённую сессию acc.session_file, если она есть, и его прокси, если
    задан), отдаёт готовую page. При выходе из `async with` - в любом
    случае, даже при исключении - сохраняет актуальную сессию обратно в
    файл и закрывает контекст.

    Использование:
        async with account_context(browser, acc) as page:
            await page.goto(...)
    """
    context_kwargs = {}
    if os.path.exists(acc.session_file):
        context_kwargs["storage_state"] = acc.session_file
    if acc.proxy:
        context_kwargs["proxy"] = acc.proxy

    context = await browser.new_context(**context_kwargs)
    try:
        page = await context.new_page()
        timeout = TIMEOUT * PROXY_TIMEOUT_MULTIPLIER if acc.proxy else TIMEOUT
        page.set_default_timeout(timeout)
        yield page
    finally:
        try:
            await context.storage_state(path=acc.session_file)
        except Exception as e:
            print(f"[{acc.name}] ⚠️ Не удалось сохранить сессию: {e}")
        await context.close()