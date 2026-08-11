import os
import redis
import requests

# ---------- Окружение (из переменных) ----------
REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]

r = redis.from_url(REDIS_URL)

# ---------- Константы Redis ----------
DYNAMIC_ALLOWED_SET = "dynamic_allowed_users"
UNAUTHORIZED_NOTIFY_PREFIX = "unauthorized_notify:"

# ---------- Базовый статический список из ALLOWED_USERS ----------
ALLOWED_USERS = {ADMIN_CHAT_ID}
allowed_raw = os.environ.get("ALLOWED_USERS", "")
if allowed_raw:
    for uid in allowed_raw.split(","):
        uid = uid.strip()
        if uid:
            ALLOWED_USERS.add(int(uid))

# ---------- Функции ----------
def is_allowed(chat_id):
    """Проверяет доступ (статический список + динамический)."""
    if r.sismember(DYNAMIC_ALLOWED_SET, chat_id):
        return True
    return chat_id in ALLOWED_USERS

def add_dynamic_user(chat_id):
    """Добавляет пользователя в динамический список (до перезапуска сервисов)."""
    r.sadd(DYNAMIC_ALLOWED_SET, chat_id)

def remove_dynamic_user(chat_id):
    """Удаляет пользователя из динамического списка."""
    r.srem(DYNAMIC_ALLOWED_SET, chat_id)

def send_telegram(chat_id, text, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode})

def notify_admin_unauthorized(chat_id, user_info):
    """Уведомляет админа о попытке доступа (не чаще раза в час)."""
    notify_key = f"{UNAUTHORIZED_NOTIFY_PREFIX}{chat_id}"
    if r.exists(notify_key):
        return  # уже уведомляли
    username = user_info.get("username", "")
    first_name = user_info.get("first_name", "")
    last_name = user_info.get("last_name", "")
    if username:
        user_display = f"@{username}"
    else:
        user_display = f"{first_name} {last_name}".strip() or "Неизвестный"
    msg_text = (
        f"🔔 <b>Запрос доступа</b>\n"
        f"Пользователь: {user_display}\n"
        f"Chat ID: <code>{chat_id}</code>\n\n"
        f"Чтобы добавить его, отправьте команду:\n"
        f"/adduser {chat_id}\n\n"
        f"Чтобы удалить:\n"
        f"/removeuser {chat_id}"
    )
    send_telegram(ADMIN_CHAT_ID, msg_text)
    r.setex(notify_key, 3600, "1")