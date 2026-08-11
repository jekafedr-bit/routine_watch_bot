import os
import datetime
import redis
import requests

# ---------- Окружение ----------
TOKEN = os.environ["TELEGRAM_BOT_TOKEN"]
REDIS_URL = os.environ["UPSTASH_REDIS_URL"]
ADMIN_CHAT_ID = int(os.environ["ADMIN_CHAT_ID"])
DEEPSEEK_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

# Админ всегда имеет доступ, даже если ALLOWED_USERS не задана или задана без него
ALLOWED_USERS = {ADMIN_CHAT_ID}
allowed_raw = os.environ.get("ALLOWED_USERS", "")
if allowed_raw:
    for uid in allowed_raw.split(","):
        uid = uid.strip()
        if uid:
            ALLOWED_USERS.add(int(uid))

r = redis.from_url(REDIS_URL)

# Ключи Redis
TASKS_SET = "tasks"                    # оставим для обратной совместимости (не обязательно)
TASK_PREFIX = "task:"
TASK_ID_COUNTER = "task_id_counter"
USER_TASKS_PREFIX = "user_tasks:"      # множество task_id для каждого пользователя

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

def pause_user_tasks(chat_id):
    """Ставит на паузу все активные задачи пользователя."""
    task_ids = get_user_task_ids(chat_id)
    for tid in task_ids:
        set_task_paused(tid, True)

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

def check_deepseek(query):
    """Проверяет новость или факт через DeepSeek Responses API с web_search.
       Возвращает (True, текст_новости) или (False, None)."""
    print(f"  [DEBUG] Checking query: {query[:80]}...")
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json"
    }
    today = datetime.date.today().strftime("%d.%m.%Y")
    prompt = (
        f"Сегодня {today}. Вот запрос пользователя: \"{query}\".\n\n"
        "Твоя задача:\n"
        "- Если запрос можно проверить как факт (например, 'сегодня август', 'закон подписан'), "
        "проверь его актуальность через поиск в интернете. Если факт верен, ответь 'ДА: ' и кратко объясни. "
        "Если неверен — ответь 'НЕТ'.\n"
        "- Если запрос является поисковой темой (например, 'снижение цен на билеты'), "
        "найди самую свежую новость за последние сутки. Если новость с официальным подтверждением найдена — "
        "ответь 'ДА: ' и кратко опиши суть. Если нет — ответь 'НЕТ'.\n\n"
        "Отвечай только в таком формате, без лишних слов."
    )
    payload = {
        "model": "deepseek-chat",
        "input": prompt,
        "tools": [{"type": "web_search"}],
        "temperature": 0.3,
        "max_output_tokens": 600
    }
    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=30
        )
        if resp.status_code == 200:
            data = resp.json()
            answer = ""
            for item in data.get("output", []):
                if item.get("type") == "message":
                    answer = item.get("content", [{}])[0].get("text", "").strip()
                    break
            print(f"  [DEBUG] Full answer: {answer}")
            if "ДА:" in answer:
                news_start = answer.find("ДА:") + 3
                news = answer[news_start:].strip().split("\n")[0]
                return True, news
            elif answer.startswith("ДА"):
                news = answer[2:].strip().lstrip(": ").strip()
                return True, news
        else:
            print(f"  [DEBUG] Error body: {resp.text}")
    except Exception as e:
        print(f"  [DEBUG] Exception: {e}")
    return False, None

# ---------- Донаты ----------
def get_donation_message():
    """Возвращает сообщение с инструкцией по донатам, если переменная окружения задана."""
    donation_text = os.environ.get("DONATION_TEXT", "").strip()
    if donation_text:
        return f"\n\n❤️ <b>Поддержите проект!</b>\n{donation_text}"
    return ("❤️ Поддержите проект — отправьте любую сумму в @wallet на никнейм @ekfedorov."
            "Как это сделать: откройте @wallet → «Отправить» → в поле «Получатель» введите @ekfedorov → укажите сумму и подтвердите.")