"""
Персистентный кэш "уже проверенных/активированных" промокодов - чтобы
промо-слушалка (каналы + чат) не пыталась активировать один и тот же код
повторно.

Хранится в JSON-файле на диске (config.PROMO_SEEN_CODES_FILE) в виде
{КОД: ISO-таймстемп}. Каждая запись "протухает" через
PROMO_CODE_EXPIRE_HOURS часов (по умолчанию 24) - то есть кэш по сути
обнуляется сам раз в сутки, без отдельного таймера на "полночь": просто
протухшие записи перестают считаться виденными и подчищаются из файла при
следующем обращении.

Переживает перезапуск бота (в отличие от простого in-memory set) -
именно поэтому это отдельный файл, а не поле в state.py.
"""
import asyncio
import json
import os
from datetime import datetime, timedelta, timezone

from config import PROMO_SEEN_CODES_FILE, PROMO_CODE_EXPIRE_HOURS

# Один лок на все операции с файлом - защищает от одновременной записи,
# если канал и чат находят коды почти в одно и то же время.
_lock = asyncio.Lock()


def _load():
    if not os.path.exists(PROMO_SEEN_CODES_FILE):
        return {}
    try:
        with open(PROMO_SEEN_CODES_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def _save(data):
    try:
        with open(PROMO_SEEN_CODES_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)
    except Exception as e:
        print(f"⚠️ [promo-cache] Не удалось сохранить {PROMO_SEEN_CODES_FILE}: {e}")


def _prune(data):
    cutoff = datetime.now(timezone.utc) - timedelta(hours=PROMO_CODE_EXPIRE_HOURS)
    pruned = {}
    for code, ts_str in data.items():
        try:
            ts = datetime.fromisoformat(ts_str)
        except Exception:
            continue
        if ts >= cutoff:
            pruned[code] = ts_str
    return pruned


async def is_seen(code: str) -> bool:
    """Проверяет, видели ли уже этот код (протухшие записи не считаются)."""
    key = code.strip().upper()
    async with _lock:
        data = _prune(_load())
        _save(data)  # заодно подчищаем файл от протухших записей
        return key in data


async def mark_seen(code: str):
    """Отмечает код как обработанный (с текущим временем)."""
    key = code.strip().upper()
    async with _lock:
        data = _prune(_load())
        data[key] = datetime.now(timezone.utc).isoformat()
        _save(data)
