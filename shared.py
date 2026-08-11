import os
import datetime
import redis
import requests

# ---------- Окружение ----------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

r = redis.from_url(REDIS_URL)

# Ключи Redis
TASKS_SET = "tasks"
TASK_PREFIX = "task:"
TASK_ID_COUNTER = "task_id_counter"

# ---------- Telegram ----------
def send_telegram(chat_id, text, parse_mode="HTML"):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": parse_mode})

# ---------- Управление задачами ----------
def get_next_task_id():
    return r.incr(TASK_ID_COUNTER)

def save_task(task_id, query, interval_min):
    now = datetime.datetime.now(datetime.UTC).isoformat()
    r.hset(f"{TASK_PREFIX}{task_id}", mapping={
        "query": query,
        "interval": interval_min,
        "last_run": now,
        "paused": "0",
        "created": now
    })
    r.sadd(TASKS_SET, task_id)

def delete_task(task_id):
    r.delete(f"{TASK_PREFIX}{task_id}")
    r.srem(TASKS_SET, task_id)

def get_all_task_ids():
    raw_ids = r.smembers(TASKS_SET)
    return [tid.decode() for tid in raw_ids]

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