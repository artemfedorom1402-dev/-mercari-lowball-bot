"""
Бот-парсер для отслеживания "лоуболлов" (заниженных цен) на Mercari (jp.mercari.com)
и Rakuma (fril.jp) — двух крупнейших японских барахолках.

Как это работает:
1. В Telegram командой /add (Mercari) или /add_rakuma (Rakuma) ты добавляешь
   то, что ищешь: название + макс. цена.
2. Раз в POLL_INTERVAL_SECONDS бот опрашивает соответствующую площадку по
   каждому активному поиску: Mercari — через mercapi, Rakuma — через
   собственный лёгкий парсер (rakuma.py).
3. Новые товары дешевле порога, которых мы ещё не видели, сохраняются
   в SQLite и присылаются тебе в Telegram.

Команды бота:
  /add <название> <макс_цена>          — добавить поиск на Mercari
  /add_rakuma <название> <макс_цена>   — добавить поиск на Rakuma
  /list                                 — поиски, с вкладками Mercari/Rakuma
  /pause <id>                           — приостановить поиск
  /resume <id>                          — возобновить поиск
  /remove <id>                          — удалить поиск
  /status                               — статус и статистика бота
  /help                                 — подсказка

Файлы проекта:
- bot.py         — этот файл, вся логика
- rakuma.py       — парсер поиска Rakuma
- .env           — токен бота и chat_id (секреты, .env.example — пример)
- bot.db         — создаётся автоматически: поиски + уже отправленные товары
"""

import asyncio
import html
import logging
from logging.handlers import RotatingFileHandler
import os
import sqlite3
from datetime import datetime, timezone
from pathlib import Path

from dotenv import load_dotenv
from mercapi import Mercapi
from mercapi.requests import SearchRequestData
from telegram import (
    BotCommand,
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
)
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
)

import rakuma

# --- Базовая настройка -------------------------------------------------

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")


def _int_env(name: str, default: int) -> int:
    """Читает целочисленную переменную окружения, безопасно откатываясь на default,
    если переменная не задана или Railway передал пустую строку."""
    raw = os.getenv(name, "").strip()
    if not raw or not raw.lstrip("-").isdigit():
        return default
    return int(raw)


OWNER_CHAT_ID = _int_env("TELEGRAM_CHAT_ID", 0)
POLL_INTERVAL_SECONDS = _int_env("POLL_INTERVAL_SECONDS", 60)
DB_PATH = BASE_DIR / "bot.db"

PLATFORMS = ("mercari", "rakuma")
PLATFORM_ICON = {"mercari": "🛍", "rakuma": "🐟"}
PLATFORM_NAME = {"mercari": "Mercari", "rakuma": "Rakuma"}

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    handlers=[
        logging.StreamHandler(),
        RotatingFileHandler(BASE_DIR / "bot.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8"),
    ],
)
log = logging.getLogger("mercari-bot")

mercapi_client = Mercapi()  # создаём один раз на весь рантайм — так рекомендует сама библиотека
START_TIME = datetime.now(timezone.utc)


# --- База данных ---------------------------------------------------------

