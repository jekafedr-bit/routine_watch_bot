import json
import os
import datetime
import redis
import requests
import logging

if not logging.getLogger().hasHandlers():
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )

logger = logging.getLogger(__name__)

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
PENDING_KEY = "pending_task:"

from datetime import timezone, timedelta

MSK = timezone(timedelta(hours=3))

def now_msk():
    return datetime.datetime.now(MSK)

def format_msk(iso_str):
    """Конвертирует ISO-строку с часовым поясом в читаемый вид по Москве."""
    try:
        dt_utc = datetime.datetime.fromisoformat(iso_str)
        if dt_utc.tzinfo is None:
            # Если вдруг сохранилось без зоны, считаем UTC
            dt_utc = dt_utc.replace(tzinfo=timezone.utc)
        dt_msk = dt_utc.astimezone(MSK)
        return dt_msk.strftime("%d.%m.%Y %H:%M (МСК)")
    except Exception:
        return iso_str

# ---------- Telegram ----------
def send_telegram(chat_id, text, parse_mode="HTML", reply_markup=None):
    url = f"https://api.telegram.org/bot{TOKEN}/sendMessage"
    payload = {
        "chat_id": chat_id,
        "text": text,
        "parse_mode": parse_mode
    }
    if reply_markup:
        payload["reply_markup"] = json.dumps(reply_markup)  # нужно будет import json
    requests.post(url, json=payload)

# ---------- Управление задачами ----------
def get_next_task_id():
    return r.incr(TASK_ID_COUNTER)

def save_task(task_id, query, interval_min, chat_id):
    now = now_msk().isoformat()
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
    """Переносит старые задачи (без chat_id) на admin_chat_id и добавляет в user_tasks."""
    logger.info("Checking for legacy tasks...")
    all_ids = [tid.decode() for tid in r.smembers(TASKS_SET)]
    if not all_ids:
        logger.info("No tasks found at all, nothing to migrate.")
        return
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
        logger.info(f"Migrated {migrated} legacy tasks to user {admin_chat_id}")
    else:
        logger.info("No legacy tasks found (all tasks already have chat_id).")

def get_all_task_ids():
    """Возвращает список всех ID задач (глобально). Используется в cron_task.py."""
    raw = r.smembers(TASKS_SET)
    return [tid.decode() for tid in raw]

