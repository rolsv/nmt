"""
Базовые действия с браузером: логин, проверка сессии, получение баланса.

Работают с ЛЮБЫМ аккаунтом - вызывающий код (main.py, bot.py) передаёт
email/password/session_file конкретного аккаунта, сама функция ничего
не знает про то, сколько всего аккаунтов и какой из них "текущий".
"""


async def dismiss_popups(page, max_wait_ms=3000, poll_ms=150):
    """
    Закрывает всплывающие окошки-подсказки, которые могут вылезти после
    захода на сайт: "Turn on notifications", обучающий поповер Power Blocks
    (pb-howto) и модальное окно с новостями (news-enter-modal).
    
    Они появляются НЕ гарантированно, в любом порядке и иногда
    с задержкой - поэтому просто опрашиваем страницу несколько раз подряд
    и закрываем всё, что найдём, пока не истечёт max_wait_ms.

    Безопасно вызывать в любом месте (в том числе если окошек нет вообще -
    тогда просто ничего не произойдёт).
    """
    elapsed = 0
    # Флаги чтобы не пытаться закрыть одно и то же окно повторно
    closed_news = False
    closed_notifications = False
    closed_howto = False
    
    while elapsed <= max_wait_ms:
        closed_something = False

        # 0. Модальное окно новостей "Бета-тест NMT завершён" - жмём "Закрыть"
        if not closed_news:
            news_close = page.locator(".news-enter-modal .news-enter__close")
            if await news_close.count() > 0:
                try:
                    if await news_close.first.is_visible():
                        await news_close.first.click(timeout=1500)
                        closed_news = True
                        closed_something = True
                        print("[dismiss] Закрыто окно новостей")
                except Exception as e:
                    # Если кнопка уже исчезла (detached) - считаем что окно закрыто
                    if "detached" in str(e):
                        closed_news = True
                    else:
                        print(f"[dismiss] Ошибка закрытия новостей: {e}")

        # 1. "Turn on notifications" - жмём "Not now"
        if not closed_notifications:
            push_later = page.locator("button.push-nudge__later")
            if await push_later.count() > 0:
                try:
                    if await push_later.first.is_visible():
                        await push_later.first.click(timeout=1500)
                        closed_notifications = True
                        closed_something = True
                        print("[dismiss] Закрыто уведомление о push")
                except Exception:
                    pass

        # 2. Power Blocks howto - жмём крестик закрытия в шапке.
        # Если вдруг крестика нет - жмём "Skip" в футере как запасной вариант.
        if not closed_howto:
            howto_close = page.locator(".pb-howto__head button.iconbtn[aria-label='Close']")
            if await howto_close.count() > 0:
                try:
                    if await howto_close.first.is_visible():
                        await howto_close.first.click(timeout=1500)
                        closed_howto = True
                        closed_something = True
                        print("[dismiss] Закрыт howto (крестик)")
                except Exception:
                    pass
            else:
                howto_skip = page.locator(".pb-howto__actions button.btn--ghost")
                if await howto_skip.count() > 0:
                    try:
                        if await howto_skip.first.is_visible():
                            await howto_skip.first.click(timeout=1500)
                            closed_howto = True
                            closed_something = True
                            print("[dismiss] Закрыт howto (skip)")
                    except Exception:
                        pass

        # Если закрыли что-то - ждём чуть дольше
        if closed_something:
            await page.wait_for_timeout(poll_ms * 2)
            elapsed += poll_ms * 2
            continue

        await page.wait_for_timeout(poll_ms)
        elapsed += poll_ms
        
        # Если все окна закрыты - выходим раньше
        if closed_news and closed_notifications and closed_howto:
            break

async def ensure_on_site(page):
    """
    Гарантирует, что page реально находится на nmt.gg, а не на about:blank
    (это бывает сразу после создания нового BrowserContext/Page). Без
    этого попытка прочитать localStorage через page.evaluate падает с
    SecurityError - у about:blank "непрозрачное" происхождение (opaque
    origin), и браузер запрещает доступ к его localStorage.

    Дешёвая проверка: если page.url уже указывает на нужный домен - никуда
    не ходим повторно (чтобы не делать лишний goto на каждый API-запрос).
    """
    if not page.url.startswith("https://nmt.gg"):
        await page.goto("https://nmt.gg", wait_until="domcontentloaded")
        await dismiss_popups(page)


