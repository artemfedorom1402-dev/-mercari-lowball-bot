"""
Бот-парсер для отслеживания "лоуболлов" (заниженных цен) на mercari.jp.

Как это работает:
1. В Telegram открываешь меню (/start) и жмёшь кнопки — команды печатать не нужно.
2. Раз в POLL_INTERVAL_SECONDS бот опрашивает Mercari через mercapi
   (неофициальный, но рабочий враппер над внутренним API mercari.jp)
   по каждому активному поиску.
3. Новые товары дешевле порога, которых мы ещё не видели, сохраняются
   в SQLite и присылаются тебе в Telegram.

Управление полностью кнопочное:
  ➕ Добавить поиск   — бот пошагово спросит название и цену
  📋 Список           — все поиски, у каждого свои кнопки ⏸/▶️/🗑
  📊 Статус           — статистика бота
  ❓ Помощь           — это меню

Команды (/add, /list...) оставлены как запасной вариант, но не обязательны.

Файлы проекта:
- bot.py         — этот файл, вся логика
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
    ConversationHandler,
    MessageHandler,
    filters,
)

# --- Базовая настройка -------------------------------------------------

BASE_DIR = Path(__file__).parent
load_dotenv(BASE_DIR / ".env")

TELEGRAM_TOKEN = os.getenv("TELEGRAM_BOT_TOKEN")
OWNER_CHAT_ID = int(os.getenv("TELEGRAM_CHAT_ID", "0"))
POLL_INTERVAL_SECONDS = int(os.getenv("POLL_INTERVAL_SECONDS", "60"))
DB_PATH = BASE_DIR / "bot.db"

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

# Состояния диалога добавления поиска
ASK_NAME, ASK_PRICE = range(2)


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
    conn.commit()
    conn.close()


def item_url(item) -> str:
    return f"https://jp.mercari.com/item/{item.id_}"


def esc(text) -> str:
    """Экранирует спецсимволы, чтобы Telegram не ломался на parse_mode=HTML."""
    return html.escape(str(text))


# --- Проверка доступа: командами пользуется только владелец бота --------

def is_owner(update: Update) -> bool:
    return update.effective_chat is not None and update.effective_chat.id == OWNER_CHAT_ID


# --- Главное меню (инлайн-кнопки в сообщениях) --------------------------

def main_menu_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить поиск", callback_data="menu:add")],
            [InlineKeyboardButton("📋 Список поисков", callback_data="menu:list")],
            [InlineKeyboardButton("📊 Статус", callback_data="menu:status")],
            [InlineKeyboardButton("❓ Помощь", callback_data="menu:help")],
        ]
    )


def cancel_kb() -> InlineKeyboardMarkup:
    return InlineKeyboardMarkup([[InlineKeyboardButton("✖️ Отмена", callback_data="add:cancel")]])


MENU_TEXT = (
    "🛍 <b>Mercari Lowball Bot</b>\n"
    "Слежу за メルカリ и присылаю новые лоты дешевле заданной цены.\n"
    "───────────────\n"
    "Всё управление — кнопками ниже 👇"
)


async def send_main_menu(update: Update) -> None:
    if update.callback_query:
        await update.callback_query.edit_message_text(
            MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_kb()
        )
    else:
        await update.message.reply_text(
            MENU_TEXT, parse_mode="HTML", reply_markup=main_menu_kb()
        )


# --- Команды Telegram ------------------------------------------------------

async def cmd_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        await update.message.reply_text(
            f"🔒 Этот бот приватный.\n"
            f"Впиши TELEGRAM_CHAT_ID={update.effective_chat.id} в .env, чтобы стать владельцем."
        )
        return
    await send_main_menu(update)


async def cmd_help(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await send_main_menu(update)


def format_watch_list(rows) -> str:
    if not rows:
        return "📭 Активных поисков пока нет. Жми «➕ Добавить поиск»."
    lines = ["📋 <b>Все поиски</b>", "───────────────"]
    for row in rows:
        status = "✅" if row["enabled"] else "⏸"
        lines.append(f"{status} #{row['id']} — «{esc(row['keyword'])}» до {row['max_price']} ¥")
    return "\n".join(lines)


def watch_list_kb(rows) -> InlineKeyboardMarkup:
    buttons = []
    for row in rows:
        if row["enabled"]:
            toggle = InlineKeyboardButton("⏸ Пауза", callback_data=f"watch:pause:{row['id']}")
        else:
            toggle = InlineKeyboardButton("▶️ Возобновить", callback_data=f"watch:resume:{row['id']}")
        remove = InlineKeyboardButton("🗑 Удалить", callback_data=f"watch:remove:{row['id']}")
        label = InlineKeyboardButton(f"#{row['id']} «{row['keyword'][:20]}»", callback_data="noop")
        buttons.append([label])
        buttons.append([toggle, remove])
    buttons.append([InlineKeyboardButton("➕ Добавить поиск", callback_data="menu:add")])
    buttons.append([InlineKeyboardButton("⬅️ Меню", callback_data="menu:main")])
    return InlineKeyboardMarkup(buttons)


async def show_watch_list(update: Update) -> None:
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, keyword, max_price, enabled FROM watches ORDER BY id"
    ).fetchall()
    conn.close()

    text = format_watch_list(rows)
    kb = watch_list_kb(rows)
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cmd_list(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await show_watch_list(update)


async def show_status(update: Update) -> None:
    conn = get_conn()
    active = conn.execute("SELECT COUNT(*) FROM watches WHERE enabled = 1").fetchone()[0]
    paused = conn.execute("SELECT COUNT(*) FROM watches WHERE enabled = 0").fetchone()[0]
    found_total = conn.execute("SELECT COUNT(*) FROM seen_items").fetchone()[0]
    conn.close()

    uptime = datetime.now(timezone.utc) - START_TIME
    hours, rem = divmod(int(uptime.total_seconds()), 3600)
    minutes = rem // 60

    text = (
        "📊 <b>Статус бота</b>\n"
        "───────────────\n"
        f"⏱ Работает: {hours}ч {minutes}м\n"
        f"🔁 Опрос каждые {POLL_INTERVAL_SECONDS} сек\n"
        f"✅ Активных поисков: {active}\n"
        f"⏸ На паузе: {paused}\n"
        f"🎯 Всего найдено за всё время: {found_total}"
    )
    kb = InlineKeyboardMarkup([[InlineKeyboardButton("⬅️ Меню", callback_data="menu:main")]])
    if update.callback_query:
        await update.callback_query.edit_message_text(text, parse_mode="HTML", reply_markup=kb)
    else:
        await update.message.reply_text(text, parse_mode="HTML", reply_markup=kb)


async def cmd_status(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    if not is_owner(update):
        return
    await show_status(update)


# --- Диалог добавления поиска (пошагово, через кнопку) --------------------

async def add_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    if not is_owner(update):
        return ConversationHandler.END

    query = update.callback_query
    if query:
        await query.answer()
        await query.edit_message_text(
            "✏️ Напиши название товара, которое искать (например: <i>iPhone 12 Pro</i>).",
            parse_mode="HTML",
            reply_markup=cancel_kb(),
        )
    else:
        await update.message.reply_text(
            "✏️ Напиши название товара, которое искать (например: <i>iPhone 12 Pro</i>).",
            parse_mode="HTML",
            reply_markup=cancel_kb(),
        )
    return ASK_NAME


async def add_got_name(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    keyword = update.message.text.strip()
    if not keyword:
        await update.message.reply_text("⚠️ Название не может быть пустым. Напиши ещё раз.", reply_markup=cancel_kb())
        return ASK_NAME

    context.user_data["new_watch_keyword"] = keyword
    await update.message.reply_text(
        f"💴 Теперь напиши максимальную цену в йенах для «{esc(keyword)}» (только число, например: <i>30000</i>).",
        parse_mode="HTML",
        reply_markup=cancel_kb(),
    )
    return ASK_PRICE


async def add_got_price(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    price_token = update.message.text.strip().replace(",", "").replace(" ", "")
    if not price_token.isdigit() or int(price_token) <= 0:
        await update.message.reply_text(
            "⚠️ Нужно просто число больше нуля, например: 30000. Попробуй ещё раз.",
            reply_markup=cancel_kb(),
        )
        return ASK_PRICE

    max_price = int(price_token)
    keyword = context.user_data.pop("new_watch_keyword", None)
    if not keyword:
        await update.message.reply_text("⚠️ Что-то пошло не так, начни заново через меню.")
        return ConversationHandler.END

    conn = get_conn()
    cur = conn.execute(
        "INSERT INTO watches (keyword, max_price) VALUES (?, ?)",
        (keyword, max_price),
    )
    watch_id = cur.lastrowid
    conn.commit()
    conn.close()

    kb = InlineKeyboardMarkup(
        [
            [InlineKeyboardButton("➕ Добавить ещё", callback_data="menu:add")],
            [InlineKeyboardButton("📋 Список поисков", callback_data="menu:list")],
        ]
    )
    await update.message.reply_text(
        f"✅ Добавил поиск <b>#{watch_id}</b>: «{esc(keyword)}» дешевле {max_price} ¥.\n"
        f"⏱ Проверяю каждые {POLL_INTERVAL_SECONDS} сек.",
        parse_mode="HTML",
        reply_markup=kb,
    )
    return ConversationHandler.END


async def add_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    context.user_data.pop("new_watch_keyword", None)
    query = update.callback_query
    await query.answer("Отменено")
    await send_main_menu(update)
    return ConversationHandler.END


# --- Обработка нажатий на кнопки меню и списка -----------------------------

async def on_menu_click(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    if not is_owner(update):
        await query.answer()
        return

    data = query.data
    if data == "menu:main":
        await query.answer()
        await send_main_menu(update)
    elif data == "menu:list":
        await query.answer()
        await show_watch_list(update)
    elif data == "menu:status":
        await query.answer()
        await show_status(update)
    elif data == "menu:help":
        await query.answer()
        await send_main_menu(update)
    elif data == "noop":
        await query.answer()
    elif data.startswith("watch:"):
        await on_watch_action(update, context)
    else:
        await query.answer()


async def on_watch_action(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    query = update.callback_query
    _, action, watch_id_str = query.data.split(":")
    watch_id = int(watch_id_str)

    conn = get_conn()
    if action == "pause":
        cur = conn.execute("UPDATE watches SET enabled = 0 WHERE id = ?", (watch_id,))
        conn.commit()
        toast = f"⏸ Поиск #{watch_id} на паузе" if cur.rowcount else f"❌ Поиск #{watch_id} не найден"
    elif action == "resume":
        cur = conn.execute("UPDATE watches SET enabled = 1 WHERE id = ?", (watch_id,))
        conn.commit()
        toast = f"▶️ Поиск #{watch_id} возобновлён" if cur.rowcount else f"❌ Поиск #{watch_id} не найден"
    elif action == "remove":
        cur = conn.execute("DELETE FROM watches WHERE id = ?", (watch_id,))
        conn.commit()
        toast = f"🗑 Поиск #{watch_id} удалён" if cur.rowcount else f"❌ Поиск #{watch_id} не найден"
    else:
        toast = None
    conn.close()

    await query.answer(toast)
    await show_watch_list(update)


# --- Фоновая проверка Mercari ----------------------------------------------

async def check_watch(context: ContextTypes.DEFAULT_TYPE, watch: sqlite3.Row) -> None:
    results = None
    last_error = None
    for attempt in range(3):
        try:
            results = await mercapi_client.search(
                watch["keyword"],
                price_max=watch["max_price"],
                sort_by=SearchRequestData.SortBy.SORT_CREATED_TIME,
                sort_order=SearchRequestData.SortOrder.ORDER_DESC,
                status=[SearchRequestData.Status.STATUS_ON_SALE],
            )
            break
        except Exception as e:
            last_error = e
            log.warning(
                "Попытка %d/3 для поиска #%s (%s) не удалась: %s",
                attempt + 1, watch["id"], watch["keyword"], e,
            )
            await asyncio.sleep(5)

    if results is None:
        raise RuntimeError(f"Mercari недоступен после 3 попыток: {last_error}")

    conn = get_conn()
    new_count = 0
    for item in results.items:
        already = conn.execute(
            "SELECT 1 FROM seen_items WHERE item_id = ? AND watch_id = ?",
            (item.id_, watch["id"]),
        ).fetchone()
        if already:
            continue

        url = item_url(item)
        text = (
            f"🔥 <b>{esc(watch['keyword'])}</b>\n"
            f"{esc(item.name)}\n"
            f"💴 {item.price} ¥"
        )
        keyboard = InlineKeyboardMarkup(
            [[InlineKeyboardButton("🔗 Открыть на Mercari", url=url)]]
        )

        try:
            await context.bot.send_message(
                chat_id=OWNER_CHAT_ID,
                text=text,
                parse_mode="HTML",
                reply_markup=keyboard,
            )
        except Exception:
            log.exception("Не удалось отправить находку %s — попробую снова в следующий раз", item.id_)
            continue  # не отмечаем как увиденное, чтобы попробовать отправить позже

        conn.execute(
            "INSERT OR IGNORE INTO seen_items (item_id, watch_id, name, price, url) VALUES (?, ?, ?, ?, ?)",
            (item.id_, watch["id"], item.name, item.price, url),
        )
        conn.commit()
        new_count += 1

    conn.close()
    log.info("[#%s %s] проверено, новых находок: %d", watch["id"], watch["keyword"], new_count)


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
            log.exception("Ошибка при проверке поиска #%s (%s)", watch["id"], watch["keyword"])


# --- Точка входа -------------------------------------------------------

async def post_init(application: Application) -> None:
    """Настраивает меню команд бота — то самое 'дизайн', кнопка / в Telegram."""
    await application.bot.set_my_commands(
        [
            BotCommand("start", "открыть меню бота"),
            BotCommand("help", "открыть меню бота"),
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

    add_conv = ConversationHandler(
        entry_points=[
            CallbackQueryHandler(add_start, pattern="^menu:add$"),
            CommandHandler("add", add_start),
        ],
        states={
            ASK_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_name),
                CallbackQueryHandler(add_cancel, pattern="^add:cancel$"),
            ],
            ASK_PRICE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, add_got_price),
                CallbackQueryHandler(add_cancel, pattern="^add:cancel$"),
            ],
        },
        fallbacks=[CallbackQueryHandler(add_cancel, pattern="^add:cancel$")],
        per_message=False,
    )

    application.add_handler(add_conv)
    application.add_handler(CommandHandler("start", cmd_start))
    application.add_handler(CommandHandler("help", cmd_help))
    application.add_handler(CommandHandler("list", cmd_list))
    application.add_handler(CommandHandler("status", cmd_status))
    application.add_handler(CallbackQueryHandler(on_menu_click))

    if application.job_queue is None:
        raise SystemExit(
            "JobQueue не установлен. Выполни: pip install \"python-telegram-bot[job-queue]\""
        )
    application.job_queue.run_repeating(poll_job, interval=POLL_INTERVAL_SECONDS, first=10)

    log.info("Бот запущен, опрос каждые %d сек", POLL_INTERVAL_SECONDS)
    application.run_polling()


if __name__ == "__main__":
    main()
