"""Школьный бот-подслушка на pyTelegramBotAPI и SQLite (Python 3.10+)."""
import os
import sqlite3
import threading
from contextlib import contextmanager
from typing import Optional
from urllib.parse import urlparse

import telebot
from telebot import types

TOKEN = os.getenv("BOT_TOKEN", "").strip()
OWNER_ID = int(os.getenv("OWNER_ID", "0"))
ADMIN_IDS = {int(x) for x in os.getenv("ADMIN_IDS", "").replace(" ", "").split(",") if x.isdigit()}
if OWNER_ID:
    ADMIN_IDS.add(OWNER_ID)
CHANNEL_ID = os.getenv("CHANNEL_ID", "").strip()  # например @school_confessions
DB_PATH = os.getenv("DB_PATH", "podslushka.sqlite3")
if not TOKEN:
    raise SystemExit("Укажите BOT_TOKEN в переменных окружения")
bot = telebot.TeleBot(TOKEN, parse_mode="HTML")
lock = threading.RLock()

@contextmanager
def db():
    with lock:
        conn = sqlite3.connect(DB_PATH, timeout=15)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys = ON")
        try:
            yield conn
            conn.commit()
        except Exception:
            conn.rollback()
            raise
        finally:
            conn.close()

def init_db():
    with db() as c:
        c.executescript("""
        CREATE TABLE IF NOT EXISTS submissions (
          id INTEGER PRIMARY KEY AUTOINCREMENT, user_id INTEGER NOT NULL,
          username TEXT, text TEXT NOT NULL, status TEXT NOT NULL DEFAULT 'pending',
          created_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
        );
        CREATE TABLE IF NOT EXISTS parallels (
          id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT NOT NULL UNIQUE,
          url TEXT NOT NULL
        );
        CREATE TABLE IF NOT EXISTS states (
          user_id INTEGER PRIMARY KEY, state TEXT NOT NULL, data TEXT NOT NULL DEFAULT ''
        );
        """)

def is_admin(uid: int) -> bool:
    return uid in ADMIN_IDS

def is_owner(uid: int) -> bool:
    return bool(OWNER_ID) and uid == OWNER_ID

def set_state(uid: int, state: str, data: str = ""):
    with db() as c:
        c.execute("INSERT INTO states(user_id,state,data) VALUES(?,?,?) ON CONFLICT(user_id) DO UPDATE SET state=excluded.state,data=excluded.data", (uid, state, data))

def get_state(uid: int):
    with db() as c:
        row = c.execute("SELECT state,data FROM states WHERE user_id=?", (uid,)).fetchone()
        return (row["state"], row["data"]) if row else ("", "")

def clear_state(uid: int):
    with db() as c:
        c.execute("DELETE FROM states WHERE user_id=?", (uid,))

def main_keyboard(uid: int):
    kb = types.ReplyKeyboardMarkup(resize_keyboard=True)
    kb.row("📝 Предложить публикацию", "📩 Написать админу")
    kb.row("🎓 Параллели")
    if is_admin(uid):
        kb.row("🛡 Проверка заявок")
    if is_owner(uid):
        kb.row("⚙️ Управление параллелями")
    return kb

def send_home(chat_id: int, uid: int, text="Привет! Это школьная подслушка. Выбери, что хочешь сделать."):
    bot.send_message(chat_id, text, reply_markup=main_keyboard(uid))

def safe_notify_admins(text: str, markup=None):
    for admin_id in ADMIN_IDS:
        try:
            bot.send_message(admin_id, text, reply_markup=markup)
        except Exception as exc:
            print("Не удалось отправить администратору", admin_id, exc)

def application_markup(submission_id: int):
    kb = types.InlineKeyboardMarkup()
    kb.row(types.InlineKeyboardButton("✅ Опубликовать", callback_data=f"approve:{submission_id}"),
           types.InlineKeyboardButton("❌ Отклонить", callback_data=f"reject:{submission_id}"))
    return kb

def parallel_keyboard():
    kb = types.InlineKeyboardMarkup()
    with db() as c:
        rows = c.execute("SELECT id,name,url FROM parallels ORDER BY id").fetchall()
    for row in rows:
        kb.add(types.InlineKeyboardButton(row["name"], url=row["url"]))
    return kb

@bot.message_handler(commands=["start", "help"])
def start(message):
    clear_state(message.from_user.id)
    send_home(message.chat.id, message.from_user.id)

@bot.message_handler(commands=["cancel"])
def cancel(message):
    clear_state(message.from_user.id)
    send_home(message.chat.id, message.from_user.id, "Действие отменено.")

@bot.message_handler(commands=["id"])
def show_id(message):
    bot.reply_to(message, f"Ваш Telegram ID: <code>{message.from_user.id}</code>")

