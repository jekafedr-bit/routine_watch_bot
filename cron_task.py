import os
import redis
import requests
from datetime import datetime, timedelta

# Переменные окружения
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
DEEPSEEK_KEY = os.environ["DEEPSEEK_API_KEY"]
REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])

r = redis.from_url(REDIS_URL)

TASKS_SET = "tasks"
TASK_PREFIX = "task:"

def get_all_task_ids():
    return list(r.smembers(TASKS_SET))

def get_task_info(task_id):
    raw = r.hgetall(f"{TASK_PREFIX}{task_id}")
    if not raw:
        return None
    return {k.decode(): v.decode() for k, v in raw.items()}

def update_last_run(task_id, ts):
    r.hset(f"{TASK_PREFIX}{task_id}", "last_run", ts)

def send_telegram(chat_id, text):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "HTML"})

def check_deepseek(query):
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": f"Проверь новости по запросу: {query}\nЕсли есть официальное подтверждение, ответь 'ДА: <суть>'. Иначе 'НЕТ'."}],
        "search": True,
        "temperature": 0.1,
        "max_tokens": 200
    }
    try:
        resp = requests.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=25)
        if resp.status_code == 200:
            data = resp.json()
            answer = data["choices"][0]["message"]["content"].strip()
            if answer.startswith("ДА"):
                news = answer.split(":", 1)[1].strip() if ":" in answer else "Нашлась новость"
                return True, news
    except Exception as e:
        print(f"DeepSeek error: {e}")
    return False, None

def process_due_tasks():
    now = datetime.utcnow()
    now_iso = now.isoformat()
    task_ids = get_all_task_ids()
    for tid in task_ids:
        info = get_task_info(tid)
        if not info or info.get("paused") == "1":
            continue
        last_run_str = info.get("last_run")
        if not last_run_str:
            last_run = now - timedelta(days=1)
        else:
            try:
                last_run = datetime.fromisoformat(last_run_str)
            except:
                last_run = now - timedelta(days=1)
        interval = int(info.get("interval", "1440"))
        if (now - last_run).total_seconds() >= interval * 60:
            print(f"Checking task {tid}")
            found, news = check_deepseek(info["query"])
            if found:
                send_telegram(ADMIN_CHAT_ID, f"🔔 <b>Новость по задаче #{tid}</b>\n{news}")
            update_last_run(tid, now_iso)

if __name__ == "__main__":
    process_due_tasks()