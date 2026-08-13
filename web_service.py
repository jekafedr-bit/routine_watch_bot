import logging
import os

import requests
from flask import Flask, request

logger = logging.getLogger(__name__)
from shared import (
    TOKEN, ADMIN_CHAT_ID, send_telegram,
    get_task_info, set_task_paused,
    delete_task,
    pause_user_tasks,
    set_pending_task, get_pending_task, delete_pending_task,
    extract_interval_from_text, parse_duration,
    migrate_legacy_tasks,
)
from user_management import (
    is_allowed, notify_admin_unauthorized,
    add_dynamic_user, remove_dynamic_user,
    reset_unauthorized_notify, get_all_allowed_users, remember_user, get_chat_id_by_username
)
from tasks import (
    create_task_and_check, handle_newtask, handle_tasks, handle_taskinfo,
    handle_deletetask, handle_pause, handle_resume,
    send_tasks_inline, send_task_actions,
)

app = Flask(__name__)

# ---------- Обработка команд ----------
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()
    user_info = msg.get("from", {})
    # Сохраняем username
    remember_user(chat_id, user_info)
    # Логируем с username
    username = user_info.get("username", "нет_username")
    logger.info(f"Received from {chat_id} (@{username}): {text[:200]}")

    # Обработка reply-кнопки "Меню"
    if text == "🏠 Меню":
        if is_allowed(chat_id):
            send_main_menu(chat_id)
        else:
            send_telegram(chat_id, "У вас нет доступа к меню.")
        return

    # Обработка reply-кнопки "Обратная связь"
    if text == "💬 Обратная связь":
        set_pending_task(chat_id, "feedback")
        send_telegram(chat_id, "📝 Пожалуйста, напишите ваше сообщение (предложение, проблему, отзыв).\nДля отмены – /cancel")
        return

    # ── /start доступен всем ──
    if text.startswith("/start"):
        user_keyboard = get_user_keyboard()
        admin_keyboard = get_admin_keyboard()

        if is_allowed(chat_id):
            if chat_id == ADMIN_CHAT_ID:
                send_telegram(chat_id, (
                    "🤖 <b>Бот для отслеживания новостей</b>\n\n"
                    "<b>Как это работает:</b>\n"
                    "• Вы создаёте задачу с поисковым запросом и интервалом проверки.\n"
                    "• Бот немедленно проверяет запрос и, если новость/факт найдены, сообщает вам и ставит задачу на паузу.\n"
                    "• Если сразу ничего не найдено, бот будет проверять задачу с указанной периодичностью (например, каждый час или день), пока не появится новость.\n"
                    "• При обнаружении новости вы получите уведомление, и задача автоматически встанет на паузу.\n\n"
                    "<b>Команды администратора:</b>\n"
                    "/newtask <запрос> /every <интервал> — создать задачу\n"
                    "/tasks — список ваших задач\n"
                    "/taskinfo <ID> — инфо о любой задаче\n"
                    "/deletetask <ID> — удалить свою задачу\n"
                    "/pause <ID> /resume <ID> — пауза/продолжить\n"
                    "/adduser <chat_id или @username> — дать доступ пользователю\n"
                    "/removeuser <chat_id или @username> — отозвать доступ (задачи пользователя встанут на паузу)\n\n"
                    "🔔 При попытке неавторизованного доступа вы получите уведомление."
                ), parse_mode="HTML", reply_markup=admin_keyboard)
            else:
                send_telegram(chat_id, (
                    "🤖 <b>Бот для отслеживания новостей</b>\n\n"
                    "<b>Как это работает:</b>\n"
                    "• Вы создаёте задачу с поисковым запросом и интервалом проверки.\n"
                    "• Бот немедленно проверяет запрос и, если новость/факт найдены, сообщает вам и ставит задачу на паузу.\n"
                    "• Если сразу ничего не найдено, бот будет проверять задачу с указанной периодичностью (например, каждый час или день), пока не появится новость.\n"
                    "• При обнаружении новости вы получите уведомление, и задача автоматически встанет на паузу.\n\n"
                    "<b>Доступные команды:</b>\n"
                    "/newtask <запрос> /every <интервал> — создать задачу\n"
                    "/tasks — список ваших задач\n"
                    "/taskinfo <ID> — инфо о задаче\n"
                    "/deletetask <ID> — удалить задачу\n"
                    "/pause <ID> /resume <ID> — пауза/продолжить"
                ), parse_mode="HTML", reply_markup=user_keyboard)
        else:
            # неавторизованный пользователь – приветствие без клавиатуры
            send_telegram(chat_id,
                          "🤖 <b>Бот для отслеживания новостей</b>\n\n"
                          "Я помогаю следить за появлением новостей и фактов по вашим запросам.\n"
                          "Доступ предоставляется администратором.\n\n"
                          "Ваш запрос на доступ отправлен. Когда администратор одобрит заявку, вы получите уведомление."
                          , parse_mode="HTML")
            user_info = msg.get("from", {})
            notify_admin_unauthorized(chat_id, user_info)
        return

    # ── Обработка ожидания ввода (пошаговое создание задачи, обратная связь) ──
    pending = get_pending_task(chat_id)
    if pending:
        if text.startswith("/cancel"):
            delete_pending_task(chat_id)
            send_telegram(chat_id, "❌ Действие отменено.")
            return
        step = pending.get("step")
        if step == "query":
            if not text or text.startswith("/"):
                send_telegram(chat_id, "📝 Пожалуйста, введите поисковый запрос. Или /cancel для отмены.")
                return
            interval = extract_interval_from_text(text)
            if interval is not None and interval >= 5:
                # Нашли интервал, предлагаем подтверждение
                set_pending_task(chat_id, "confirm_interval", query=text, interval=interval)
                keyboard = {
                    "inline_keyboard": [
                        [{"text": "✅ Да", "callback_data": "confirm_interval_yes"}],
                        [{"text": "❌ Нет, ввести свой", "callback_data": "confirm_interval_no"}]
                    ]
                }
                send_telegram(chat_id,
                              f"Похоже, вы указали интервал: {interval} мин. Верно?",
                              parse_mode="HTML", reply_markup=keyboard)
                return
            else:
                set_pending_task(chat_id, "interval", query=text)
                send_telegram(chat_id,
                              "📝 Запрос сохранён. Теперь введите интервал проверки (например, 5 minutes, 1 hour, daily).\nДля отмены – /cancel")
                return
        elif step == "interval":
            query = pending.get("query", "")
            if not query:
                delete_pending_task(chat_id)
                send_telegram(chat_id, "⚠️ Ошибка: запрос не найден. Начните заново с /newtask.")
                return
            interval = parse_duration(text)
            if interval is None:
                send_telegram(chat_id,
                              "❗ Не удалось распознать интервал. Введите, например, 5 minutes, 1 hour, daily.\nДля отмены – /cancel")
                return
            if interval < 5:
                send_telegram(chat_id, "❗ Минимальный интервал — 5 минут.")
                return
            # Создаём задачу и выполняем немедленную проверку
            create_task_and_check(chat_id, query, interval, msg)
            delete_pending_task(chat_id)
            return
        elif step == "feedback":
            # Пользователь вводит сообщение для администратора
            if not text or text.startswith("/"):
                send_telegram(chat_id, "📝 Пожалуйста, введите текст сообщения. Или /cancel для отмены.")
                return
            delete_pending_task(chat_id)
            user_info = msg.get("from", {})
            username = user_info.get("username", "нет_username")
            send_telegram(
                ADMIN_CHAT_ID,
                f"📩 <b>Обратная связь</b>\n"
                f"От: @{username} (chat_id: {chat_id})\n\n"
                f"{text}"
            )
            send_telegram(chat_id, "✅ Спасибо! Ваше сообщение отправлено администратору.")
            return
        else:
            delete_pending_task(chat_id)
            send_telegram(chat_id, "⚠️ Ошибка состояния. Начните заново с /newtask.")
            return

    # ── Все остальные команды требуют авторизации ──
    if not is_allowed(chat_id):
        user_info = msg.get("from", {})
        notify_admin_unauthorized(chat_id, user_info)
        send_telegram(chat_id, "У вас нет доступа к этому боту. Запрос администратору отправлен.")
        return

    if text.startswith("/newtask"):
        handle_newtask(chat_id, text, msg)

    elif text.startswith("/tasks"):
        handle_tasks(chat_id)

    elif text.startswith("/taskinfo"):
        handle_taskinfo(chat_id, text)

    elif text.startswith("/deletetask"):
        handle_deletetask(chat_id, text)

    elif text.startswith("/pause"):
        handle_pause(chat_id, text)

    elif text.startswith("/resume"):
        handle_resume(chat_id, text)

    elif text.startswith("/adduser") and chat_id == ADMIN_CHAT_ID:
        parts = text.split()
        if len(parts) < 2:
            send_telegram(chat_id, "Укажите chat_id или @username, например: /adduser 123456789 или /adduser @user")
            return
        target = parts[1]
        new_user_id = None
        # Если введён @username, ищем chat_id
        if target.startswith('@') or not target.isdigit():
            target_username = target.lstrip('@')
            new_user_id = get_chat_id_by_username(target_username)
            if not new_user_id:
                send_telegram(chat_id,
                              f"Не найден пользователь с username @{target_username}. Попросите его написать боту /start, чтобы я его запомнил.")
                return
        else:
            new_user_id = int(target)

        add_dynamic_user(new_user_id)
        send_telegram(chat_id, f"✅ Пользователь {target} добавлен в динамический доступ (chat_id: {new_user_id}).")
        try:
            send_telegram(new_user_id, "✅ Администратор предоставил вам доступ к боту. Можете начинать работу.")
        except:
            pass

    elif text.startswith("/removeuser") and chat_id == ADMIN_CHAT_ID:
        parts = text.split()
        if len(parts) < 2:
            send_telegram(chat_id, "Укажите chat_id или @username, например: /removeuser 123456789 или /removeuser @user")
            return
        target = parts[1]
        user_id = None
        if target.startswith('@') or not target.isdigit():
            target_username = target.lstrip('@')
            user_id = get_chat_id_by_username(target_username)
            if not user_id:
                send_telegram(chat_id, f"Не найден пользователь с username @{target_username}.")
                return
        else:
            user_id = int(target)

        remove_dynamic_user(user_id)
        pause_user_tasks(user_id)
        reset_unauthorized_notify(user_id)
        send_telegram(chat_id, f"✅ Пользователь {target} удалён из доступа, все его задачи остановлены.")

    elif text.startswith("/notify_update") and chat_id == ADMIN_CHAT_ID:
        users = get_all_allowed_users()
        notified = 0
        for uid in users:
            try:
                send_telegram(uid, "🔄 Бот был обновлён! Нажмите /start, чтобы увидеть обновлённое меню и кнопки.")
                notified += 1
            except Exception as e:
                logger.warning(f"Failed to notify user {uid}: {e}")
        send_telegram(chat_id, f"✅ Уведомление отправлено {notified} пользователям.")