def check_deepseek(query):
    """Проверяет новость или факт через DeepSeek Responses API с web_search.
       Возвращает (True, текст_новости) или (False, None)."""
    logger.info(f"Checking query: {query[:80]}...")
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json"
    }

    msk_time = now_msk().strftime("%H:%M %d.%m.%Y")

    prompt = (
        f"Текущее московское время: {msk_time}. Запрос пользователя: \"{query}\".\n\n"
        "Ты — ассистент, который выдаёт только результат проверки факта или поиска новости.\n"
        "Формат ответа СТРОГО:\n"
        "1) 'ДА: <краткая суть>' — если факт подтверждён или новость найдена.\n"
        "2) 'НЕТ' — если факт не подтверждён или новостей нет.\n\n"
        "Не пиши НИКАКИХ пояснений, рассуждений или вводных фраз. Начинай сразу с 'ДА:' или 'НЕТ'.\n\n"
        "Примеры:\n"
        "Запрос: снижение цен на авиабилеты в Японию\n"
        "Ответ: ДА: Авиакомпания JAL объявила о снижении цен на 20%.\n"
        "Запрос: есть ли прямые рейсы Москва-Токио?\n"
        "Ответ: НЕТ"
    )

    payload = {
        "model": "deepseek-chat",
        "input": prompt,
        "tools": [{"type": "web_search"}],
        "temperature": 0.2,
        "max_output_tokens": 500
    }

    # Логируем то, что отправляем (без ключей)
    logger.info(f"DeepSeek request payload: model={payload['model']}, temperature={payload['temperature']}, "
                f"max_output_tokens={payload['max_output_tokens']}, prompt_preview={prompt[:120]}...")

    try:
        resp = requests.post(
            "https://api.deepseek.com/v1/responses",
            headers=headers,
            json=payload,
            timeout=60
        )
        # Логируем статус и часть ответа
        logger.info(f"DeepSeek response status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            # Логируем полный JSON (можно обрезать, если слишком большой)
            logger.info(f"DeepSeek full response: {json.dumps(data, ensure_ascii=False)[:2000]}")
            # Пытаемся взять output_text (если есть)
            answer = data.get("output_text", "").strip()
            if not answer:
                # Ищем сообщение с phase "final_answer"
                for item in data.get("output", []):
                    if item.get("type") == "message" and item.get("phase") == "final_answer":
                        content = item.get("content", [])
                        if content:
                            answer = content[0].get("text", "").strip()
                            break
            if not answer:
                # Если не нашли, берём последнее сообщение
                for item in reversed(data.get("output", [])):
                    if item.get("type") == "message":
                        content = item.get("content", [])
                        if content:
                            answer = content[0].get("text", "").strip()
                            break
            logger.info(f"Extracted answer: {answer}")
            if not answer:
                logger.warning("Empty answer extracted from DeepSeek response")

            # Проверяем формат: ищем "ДА:" в любом месте ответа
            if "ДА:" in answer:
                news_start = answer.find("ДА:") + 3
                news = answer[news_start:].strip().split("\n")[0]
                return True, news
            elif answer.startswith("НЕТ"):
                return False, None
            else:
                # Fallback: используем chat/completions с принудительным поиском
                logger.warning("DeepSeek returned non-compliant answer, falling back to chat/completions")
                fallback_payload = {
                    "model": "deepseek-chat",
                    "messages": [
                        {
                            "role": "system",
                            "content": (
                                "Ты — ассистент, который отвечает только в формате 'ДА: <краткая суть>' или 'НЕТ'. "
                                "Не пиши никаких вступлений, пояснений или рассуждений. "
                                "Сразу давай ответ, используя поиск в интернете."
                            )
                        },
                        {
                            "role": "user",
                            "content": f"Запрос: {query}"
                        }
                    ],
                    "search": True,
                    "temperature": 0.0,
                    "max_tokens": 150
                }
                logger.info(f"Fallback request payload: {json.dumps(fallback_payload, ensure_ascii=False)[:500]}")
                try:
                    fallback_resp = requests.post(
                        "https://api.deepseek.com/v1/chat/completions",
                        headers=headers,
                        json=fallback_payload,
                        timeout=30
                    )
                    logger.info(f"Fallback response status: {fallback_resp.status_code}")
                    if fallback_resp.status_code == 200:
                        fallback_data = fallback_resp.json()
                        logger.info(f"Fallback full response: {json.dumps(fallback_data, ensure_ascii=False)[:2000]}")
                        fallback_answer = fallback_data["choices"][0]["message"]["content"].strip()
                        logger.info(f"Fallback answer: {fallback_answer}")
                        answer = fallback_answer
                        if answer.startswith("ДА:"):
                            news_start = answer.find("ДА:") + 3
                            news = answer[news_start:].strip().split("\n")[0]
                            return True, news
                        elif answer.startswith("НЕТ"):
                            return False, None
                except Exception as e:
                    logger.warning(f"Fallback request failed: {e}")
        else:
            logger.warning(f"DeepSeek API error: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"DeepSeek exception: {e}")

    return False, None

# ---------- Донаты ----------
def get_donation_message():
    """Возвращает сообщение с инструкцией по донатам."""
    donation_text = os.environ.get("DONATION_TEXT", "").strip()
    if not donation_text:
        donation_text = (
            "Отправьте любую сумму в @wallet на никнейм @ekfedorov.\n"
            "Как это сделать: откройте @wallet → «Отправить» → "
            "в поле «Получатель» введите @ekfedorov → укажите сумму и подтвердите."
        )
    return f"\n\n❤️ <b>Поддержите проект!</b>\n{donation_text}"

def set_pending_task(chat_id, step, query=None):
    """Сохраняет состояние создания задачи: step='query' или 'interval', query - текст запроса."""
    data = {"step": step}
    if query:
        data["query"] = query
    r.setex(f"{PENDING_KEY}{chat_id}", 600, json.dumps(data))

def get_pending_task(chat_id):
    """Возвращает словарь с состоянием или None."""
    raw = r.get(f"{PENDING_KEY}{chat_id}")
    if raw:
        return json.loads(raw.decode())
    return None

def delete_pending_task(chat_id):
    r.delete(f"{PENDING_KEY}{chat_id}")