@bot.message_handler(func=lambda m: m.text == "🎓 Параллели")
def parallels(message):
    kb = parallel_keyboard()
    if not kb.keyboard:
        bot.send_message(message.chat.id, "Ссылки на группы пока не добавлены.", reply_markup=main_keyboard(message.from_user.id))
    else:
        bot.send_message(message.chat.id, "Выбери свою параллель:", reply_markup=kb)

@bot.message_handler(func=lambda m: m.text == "📝 Предложить публикацию")
def ask_submission(message):
    set_state(message.from_user.id, "submission")
    bot.send_message(message.chat.id, "Напиши текст для публикации. Заявка появится в канале только после проверки администраторами. Можно прислать текст одним сообщением. /cancel — отмена")

@bot.message_handler(func=lambda m: m.text == "📩 Написать админу")
def ask_admin(message):
    set_state(message.from_user.id, "admin_message")
    bot.send_message(message.chat.id, "Напиши личное сообщение для администратора. Его увидят только администраторы. /cancel — отмена")

@bot.message_handler(func=lambda m: m.text == "🛡 Проверка заявок")
def review_queue(message):
    if not is_admin(message.from_user.id):
        return
    with db() as c:
        rows = c.execute("SELECT * FROM submissions WHERE status='pending' ORDER BY id LIMIT 20").fetchall()
    if not rows:
        bot.send_message(message.chat.id, "Новых заявок нет.")
        return
    bot.send_message(message.chat.id, f"Заявок на проверку: {len(rows)} (показаны первые 20).")
    for row in rows:
        who = f"@{row['username']}" if row["username"] else f"ID {row['user_id']}"
        bot.send_message(message.chat.id, f"<b>Заявка #{row['id']}</b> · {who}\n\n{row['text']}", reply_markup=application_markup(row["id"]))

@bot.message_handler(func=lambda m: m.text == "⚙️ Управление параллелями")
def manage_parallels(message):
    if not is_owner(message.from_user.id):
        return
    kb = types.InlineKeyboardMarkup()
    kb.add(types.InlineKeyboardButton("➕ Добавить параллель", callback_data="parallel:add"))
    with db() as c:
        rows = c.execute("SELECT id,name FROM parallels ORDER BY id").fetchall()
    for row in rows:
        kb.row(types.InlineKeyboardButton(f"✏️ {row['name']}", callback_data=f"parallel:edit:{row['id']}"),
               types.InlineKeyboardButton("🗑", callback_data=f"parallel:delete:{row['id']}"))
    bot.send_message(message.chat.id, "Управление кнопками параллелей. Нажми на название, чтобы изменить его или ссылку.", reply_markup=kb)

@bot.callback_query_handler(func=lambda call: call.data.startswith("approve:") or call.data.startswith("reject:"))
def review_action(call):
    if not is_admin(call.from_user.id):
        bot.answer_callback_query(call.id, "Нет доступа", show_alert=True)
        return
    action, raw_id = call.data.split(":", 1)
    sid = int(raw_id)
    with db() as c:
        row = c.execute("SELECT * FROM submissions WHERE id=?", (sid,)).fetchone()
        if not row or row["status"] != "pending":
            bot.answer_callback_query(call.id, "Заявка уже обработана или не найдена", show_alert=True)
            return
        if action == "approve":
            try:
                if not CHANNEL_ID:
                    bot.answer_callback_query(call.id, "Не настроен CHANNEL_ID", show_alert=True)
                    return
                bot.send_message(CHANNEL_ID, f"📣 <b>Подслушка</b>\n\n{row['text']}")
            except Exception as exc:
                print("Ошибка публикации:", exc)
                bot.answer_callback_query(call.id, "Не удалось опубликовать. Проверьте CHANNEL_ID и права бота.", show_alert=True)
                return
            c.execute("UPDATE submissions SET status='approved' WHERE id=?", (sid,))
            result = "опубликована"
        else:
            c.execute("UPDATE submissions SET status='rejected' WHERE id=?", (sid,))
            result = "отклонена"
    try:
        bot.send_message(row["user_id"], f"Твоя заявка #{sid} {result}.")
    except Exception:
        pass
    try:
        bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
    except Exception:
        pass
    bot.answer_callback_query(call.id, f"Заявка {result}")

@bot.callback_query_handler(func=lambda call: call.data.startswith("parallel:"))
def parallel_action(call):
    if not is_owner(call.from_user.id):
        bot.answer_callback_query(call.id, "Только главный администратор может управлять параллелями", show_alert=True)
        return
    bits = call.data.split(":")
    action = bits[1]
    if action == "add":
        set_state(call.from_user.id, "parallel_name")
        bot.send_message(call.message.chat.id, "Отправь название новой параллели (например, 9-е классы). /cancel — отмена")
    elif action == "edit":
        set_state(call.from_user.id, "parallel_edit_name", bits[2])
        bot.send_message(call.message.chat.id, "Отправь новое название параллели. /cancel — отмена")
    elif action == "delete":
        with db() as c:
            c.execute("DELETE FROM parallels WHERE id=?", (int(bits[2]),))
        bot.answer_callback_query(call.id, "Параллель удалена")
        try:
            bot.edit_message_reply_markup(call.message.chat.id, call.message.message_id, reply_markup=None)
        except Exception:
            pass
    bot.answer_callback_query(call.id)