async def login(page, email, password, session_file, max_attempts=2):
    """Логинит указанный page под конкретным email/password и сохраняет
    сессию в session_file этого аккаунта. С ретраем - под прокси иногда
    первая попытка (goto/клик) может словить разовый таймаут, вторая
    попытка "с чистого листа" часто проходит нормально."""
    last_exc = None

    for attempt in range(1, max_attempts + 1):
        try:
            print(f"[{email}] Выполняю вход... (попытка {attempt}/{max_attempts})")

            await page.goto("https://nmt.gg", wait_until="domcontentloaded")
            
            # Закрываем все возможные попапы
            await dismiss_popups(page, max_wait_ms=5000)

            # Открываем окно авторизации
            login_btn = page.locator(".btn.topbar__cta")
            await login_btn.click(timeout=15000)

            # Ждем появления окна
            await page.locator(".auth-form").wait_for(timeout=10000)

            # Нажимаем именно вкладку "Войти"
            auth_tabs = page.locator(".auth-tabs .auth-tab")
            await auth_tabs.first.click()

            # Ждем переключения формы
            await page.wait_for_timeout(500)

            # Заполняем данные
            await page.locator('input[type="email"]').fill(email)
            await page.locator('input[type="password"]').fill(password)

            # Нажимаем кнопку входа
            await page.locator(".auth-submit").click()

            # Ждём загрузки после логина
            await page.wait_for_timeout(5000)

            print(f"[{email}] URL после логина: {page.url}")

            # После логина тоже может вылезти "Turn on notifications" / howto / новости
            await dismiss_popups(page, max_wait_ms=5000)

            # Сохраняем сессию
            await page.context.storage_state(path=session_file)

            print(f"[{email}] Сессия сохранена -> {session_file}")
            return

        except Exception as e:
            last_exc = e
            print(f"[{email}] ⚠️ Попытка {attempt}/{max_attempts} не удалась: {e}")
            if attempt < max_attempts:
                # Делаем скриншот для отладки
                try:
                    screenshot_path = f"login_error_{email}_{attempt}.png"
                    await page.screenshot(path=screenshot_path)
                    print(f"[{email}] Скриншот сохранён: {screenshot_path}")
                except Exception:
                    pass
                await page.wait_for_timeout(3000)
                continue

    raise last_exc


async def get_balance(page, max_attempts=3):
    """
    Читает баланс с главной страницы. wait_until="domcontentloaded" вместо
    "networkidle" - на сайте, судя по всему, идёт постоянный фоновый
    поллинг/вебсокеты, из-за чего "тишины в сети" может не наступать
    вообще, и networkidle тогда просто гарантированно таймаутится
    (особенно если одновременно открыто много контекстов).

    Дополнительно: если прочитанное значение - "0" (а его в проекте не
    бывает по словам владельца), это обычно означает, что мы поймали
    заглушку/скелетон до подгрузки реальных данных - даём странице ещё
    немного времени и пробуем ещё раз, вместо того чтобы сразу верить "0".
    """
    last_exc = None

    for attempt in range(1, max_attempts + 1):
        try:
            await page.goto("https://nmt.gg", wait_until="domcontentloaded")
            await dismiss_popups(page)

            locator = page.locator(".bal__val--brick").first
            await locator.wait_for(timeout=10000)
            text = (await locator.inner_text()).strip()

            if text == "0" and attempt < max_attempts:
                await page.wait_for_timeout(1500)
                continue

            return text
        except Exception as e:
            last_exc = e
            if attempt < max_attempts:
                await page.wait_for_timeout(1500)
                continue

    raise last_exc


async def logged_in(page):
    await page.goto("https://nmt.gg", wait_until="domcontentloaded")
    await dismiss_popups(page)

    try:
        await page.locator(".bal__val--brick").first.wait_for(timeout=10000)
        return True
    except Exception:
        return False