def get_admin_keyboard():
    return {
        "keyboard": [
            ["🏠 Меню", "💬 Обратная связь"],
            ["/newtask", "/tasks"],
            ["/taskinfo", "/pause"],
            ["/resume", "/deletetask"],
            ["/adduser", "/removeuser"],
            ["/notify_update"]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

def get_user_keyboard():
    return {
        "keyboard": [
            ["🏠 Меню", "💬 Обратная связь"],
            ["/newtask", "/tasks"],
            ["/taskinfo", "/pause"],
            ["/resume", "/deletetask"]
        ],
        "resize_keyboard": True,
        "one_time_keyboard": False
    }

def send_main_menu(chat_id):
    keyboard = {
        "inline_keyboard": [
            [{"text": "➕ Создать задачу", "callback_data": "new_task"}],
            [{"text": "📋 Мои задачи", "callback_data": "tasks"}],
            [{"text": "💬 Обратная связь", "callback_data": "feedback"}],
        ]
    }
    send_telegram(chat_id, "🏠 <b>Главное меню</b>\nВыберите действие:", parse_mode="HTML", reply_markup=keyboard)

# ---------- Вебхук ----------
@app.route("/webhook", methods=["POST"])
def webhook():
    data = request.get_json()
    if not data:
        return "ok"

    if "callback_query" in data:
        callback = data["callback_query"]
        chat_id = callback["message"]["chat"]["id"]
        data_cb = callback.get("data", "")
        # Отвечаем на callback
        requests.post(
            f"https://api.telegram.org/bot{TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback["id"]}
        )

        if data_cb == "menu":
            send_main_menu(chat_id)
        elif data_cb == "new_task":
            set_pending_task(chat_id, "query")
            send_telegram(chat_id,
                          "📝 Введите поисковый запрос, по которому нужно отслеживать новости или проверять факт.\nДля отмены – /cancel")
        elif data_cb == "feedback":
            set_pending_task(chat_id, "feedback")
            send_telegram(chat_id,
                          "📝 Пожалуйста, напишите ваше сообщение (предложение, проблему, отзыв).\nДля отмены – /cancel")
        elif data_cb == "tasks":
            send_tasks_inline(chat_id)
        elif data_cb.startswith("tasks_page_"):
            page = int(data_cb.split("_")[-1])
            send_tasks_inline(chat_id, page)
        elif data_cb == "confirm_interval_yes":
            pending = get_pending_task(chat_id)
            if pending and pending.get("step") == "confirm_interval":
                query = pending.get("query")
                interval = pending.get("interval")
                delete_pending_task(chat_id)
                if query and interval:
                    fake_msg = {
                        "chat": {"id": chat_id},
                        "from": callback.get("from", {})
                    }
                    create_task_and_check(chat_id, query, interval, fake_msg)
                else:
                    send_telegram(chat_id, "Ошибка: не удалось создать задачу.")
            else:
                send_telegram(chat_id, "Действие устарело.")
        elif data_cb == "confirm_interval_no":
            pending = get_pending_task(chat_id)
            if pending and pending.get("step") == "confirm_interval":
                query = pending.get("query")
                set_pending_task(chat_id, "interval", query=query)
                send_telegram(chat_id, "Введите интервал проверки (например, 5 minutes, 1 hour, daily).")
        elif data_cb.startswith("task_"):
            task_id = data_cb.split("_", 1)[1]
            send_task_actions(chat_id, task_id)
        elif data_cb.startswith("pause_"):
            task_id = data_cb.split("_", 1)[1]
            info = get_task_info(task_id)
            if info and info.get("chat_id") == str(chat_id):
                set_task_paused(task_id, True)
                send_telegram(chat_id, f"⏸ Задача #{task_id} поставлена на паузу.")
                send_task_actions(chat_id, task_id)
            else:
                send_telegram(chat_id, "Задача не найдена или не ваша.")
        elif data_cb.startswith("resume_"):
            task_id = data_cb.split("_", 1)[1]
            info = get_task_info(task_id)
            if info and info.get("chat_id") == str(chat_id):
                set_task_paused(task_id, False)
                send_telegram(chat_id, f"▶ Задача #{task_id} возобновлена.")
                send_task_actions(chat_id, task_id)
            else:
                send_telegram(chat_id, "Задача не найдена или не ваша.")
        elif data_cb.startswith("delete_"):
            task_id = data_cb.split("_", 1)[1]
            info = get_task_info(task_id)
            if info and info.get("chat_id") == str(chat_id):
                delete_task(task_id, chat_id)
                send_telegram(chat_id, f"🗑 Задача #{task_id} удалена.")
                send_tasks_inline(chat_id)
            else:
                send_telegram(chat_id, "Задача не найдена или не ваша.")
        return "ok"

    # Обычное сообщение
    if "message" in data:
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
        logger.info("Webhook set:", resp.json())

if __name__ == "__main__":
    if os.environ.get("RENDER"):
        logger.info("Running on Render, performing legacy task migration...")
        migrate_legacy_tasks(ADMIN_CHAT_ID)
    else:
        logger.info("Running locally, skipping legacy task migration (no Redis access assumed).")
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))