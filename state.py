# state.py - добавим импорт farm_stats
"""
Общее состояние приложения при работе с несколькими аккаунтами.

AccountState теперь - это просто "паспорт" аккаунта: данные для входа и
путь к файлу сессии. НИКАКОГО открытого BrowserContext/Page тут не
хранится - они создаются временно через context_manager.account_context()
прямо перед действием и сразу закрываются после. Это экономит память при
большом количестве аккаунтов (не висят десятки открытых контекстов).
"""
import asyncio
import time


class AccountState:
    def __init__(self, name, email, password, session_file, proxy=None):
        self.name = name
        self.email = email
        self.password = password
        self.session_file = session_file
        self.proxy = proxy

        # Лок защищает работу С ЭТИМ аккаунтом целиком: открытие контекста,
        # действие на сайте, сохранение сессии на диск. Нужен, чтобы две
        # команды (например, автофарм и промокод) не открыли одновременно
        # два контекста из одного и того же файла сессии и не затёрли
        # сохранение друг друга.
        self.page_lock = asyncio.Lock()

        # --- "Здоровье" аккаунта / cooldown после повторных ошибок ---
        # Если аккаунт подряд несколько раз падает с ошибкой (сайт лежит,
        # сессия невалидна и т.д.) - даём ему "отдохнуть" cooldown_minutes,
        # чтобы не долбить его на каждом раунде/действии впустую.
        self.consecutive_fails = 0
        self.cooldown_until = 0.0  # time.monotonic(); 0 = не в cooldown

        # Последний известный статус сессии (для health-check - чтобы
        # слать уведомление в Telegram только при СМЕНЕ статуса, а не
        # на каждой проверке).
        self.last_known_healthy = True

    def in_cooldown(self):
        return time.monotonic() < self.cooldown_until

    def mark_success(self):
        self.consecutive_fails = 0
        self.cooldown_until = 0.0

    def mark_failure(self, fail_threshold, cooldown_minutes):
        self.consecutive_fails += 1
        if self.consecutive_fails >= fail_threshold:
            self.cooldown_until = time.monotonic() + cooldown_minutes * 60


class AppState:
    def __init__(self, browser, accounts, watcher_name, admin_chat_id):
        self.browser = browser  # общий Playwright Browser на все аккаунты
        self.accounts = accounts  # list[AccountState]
        self.pending_promocodes = False
        self.watcher_name = watcher_name
        self.admin_chat_id = admin_chat_id
        self.awaiting_giveaway_code = {}

        # Управление автофармом (общее на все аккаунты - одна фоновая
        # задача следит за раундом и раскидывает работу по всем аккаунтам)
        self.farming = False
        self.stop_event = asyncio.Event()
        self.farm_task = None  # asyncio.Task, когда автофарм запущен

        # chat_id -> True, если для этого чата бот ждёт ввод промокода
        self.awaiting_promo = {}

        # Лок на активацию промокодов - гарантирует, что ручной ввод из
        # меню и авто-активация из промо-слушалки (каналы/чат) никогда не
        # бегут ОДНОВРЕМЕННО по одним и тем же 25 аккаунтам. Вторая
        # попытка просто дождётся, пока первая закончит, и потом выполнится
        # сама - без гонки и без двух параллельных отчётов вперемешку.
        self.promo_activation_lock = asyncio.Lock()

        # --- Промо-слушалка (мониторинг Telegram-каналов) ---
        # Telethon-клиент подключается один раз при старте бота и остаётся
        # подключенным всё время - этот флаг просто включает/выключает
        # ОБРАБОТКУ новых постов (кнопка в меню), не пересоздавая соединение.
        self.promo_listening = False
        # Коды, которые уже пробовали активировать в этой сессии - чтобы
        # один и тот же промокод, упомянутый в нескольких каналах или
        # продублированный постом, не пытались активировать по новой.
        self.seen_promo_codes = set()

    def watcher(self):
        """Аккаунт, который следит за раундами power-blocks."""
        for acc in self.accounts:
            if acc.name == self.watcher_name:
                return acc
        # На всякий случай, если имя не совпало ни с одним аккаунтом -
        # берём первый по списку, чтобы бот не падал.
        return self.accounts[0]