import os
import datetime
import redis
import requests

# ---------- Окружение ----------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# Список разрешённых пользователей (через запятую). Если пусто – только ADMIN_CHAT_ID
allowed_raw = os.environ.get("ALLOWED_USERS", "")
if allowed_raw:
    ALLOWED_USERS = set(int(uid.strip()) for uid in allowed_raw.split(",") if uid.strip())
else:
    ALLOWED_USERS = {ADMIN_CHAT_ID}

r = redis.from_url(REDIS_URL)

# Ключи Redis
TASKS_SET = "tasks"                    # оставим для обратной совместимости (не обязательно)
TASK_PREFIX = "task:"
TASK_ID_COUNTER = "task_id_counter"
USER_TASKS_PREFIX = "user_tasks:"      # множество task_id для каждого пользователя

# ---------- Проверка доступа ----------
def is_allowed(chat_id):
    """Возвращает True, если пользователь с chat_id имеет доступ к боту."""
    return chat_id in ALLOWED_USERS

# ---------- Telegram ----------
def send_telegram(chat_id, text, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode})

# ---------- Управление задачами ----------
def get_next_task_id():
    return r.incr(TASK_ID_COUNTER)

def save_task(task_id, query, interval_min, chat_id):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    r.hset(f"{TASK_PREFIX}{task_id}", mapping={
        "query": query,
        "interval": interval_min,
        "last_run": now,
        "paused": "0",
        "created": now,
        "chat_id": str(chat_id)          # храним как строку
    })
    r.sadd(TASKS_SET, task_id)          # общий набор
    r.sadd(f"{USER_TASKS_PREFIX}{chat_id}", task_id)   # набор пользователя

def delete_task(task_id, chat_id):
    # Удаляем из общего и пользовательского наборов
    r.srem(TASKS_SET, task_id)
    r.srem(f"{USER_TASKS_PREFIX}{chat_id}", task_id)
    r.delete(f"{TASK_PREFIX}{task_id}")

def get_user_task_ids(chat_id):
    """Возвращает список task_id, принадлежащих пользователю."""
    raw = r.smembers(f"{USER_TASKS_PREFIX}{chat_id}")
    return [tid.decode() for tid in raw]

def get_task_info(task_id):
    raw = r.hgetall(f"{TASK_PREFIX}{task_id}")
    if not raw:
        return None
    return {k.decode(): v.decode() for k, v in raw.items()}

def set_task_paused(task_id, paused):
    r.hset(f"{TASK_PREFIX}{task_id}", "paused", "1" if paused else "0")

def update_last_run(task_id, ts):
    r.hset(f"{TASK_PREFIX}{task_id}", "last_run", ts)

def parse_duration(text):
    text = text.strip().lower()
    if text in ("day", "daily", "сутки"):
        return 1440
    if text in ("hour", "hourly", "час"):
        return 60
    parts = text.split()
    if len(parts) >= 2:
        try:
            num = float(parts[0])
            unit = parts[1]
            if unit in ("min", "mins", "minute", "minutes", "минуты", "минут"):
                return int(num)
            elif unit in ("hour", "hours", "часа", "часов"):
                return int(num * 60)
            elif unit in ("day", "days", "дня", "дней"):
                return int(num * 1440)
        except:
            pass
    return None

def migrate_legacy_tasks(admin_chat_id):
    """
    Переносит старые задачи (без chat_id) на admin_chat_id и добавляет в user_tasks.
    Выполняется один раз при старте.
    """
    all_ids = [tid.decode() for tid in r.smembers(TASKS_SET)]
    migrated = 0
    for tid in all_ids:
        info = r.hgetall(f"{TASK_PREFIX}{tid}")
        if not info:
            continue
        info = {k.decode(): v.decode() for k, v in info.items()}
        if "chat_id" not in info:
            # Привязываем к админу
            r.hset(f"{TASK_PREFIX}{tid}", "chat_id", str(admin_chat_id))
            r.sadd(f"{USER_TASKS_PREFIX}{admin_chat_id}", tid)
            migrated += 1
    if migrated:
        print(f"Migrated {migrated} legacy tasks to user {admin_chat_id}")

def get_all_task_ids():
    """Возвращает список всех ID задач (глобально). Используется в cron_task.py."""
    raw = r.smembers(TASKS_SET)
    return [tid.decode() for tid in raw]