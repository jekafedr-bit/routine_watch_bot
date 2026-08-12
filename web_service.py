import os

import requests
from flask import Flask, request
import logging

from affiliate import fetch_admitad_link, build_promo_block

logger = logging.getLogger(__name__)
from shared import (
    TOKEN, ADMIN_CHAT_ID, send_telegram,
    get_next_task_id, save_task, delete_task,
    get_user_task_ids, get_task_info, set_task_paused,
    parse_duration, migrate_legacy_tasks,
    update_last_run, check_deepseek, pause_user_tasks,
    get_donation_message,
    set_pending_task, get_pending_task, delete_pending_task, now_msk, format_msk  # ← новые
)
from user_management import (
    is_allowed, notify_admin_unauthorized,
    add_dynamic_user, remove_dynamic_user,
    reset_unauthorized_notify, get_all_allowed_users
)

app = Flask(__name__)

# ---------- Обработка команд ----------
def handle_message(msg):
    chat_id = msg["chat"]["id"]
    text = msg.get("text", "").strip()

    # ── /start доступен всем ──
    if text.startswith("/start"):
        # Клавиатура для обычного авторизованного пользователя
        user_keyboard = {
            "keyboard": [
                ["/newtask", "/tasks"],
                ["/taskinfo", "/pause"],
                ["/resume", "/deletetask"]
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False
        }
        # Клавиатура для админа (добавлены кнопки управления пользователями)
        admin_keyboard = {
            "keyboard": [
                ["/newtask", "/tasks"],
                ["/taskinfo", "/pause"],
                ["/resume", "/deletetask"],
                ["/adduser", "/removeuser"],
                ["/notify_update"]  # <-- новая кнопка
            ],
            "resize_keyboard": True,
            "one_time_keyboard": False
        }

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
                    "/newtask &lt;запрос&gt; /every &lt;интервал&gt; — создать задачу\n"
                    "/tasks — список ваших задач\n"
                    "/taskinfo &lt;ID&gt; — инфо о любой задаче\n"
                    "/deletetask &lt;ID&gt; — удалить свою задачу\n"
                    "/pause &lt;ID&gt; /resume &lt;ID&gt; — пауза/продолжить\n"
                    "/adduser &lt;chat_id&gt; — дать доступ пользователю\n"
                    "/removeuser &lt;chat_id&gt; — отозвать доступ (задачи пользователя встанут на паузу)\n\n"
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
                    "/newtask &lt;запрос&gt; /every &lt;интервал&gt; — создать задачу\n"
                    "/tasks — список ваших задач\n"
                    "/taskinfo &lt;ID&gt; — инфо о задаче\n"
                    "/deletetask &lt;ID&gt; — удалить задачу\n"
                    "/pause &lt;ID&gt; /resume &lt;ID&gt; — пауза/продолжить"
                ), parse_mode="HTML", reply_markup=user_keyboard)
        else:
            # неавторизованный пользователь – приветствие без клавиатуры
            send_telegram(chat_id,
                          "🤖 <b>Бот для отслеживания новостей</b>\n\n"
                          "Я помогаю следить за появлением новостей и фактов по вашим запросам.\n"
                          "Доступ предоставляется администратором.\n\n"
                          "Ваш запрос на доступ отправлен. Когда администратор одобрит заявку, вы получите уведомление."
                          , parse_mode="HTML")
            # отправляем уведомление администратору
            user_info = msg.get("from", {})
            notify_admin_unauthorized(chat_id, user_info)
        return

    # ── Обработка ожидания ввода (пошаговое создание задачи) ──
    pending = get_pending_task(chat_id)
    if pending:
        if text.startswith("/cancel"):
            delete_pending_task(chat_id)
            send_telegram(chat_id, "❌ Создание задачи отменено.")
            return
        step = pending.get("step")
        if step == "query":
            # Пользователь вводит поисковый запрос
            if not text or text.startswith("/"):
                send_telegram(chat_id, "📝 Пожалуйста, введите поисковый запрос. Или /cancel для отмены.")
                return
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
            # Создаём задачу
            task_id = get_next_task_id()
            save_task(task_id, query, interval, chat_id)
            delete_pending_task(chat_id)
            logger.info(f"Performing immediate check for new task {task_id}...")
            found, news = check_deepseek(query)
            now_iso = now_msk().isoformat()
            if found:
                partner_block, don_msg = build_promo_block(query)

                send_telegram(chat_id,
                              f"🔔 <b>Сразу нашлась новость по задаче #{task_id}:</b>\n{news}\n\n⏸ Задача поставлена на паузу.{don_msg}{partner_block}")
                set_task_paused(task_id, True)
            else:
                send_telegram(chat_id,
                              f"✅ Задача #{task_id} создана.\nЗапрос: {query}\nИнтервал: {interval} мин.\n\n"
                              f"🔍 Сейчас по запросу ничего не найдено. Я продолжу проверять каждые {interval} мин. и пришлю уведомление, когда появится новость.")
            update_last_run(task_id, now_iso)
            if chat_id != ADMIN_CHAT_ID:
                user = msg.get("from", {})
                username = user.get("username", user.get("first_name", str(chat_id)))
                send_telegram(ADMIN_CHAT_ID,
                              f"👤 Пользователь @{username} (chat_id {chat_id}) создал задачу #{task_id}:\n{query[:200]}")
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

    elif text.startswith("/newtask"):
        # Если команда в точности "/newtask" (нажата кнопка) — запускаем пошаговый опрос
        if text.strip() == "/newtask":
            set_pending_task(chat_id, "query")
            send_telegram(chat_id,
                          "📝 Введите поисковый запрос, по которому нужно отслеживать новости или проверять факт.\nДля отмены – /cancel")
            return
        # Иначе пробуем разобрать как команду с /every
        if "/every" not in text:
            send_telegram(chat_id,
                "❗ Укажите интервал проверки с помощью /every.\n"
                "Пример: /newtask снижение цен на билеты /every 1 day")
            return
        parts = text.split("/every", 1)
        query = parts[0].replace("/newtask", "", 1).strip()
        if not query:
            send_telegram(chat_id, "❗ Укажите запрос для отслеживания.")
            return
        dur_str = parts[1].strip()
        interval = parse_duration(dur_str)
        if interval is None:
            send_telegram(chat_id, "❗ Не понял интервал. Используйте: 5 minutes, 1 hour, daily")
            return
        if interval < 5:
            send_telegram(chat_id, "❗ Минимальный интервал — 5 минут.")
            return
        task_id = get_next_task_id()
        save_task(task_id, query, interval, chat_id)

        # Немедленная проверка
        found, news = check_deepseek(query)
        now_iso = now_msk().isoformat()
        if found:
            partner_block, don_msg = build_promo_block(query)

            send_telegram(chat_id,
                          f"🔔 <b>Сразу нашлась новость по задаче #{task_id}:</b>\n{news}\n\n⏸ Задача поставлена на паузу.{don_msg}{partner_block}")
            set_task_paused(task_id, True)
        else:
            # Ничего не найдено — сообщаем и говорим о повторной проверке
            send_telegram(chat_id,
                          f"✅ Задача #{task_id} создана.\nЗапрос: {query}\nИнтервал: {interval} мин.\n\n"
                          f"🔍 Сейчас по запросу ничего не найдено. Я продолжу проверять каждые {interval} мин. и пришлю уведомление, когда появится новость.")
        update_last_run(task_id, now_iso)

        # Уведомление админу, если задачу создал не он сам
        if chat_id != ADMIN_CHAT_ID:
            user = msg.get("from", {})
            username = user.get("username", user.get("first_name", str(chat_id)))
            send_telegram(
                ADMIN_CHAT_ID,
                f"👤 Пользователь @{username} (chat_id {chat_id}) создал задачу #{task_id}:\n{query[:200]}"
            )

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
        # Админ может смотреть любую задачу, остальные — только свои
        if info.get("chat_id") != str(chat_id) and chat_id != ADMIN_CHAT_ID:
            send_telegram(chat_id, "Это не ваша задача.")
            return
        paused = "Да" if info.get("paused") == "1" else "Нет"
        owner = info.get("chat_id", "неизвестно")
        last_run_display = format_msk(info.get('last_run', '')) if info.get('last_run') else '?'
        send_telegram(chat_id,
            f"<b>Задача #{tid}</b>\n"
            f"Владелец: {owner}\n"
            f"Запрос: {info['query']}\n"
            f"Интервал: {info['interval']} мин\n"
            f"Пауза: {paused}\n"
            f"Последний запуск: {last_run_display}",
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

    elif text.startswith("/adduser") and chat_id == ADMIN_CHAT_ID:
        parts = text.split()
        if len(parts) < 2:
            send_telegram(chat_id, "Укажите chat_id пользователя, например /adduser 123456789")
            return
        try:
            new_user_id = int(parts[1])
        except ValueError:
            send_telegram(chat_id, "Неверный формат ID.")
            return
        add_dynamic_user(new_user_id)
        send_telegram(chat_id, f"✅ Пользователь {new_user_id} добавлен в динамический доступ.")
        # Уведомим самого пользователя, если бот может ему написать (необязательно)
        try:
            send_telegram(new_user_id, "✅ Администратор предоставил вам доступ к боту. Можете начинать работу.")
        except:
            pass

    elif text.startswith("/removeuser") and chat_id == ADMIN_CHAT_ID:
        parts = text.split()
        if len(parts) < 2:
            send_telegram(chat_id, "Укажите chat_id пользователя, например /removeuser 123456789")
            return
        try:
            user_id = int(parts[1])
        except ValueError:
            send_telegram(chat_id, "Неверный формат ID.")
            return
        remove_dynamic_user(user_id)
        pause_user_tasks(user_id)
        reset_unauthorized_notify(user_id)
        send_telegram(chat_id, f"✅ Пользователь {user_id} удалён из доступа, все его задачи остановлены.")

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
        logger.info("Webhook set:", resp.json())

if __name__ == "__main__":
    if os.environ.get("RENDER"):
        logger.info("Running on Render, performing legacy task migration...")
        migrate_legacy_tasks(ADMIN_CHAT_ID)
    else:
        logger.info("Running locally, skipping legacy task migration (no Redis access assumed).")
    set_webhook()
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", 10000)))