def get_conn() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    conn = get_conn()
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS watches (
            id         INTEGER PRIMARY KEY AUTOINCREMENT,
            platform   TEXT NOT NULL DEFAULT 'mercari',
            keyword    TEXT NOT NULL,
            max_price  INTEGER NOT NULL,
            enabled    INTEGER NOT NULL DEFAULT 1,
            created_at TEXT DEFAULT CURRENT_TIMESTAMP
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS seen_items (
            item_id  TEXT NOT NULL,
            watch_id INTEGER NOT NULL,
            name     TEXT,
            price    INTEGER,
            url      TEXT,
            found_at TEXT DEFAULT CURRENT_TIMESTAMP,
            PRIMARY KEY (item_id, watch_id)
        )
        """
    )
    # Миграция для баз, созданных до появления мульти-платформенности
    try:
        conn.execute("ALTER TABLE watches ADD COLUMN platform TEXT NOT NULL DEFAULT 'mercari'")
        conn.commit()
    except sqlite3.OperationalError:
        pass  # колонка уже есть
    conn.commit()
    conn.close()


def esc(text) -> str:
    """Экранирует спецсимволы, чтобы Telegram не ломался на parse_mode=HTML."""
    return html.escape(str(text))


# --- Проверка доступа: командами пользуется только владелец бота --------

def is_owner(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == OWNER_CHAT_ID


# --- Команды Telegram ------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text(
            f"🔒 Этот бот приватный.\n"
            f"Впиши TELEGRAM_CHAT_ID={update.effective_chat.id} в .env, чтобы стать владельцем."
        )
        return
    await update.message.reply_text(
        "🛍🐟 <b>Lowball Bot</b> — Mercari + Rakuma\n"
        "Слежу за メルカリ и ラクマ и присылаю новые лоты дешевле заданной цены.\n"
        "───────────────\n"
        "🗂 <code>/catalog</code> — каталог площадок\n"
        "➕ <code>/add название цена</code> — поиск на Mercari\n"
        "🐟 <code>/add_rakuma название цена</code> — поиск на Rakuma\n"
        "📋 <code>/list</code> — поиски (вкладки Mercari/Rakuma)\n"
        "⏸ <code>/pause id</code> — приостановить поиск\n"
        "▶️ <code>/resume id</code> — возобновить поиск\n"
        "🗑 <code>/remove id</code> — удалить поиск\n"
        "📊 <code>/status</code> — статус и статистика бота\n"
        "❓ <code>/help</code> — эта подсказка",
        parse_mode="HTML",
    )


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await cmd_start(update, context)


async def _add_watch(update: Update, context: ContextTypes.DEFAULT_TYPE, platform: str) -> None:
    if not is_owner(update):
        return

    args = context.args
    if len(args) < 2:
        icon = PLATFORM_ICON[platform]
        cmd = "add" if platform == "mercari" else "add_rakuma"
        await update.message.reply_text(
            f"⚠️ Формат: /{cmd} <название> <макс_цена>\n"
            f"Например: /{cmd} iPhone 12 Pro 30000 {icon}"
        )
        return

    price_token = args[-1].replace(",", "").replace(" ", "")
    if not price_token.isdigit():
        await update.message.reply_text(
            "⚠️ Последним аргументом должна быть цена (число), например: /add iPhone 12 Pro 30000"
        )
        return

    max_price = int(price_token)
    if max_price <= 0:
        await update.message.reply_text("⚠️ Цена должна быть больше нуля.")
        return

    keyword = " ".join(args[:-1]).strip()
    if not keyword:
        await update.message.reply_text("⚠️ Не указано название товара.")
        return

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO watches (platform, keyword, max_price) VALUES (?, ?, ?)",
        (platform, keyword, max_price),
    )
    watch_id = cur.lastrowid
    conn.commit()
    conn.close()

    icon = PLATFORM_ICON[platform]
    name = PLATFORM_NAME[platform]
    await update.message.reply_text(
        f"✅ {icon} Добавил поиск <b>#{watch_id}</b> на {name}: «{esc(keyword)}» дешевле {max_price} ¥.\n"
        f"⏱ Проверяю каждые {POLL_INTERVAL_SECONDS} сек.",
        parse_mode="HTML",
    )


async def cmd_add(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _add_watch(update, context, platform="mercari")


async def cmd_add_rakuma(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _add_watch(update, context, platform="rakuma")


def _list_text_and_keyboard(platform: str) -> tuple[str, InlineKeyboardMarkup]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, keyword, max_price, enabled FROM watches WHERE platform = ? ORDER BY id",
        (platform,),
    ).fetchall()
    conn.close()

    icon = PLATFORM_ICON[platform]
    name = PLATFORM_NAME[platform]

    lines = [f"{icon} <b>{name} — поиски</b>", "───────────────"]
    if not rows:
        lines.append("📭 Пока нет поисков на этой площадке. Добавь через /add" + ("" if platform == "mercari" else "_rakuma"))
    else:
        for row in rows:
            status = "✅" if row["enabled"] else "⏸"
            lines.append(f"{status} #{row['id']} — «{esc(row['keyword'])}» до {row['max_price']} ¥")

    buttons = [
        InlineKeyboardButton(
            f"{'• ' if platform == p else ''}{PLATFORM_ICON[p]} {PLATFORM_NAME[p]}",
            callback_data=f"list:{p}",
        )
        for p in PLATFORMS
    ]
    keyboard = InlineKeyboardMarkup([buttons, [InlineKeyboardButton("🗂 Каталог", callback_data="catalog")]])
    return "\n".join(lines), keyboard


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    text, keyboard = _list_text_and_keyboard("mercari")
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


def _catalog_text_and_keyboard() -> tuple[str, InlineKeyboardMarkup]:
    conn = get_conn()
    rows = conn.execute(
        "SELECT platform, "
        "COUNT(*) AS total, "
        "SUM(CASE WHEN enabled = 1 THEN 1 ELSE 0 END) AS active "
        "FROM watches GROUP BY platform"
    ).fetchall()
    conn.close()
    counts = {row["platform"]: (row["total"], row["active"]) for row in rows}

    lines = ["🗂 <b>Каталог площадок</b>", "───────────────"]
    for p in PLATFORMS:
        total, active = counts.get(p, (0, 0))
        lines.append(f"{PLATFORM_ICON[p]} <b>{PLATFORM_NAME[p]}</b> — {active} активных из {total}")
    lines.append("")
    lines.append("Выбери площадку, чтобы посмотреть и управлять её поисками:")

    buttons = [
        InlineKeyboardButton(f"{PLATFORM_ICON[p]} {PLATFORM_NAME[p]}", callback_data=f"list:{p}")
        for p in PLATFORMS
    ]
    keyboard = InlineKeyboardMarkup([buttons])
    return "\n".join(lines), keyboard


async def cmd_catalog(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    text, keyboard = _catalog_text_and_keyboard()
    await update.message.reply_text(text, parse_mode="HTML", reply_markup=keyboard)


async def on_list_tab(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_owner(update):
        await query.answer()
        return

    platform = query.data.split(":", 1)[1]
    text, keyboard = _list_text_and_keyboard(platform)
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    await query.answer()


async def on_catalog_button(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_owner(update):
        await query.answer()
        return

    text, keyboard = _catalog_text_and_keyboard()
    await query.edit_message_text(text, parse_mode="HTML", reply_markup=keyboard)
    await query.answer()


async def cmd_remove(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return

    args = context.args
    if not args or not args[0].isdigit():
        await update.message.reply_text("⚠️ Формат: /remove <id>\nId смотри в /list")
        return

    watch_id = int(args[0])
    conn = get_conn()
    cur = conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
    conn.commit()
    conn.close()

    if cur.rowcount:
        await update.message.reply_text(f"🗑 Удалил поиск #{watch_id}.")
    else:
        await update.message.reply_text(f"❌ Поиск #{watch_id} не найден.")


async def _toggle_watch(update: Update, enabled: int, verb: str) -> None:
    if not is_owner(update):
        return

    args = update.message.text.split()[1:]
    if not args or not args[0].isdigit():
        await update.message.reply_text(f"⚠️ Формат: /{verb} <id>\nId смотри в /list")
        return

    watch_id = int(args[0])
    conn = get_conn()
    cur = conn.execute("UPDATE watches SET enabled = ? WHERE id = ?", (enabled, watch_id))
    conn.commit()
    conn.close()

    if not cur.rowcount:
        await update.message.reply_text(f"❌ Поиск #{watch_id} не найден.")
    elif enabled:
        await update.message.reply_text(f"▶️ Поиск #{watch_id} возобновлён.")
    else:
        await update.message.reply_text(f"⏸ Поиск #{watch_id} на паузе — уведомлений по нему не будет.")


async def cmd_pause(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _toggle_watch(update, enabled=0, verb="pause")


async def cmd_resume(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    await _toggle_watch(update, enabled=1, verb="resume")


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return

    conn = get_conn()
    active = conn.execute("SELECT COUNT(*) FROM watches WHERE enabled = 1").fetchone()[0]
    paused = conn.execute("SELECT COUNT(*) FROM watches WHERE enabled = 0").fetchone()[0]
    found_total = conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0]
    per_platform = conn.execute(
        "SELECT platform, COUNT(*) AS n FROM watches WHERE enabled = 1 GROUP BY platform"
    ).fetchall()
    conn.close()

    uptime = datetime.now(timezone.utc) - START_TIME
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    minutes = rem // 60

    per_platform_line = " · ".join(
        f"{PLATFORM_ICON[row['platform']]} {row['n']}" for row in per_platform
    ) or "—"

    await update.message.reply_text(
        "📊 <b>Статус бота</b>\n"
        "───────────────\n"
        f"⏱ Работает: {hours}ч {minutes}м\n"
        f"🔁 Опрос каждые {POLL_INTERVAL_SECONDS} сек\n"
        f"✅ Активных поисков: {active} ({per_platform_line})\n"
        f"⏸ На паузе: {paused}\n"
        f"🎯 Всего найдено за всё время: {found_total}",
        parse_mode="HTML",
    )


# --- Фоновая проверка площадок ----------------------------------------------

async def fetch_items(watch: sqlite3.Row) -> list[dict]:
    """Возвращает список товаров с площадки watch['platform'] с 3 попытками при сбоях."""
    last_error = None
    for attempt in range(3):
        try:
            if watch["platform"] == "rakuma":
                return await rakuma.search_rakuma(watch["keyword"])

            results = await mercapi_client.search(
                watch["keyword"],
                price_max=watch["max_price"],
                sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
                sort_order=SearchRequestData.SortOrder.ORDER_DESC,
                status=[SearchRequestData.Status.STATUS_ON_SALE],
            )
            return [
                {"id": item.id_, "name": item.name, "price": item.price,
                 "url": f"https://jp.mercari.com/item/{item.id_}"}
                for item in results.items
            ]
        except Exception as e:
            last_error = e
            log.warning(
                "Попытка %d/3 для поиска #%s (%s, %s) не удалась: %s",
                attempt + 1, watch["id"], watch["platform"], watch["keyword"], e,
            )
            await asyncio.sleep(5)

    raise RuntimeError(f"Площадка недоступна после 3 попыток: {last_error}")


async def check_watch(context: ContextTypes.DEFAULT_TYPE, watch: sqlite3.Row) -> None:
    items = await fetch_items(watch)
    platform = watch["platform"]

    conn = get_conn()
    new_count = 0
    for item in items:
        if watch["max_price"] and item["price"] > watch["max_price"]:
            continue  # для Rakuma фильтр по цене делаем на своей стороне

        already = conn.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ? AND watch_id = ?",
            (item["id"], watch["id"]),
        ).fetchone()
        if already:
            continue

        icon = PLATFORM_ICON[platform]
        text = (
            f"{icon} <b>{esc(watch['keyword'])}</b>\n"
            f"{esc(item['name'])}\n"
            f"💴 {item['price']} ¥"
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton(f"🔗 Открыть на {PLATFORM_NAME[platform]}", url=item["url"])]]
        )

        try:
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            log.exception("Не удалось отправить находку %s — попробую снова в следующий раз", item["id"])
            continue  # не отмечаем как увиденное, чтобы попробовать отправить позже

        conn.execute(
            "INSERT OR IGNORE INTO seen_items (item_id, watch_id, name, price, url) VALUES (?, ?, ?, ?, ?)",
            (item["id"], watch["id"], item["name"], item["price"], item["url"]),
        )
        conn.commit()
        new_count += 1

    conn.close()
    log.info(
        "[#%s %s %s] проверено, новых находок: %d",
        watch["id"], watch["platform"], watch["keyword"], new_count,
    )


async def poll_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    if not OWNER_CHAT_ID:
        return  # некому слать — chat_id ещё не настроен в .env

    conn = get_conn()
    watches = conn.execute("SELECT * FROM watches WHERE enabled = 1").fetchall()
    conn.close()

    for watch in watches:
        try:
            await check_watch(context, watch)
        except Exception:
            log.exception(
                "Ошибка при проверке поиска #%s (%s, %s)",
                watch["id"], watch["platform"], watch["keyword"],
            )


# --- Точка входа -------------------------------------------------------

async def post_init(application: Application) -> None:
    """Настраивает меню команд бота — то самое 'дизайн', кнопка / в Telegram."""
    await application.bot.set_my_commands(
        [
            BotCommand("catalog", "каталог площадок Mercari/Rakuma"),
            BotCommand("add", "поиск на Mercari: /add название цена"),
            BotCommand("add_rakuma", "поиск на Rakuma: /add_rakuma название цена"),
            BotCommand("list", "показать все поиски"),
            BotCommand("pause", "приостановить поиск: /pause id"),
            BotCommand("resume", "возобновить поиск: /resume id"),
            BotCommand("remove", "удалить поиск: /remove id"),
            BotCommand("status", "статус и статистика бота"),
            BotCommand("help", "помощь"),
        ]
    )


def main() -> None:
    if not TELEGRAM_TOKEN:
        raise SystemExit("Не задан TELEGRAM_BOT_TOKEN в .env")
    if not OWNER_CHAT_ID:
        log.warning(
            "TELEGRAM_CHAT_ID не задан в .env — бот запустится, но команды не будут работать. "
            "Напиши боту /start, он покажет твой chat_id, впиши его в .env и перезапусти бота."
        )

    init_db()

    application = Application.builder().token(TELEGRAM_TOKEN).post_init(post_init).build()
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("catalog", cmd_catalog))
    application.add_handler(CommandHandler("add", cmd_add))
    application.add_handler(CommandHandler("add_rakuma", cmd_add_rakuma))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("remove", cmd_remove))
    application.add_handler(CommandHandler("pause", cmd_pause))
    application.add_handler(CommandHandler("resume", cmd_resume))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CallbackQueryHandler(on_list_tab, pattern=r"^list:"))
    application.add_handler(CallbackQueryHandler(on_catalog_button, pattern=r"^catalog$"))

    if application.job_queue is None:
        raise SystemExit(
            "JobQueue не установлен. Выполни: pip install \"python-telegram-bot[job-queue]\""
        )
    application.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SECONDS, first=10)

    log.info("Бот запущен, опрос каждые %d сек", POLL_INTERVAL_SECONDS)
    application.run_polling()


if __name__ == "__main__":
    main()
