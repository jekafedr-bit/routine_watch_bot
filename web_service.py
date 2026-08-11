import os
import requests
from flask import Flask, request
from shared import (
    TOKEN, ADMIN_CHAT_ID, send_telegram, is_allowed,
    get_next_task_id, save_task, delete_task,
    get_user_task_ids, get_task_info, set_task_paused,
    parse_duration
)

app = Flask(__name__)

# ---------- Обработка команд ----------
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    # Проверка доступа
    if not is_allowed(chat_id):
        send_telegram(chat_id, "У вас нет доступа к этому боту.")
        return

    if text.startswith("/start"):
        send_telegram(chat_id, (
            "🤖 Бот для отслеживания новостей.\n\n"
            "Команды:\n"
            "/newtask &lt;запрос&gt; /every &lt;интервал&gt; — создать задачу\n"
            "/tasks — список ваших задач\n"
            "/taskinfo &lt;ID&gt; — инфо о задаче\n"
            "/deletetask &lt;ID&gt; — удалить\n"
            "/pause &lt;ID&gt; /resume &lt;ID&gt; — пауза/продолжить"
        ), parse_mode="HTML")

    elif text.startswith("/newtask"):
        parts = text.split("/every")
        if len(parts) < 2:
            send_telegram(chat_id, "❗ Формат: /newtask <запрос> /every <интервал>")
            return
        query = parts[0].replace("/newtask", "", 1).strip()
        if not query:
            send_telegram(chat_id, "❗ Укажи запрос.")
            return
        dur_str = parts[1].strip()
        interval = parse_duration(dur_str)
        if interval is None:
            send_telegram(chat_id, "❗ Не понял интервал. Используй: 5 minutes, 1 hour, daily")
            return
        if interval < 5:
            send_telegram(chat_id, "❗ Минимальный интервал — 5 минут.")
            return
        task_id = get_next_task_id()
        save_task(task_id, query, interval, chat_id)   # передаём chat_id
        send_telegram(chat_id, f"✅ Задача #{task_id} создана.\nЗапрос: {query}\nИнтервал: {interval} мин.")

    elif text.startswith("/tasks"):
        ids = get_user_task_ids(chat_id)   # только задачи пользователя
        if not ids:
            send_telegram(chat_id, "У вас нет задач.")
            return
        lines = ["<b>📋 Ваши задачи:</b>"]
        for tid in sorted(ids, key=lambda x: int(x)):
            info = get_task_info(tid)
            if not info:
                continue
            paused = "⏸" if info.get("paused") == "1" else "▶"
            q = info.get("query", "")[:50]
            interval = info.get("interval", "?")
            lines.append(f"{paused} <b>#{tid}</b> {q}… ({interval} мин)")
        send_telegram(chat_id, "\n".join(lines), parse_mode="HTML")

    elif text.startswith("/taskinfo"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram(chat_id, "Укажи ID, например /taskinfo 1")
            return
        tid = parts[1]
        info = get_task_info(tid)
        if not info:
            send_telegram(chat_id, "Задача не найдена.")
            return
        if info.get("chat_id") != str(chat_id):   # проверка владельца
            send_telegram(chat_id, "Это не ваша задача.")
            return
        paused = "Да" if info.get("paused") == "1" else "Нет"
        send_telegram(chat_id,
            f"<b>Задача #{tid}</b>\n"
            f"Запрос: {info['query']}\n"
            f"Интервал: {info['interval']} мин\n"
            f"Пауза: {paused}\n"
            f"Последний запуск: {info.get('last_run', '?')}",
            parse_mode="HTML")

    elif text.startswith("/deletetask"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram(chat_id, "Укажи ID.")
            return
        tid = parts[1]
        info = get_task_info(tid)
        if not info:
            send_telegram(chat_id, "Задача не найдена.")
            return
        if info.get("chat_id") != str(chat_id):
            send_telegram(chat_id, "Это не ваша задача.")
            return
        delete_task(tid, chat_id)   # удаляем с привязкой к пользователю
        send_telegram(chat_id, f"🗑 Задача #{tid} удалена.")

    elif text.startswith("/pause"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram(chat_id, "Укажи ID.")
            return
        tid = parts[1]
        info = get_task_info(tid)
        if not info or info.get("chat_id") != str(chat_id):
            send_telegram(chat_id, "Задача не найдена или не ваша.")
            return
        set_task_paused(tid, True)
        send_telegram(chat_id, f"⏸ Задача #{tid} на паузе.")

    elif text.startswith("/resume"):
        parts = text.split()
        if len(parts) < 2:
            send_telegram(chat_id, "Укажи ID.")
            return
        tid = parts[1]
        info = get_task_info(tid)
        if not info or info.get("chat_id") != str(chat_id):
            send_telegram(chat_id, "Задача не найдена или не ваша.")
            return
        set_task_paused(tid, False)
        send_telegram(chat_id, f"▶ Задача #{tid} возобновлена.")

# ---------- Вебхук ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data or "message" not in data:
        return "ok"
    handle_message(data["message"])
    return "ok"

@app.route("/", methods=["GET"])
def index():
    return "Bot Web Service is running"

# ---------- Установка вебхука при старте ----------
def set_webhook():
    base = os.environ.get("RENDER_EXTERNAL_URL", "")
    if base:
        url = f"{base}/webhook"
        resp = requests.post(f"https://api.telegram.org/bot{TOKEN}/setWebhook", json={"url": url})
        print("Webhook set:", resp.json())

if __name__ == "__main__":
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))