def valid_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme == "https" and parsed.netloc in {"t.me", "www.t.me", "telegram.me", "www.telegram.me"}

@bot.message_handler(content_types=["text", "photo", "video", "document", "audio", "voice", "animation", "sticker"])
def handle_content(message):
    uid = message.from_user.id
    state, data = get_state(uid)
    if not state:
        if message.content_type != "text":
            bot.reply_to(message, "Используй кнопки меню, чтобы отправить заявку или написать админу.")
        else:
            send_home(message.chat.id, uid, "Не понял команду. Выбери действие в меню.")
        return
    if message.content_type != "text":
        bot.send_message(message.chat.id, "Пока принимаются только текстовые сообщения. Отправь текст или /cancel.")
        return
    text = message.text.strip()
    if not text:
        bot.send_message(message.chat.id, "Сообщение пустое. Напиши текст или /cancel.")
        return
    if state == "submission":
        if len(text) > 3500:
            bot.send_message(message.chat.id, "Слишком длинно: максимум 3500 символов. Сократи текст и отправь снова.")
            return
        with db() as c:
            cur = c.execute("INSERT INTO submissions(user_id,username,text) VALUES(?,?,?)", (uid, message.from_user.username, text))
            sid = cur.lastrowid
        clear_state(uid)
        safe_notify_admins(f"<b>Новая заявка #{sid}</b>\nОт: {('@' + message.from_user.username) if message.from_user.username else 'ID ' + str(uid)}\n\n{text}", application_markup(sid))
        bot.send_message(message.chat.id, f"Заявка #{sid} отправлена на проверку. Спасибо!", reply_markup=main_keyboard(uid))
    elif state == "admin_message":
        clear_state(uid)
        username = "@" + message.from_user.username if message.from_user.username else "без username"
        safe_notify_admins(f"<b>Личное сообщение админу</b>\nОт: {username} (ID <code>{uid}</code>)\n\n{text}")
        bot.send_message(message.chat.id, "Сообщение передано администраторам.", reply_markup=main_keyboard(uid))
    elif state == "parallel_name":
        set_state(uid, "parallel_url", text[:80])
        bot.send_message(message.chat.id, "Теперь отправь ссылку Telegram на группу (https://t.me/...).")
    elif state == "parallel_url":
        if not valid_url(text):
            bot.send_message(message.chat.id, "Нужна ссылка вида https://t.me/имя_группы. Попробуй ещё раз или /cancel.")
            return
        _, name = get_state(uid)
        try:
            with db() as c:
                c.execute("INSERT INTO parallels(name,url) VALUES(?,?)", (name, text))
            clear_state(uid)
            bot.send_message(message.chat.id, f"Параллель «{name}» добавлена.", reply_markup=main_keyboard(uid))
        except sqlite3.IntegrityError:
            bot.send_message(message.chat.id, "Такое название уже есть. Отправь другое название или /cancel.")
            set_state(uid, "parallel_name")
    elif state == "parallel_edit_name":
        with db() as c:
            exists = c.execute("SELECT 1 FROM parallels WHERE id=?", (int(data),)).fetchone()
        if not exists:
            clear_state(uid)
            bot.send_message(message.chat.id, "Параллель уже удалена.")
            return
        set_state(uid, "parallel_edit_url", f"{data}|{text[:80]}")
        bot.send_message(message.chat.id, "Отправь новую ссылку https://t.me/...")
    elif state == "parallel_edit_url":
        if not valid_url(text):
            bot.send_message(message.chat.id, "Нужна ссылка вида https://t.me/имя_группы. Попробуй ещё раз или /cancel.")
            return
        try:
            pid, name = data.split("|", 1)
            with db() as c:
                c.execute("UPDATE parallels SET name=?,url=? WHERE id=?", (name, text, int(pid)))
            clear_state(uid)
            bot.send_message(message.chat.id, "Параллель обновлена.", reply_markup=main_keyboard(uid))
        except sqlite3.IntegrityError:
            bot.send_message(message.chat.id, "Такое название уже используется. Отправь другое название.")
            set_state(uid, "parallel_edit_name", pid)

if __name__ == "__main__":
    init_db()
    print("Бот запущен")
    bot.infinity_polling(skip_pending=True, timeout=30, long_polling_timeout=30)
