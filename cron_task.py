import datetime
from datetime import timedelta
import requests
from shared import (
    TOKEN, ADMIN_CHAT_ID, DEEPSEEK_KEY, send_telegram,
    get_all_task_ids, get_task_info, update_last_run
)

def check_deepseek(query):
    print(f"  [DEBUG] Sending query to DeepSeek: {query[:80]}...")
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_KEY}",
        "Content-Type": "application/json"
    }
    # Изменённый промпт: просим просто найти новость и кратко ответить, начиная с ДА/НЕТ
    prompt = (
        f"Найди в интернете актуальную информацию по запросу: {query}\n\n"
        "Если есть официальное подтверждение или новость, напиши 'ДА: ' и кратко опиши суть (1-2 предложения). "
        "Если ничего нет, напиши просто 'НЕТ'."
    )
    payload = {
        "model": "deepseek-chat",
        "messages": [{"role": "user", "content": prompt}],
        "search": True,
        "temperature": 0.3,
        "max_tokens": 400
    }
    try:
        resp = requests.post("https://api.deepseek.com/v1/chat/completions", headers=headers, json=payload, timeout=25)
        print(f"  [DEBUG] DeepSeek response status: {resp.status_code}")
        if resp.status_code == 200:
            data = resp.json()
            answer = data["choices"][0]["message"]["content"].strip()
            print(f"  [DEBUG] DeepSeek answer: {answer}")
            if answer.startswith("ДА"):
                news = answer.split(":", 1)[1].strip() if ":" in answer else "Нашлась новость"
                return True, news
        else:
            print(f"  [DEBUG] DeepSeek error body: {resp.text}")
    except Exception as e:
        print(f"  [DEBUG] DeepSeek exception: {e}")
    return False, None

def process_due_tasks():
    now = datetime.datetime.now(datetime.UTC)
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
                last_run = datetime.datetime.fromisoformat(last_run_str)
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