import logging

from affiliate import build_promo_block
from shared import (
    ADMIN_CHAT_ID, send_telegram,
    get_next_task_id, save_task, delete_task,
    get_user_task_ids, get_task_info, set_task_paused,
    parse_duration,
    update_last_run, check_deepseek,
    now_msk, format_msk, set_pending_task,
    get_task_list_state, set_task_list_state,
)

logger = logging.getLogger(__name__)


def create_task_and_check(chat_id, query, interval, msg):
    """
    Создаёт задачу, выполняет немедленную проверку, отправляет уведомление
    пользователю и (если нужно) админу. Возвращает ничего.
    """
    task_id = get_next_task_id()
    save_task(task_id, query, interval, chat_id)

    logger.info(f"Performing immediate check for new task {task_id}...")
    found, news = check_deepseek(query)
    now_iso = now_msk().isoformat()

    if found:
        partner_block, don_msg, partner_reply_markup = build_promo_block(query)
        send_telegram(
            chat_id,
            f"🔔 <b>Сразу нашлась новость по задаче #{task_id}:</b>\n{news}\n\n⏸ Задача поставлена на паузу.{don_msg}{partner_block}",
            reply_markup=partner_reply_markup
        )
        set_task_paused(task_id, True)
    else:
        send_telegram(
            chat_id,
            f"✅ Задача #{task_id} создана.\nЗапрос: {query}\nИнтервал: {interval} мин.\n\n"
            f"🔍 Сейчас по запросу ничего не найдено. Я продолжу проверять каждые {interval} мин. и пришлю уведомление, когда появится новость."
        )

    update_last_run(task_id, now_iso)

    # Уведомление админу, если создал не он сам
    if chat_id != ADMIN_CHAT_ID:
        user = msg.get("from", {})
        username = user.get("username", user.get("first_name", str(chat_id)))
        send_telegram(
            ADMIN_CHAT_ID,
            f"👤 Пользователь @{username} (chat_id {chat_id}) создал задачу #{task_id}:\n{query[:200]}"
        )


def handle_newtask(chat_id, text, msg):
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
    create_task_and_check(chat_id, query, interval, msg)


def handle_tasks(chat_id):
    ids = get_user_task_ids(chat_id)
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


def handle_taskinfo(chat_id, text):
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


def handle_deletetask(chat_id, text):
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
    delete_task(tid, chat_id)
    send_telegram(chat_id, f"🗑 Задача #{tid} удалена.")


def handle_pause(chat_id, text):
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


def handle_resume(chat_id, text):
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


def send_tasks_inline(chat_id, page=0, filter_state=None, search=None):
    """Отправляет список задач пользователя с пагинацией, фильтром и поиском."""
    state = get_task_list_state(chat_id)
    if filter_state is None:
        filter_state = state.get("filter", "all")
    if search is None:
        search = state.get("search", "")

    # Сохраняем состояние фильтра и поиска
    set_task_list_state(chat_id, filter_state, search)

    ids = get_user_task_ids(chat_id)
    if not ids:
        send_telegram(chat_id, "У вас нет задач.")
        return

    # Отбираем и сортируем задачи по фильтру и поисковому запросу
    filtered = []
    for tid in ids:
        info = get_task_info(tid)
        if not info:
            continue
        paused = info.get("paused") == "1"
        if filter_state == "active" and paused:
            continue
        if filter_state == "paused" and not paused:
            continue
        query_lower = info.get("query", "").lower()
        if search and search.lower() not in query_lower:
            continue
        filtered.append((tid, info, paused))

    # Сортируем от новых к старым (по убыванию ID)
    filtered.sort(key=lambda x: int(x[0]), reverse=True)

    page_size = 10
    total_pages = max(1, (len(filtered) + page_size - 1) // page_size)

    # Корректируем страницу, если вышла за границы
    if page < 0:
        page = 0
    elif page >= total_pages:
        page = total_pages - 1

    start = page * page_size
    end = start + page_size
    page_items = filtered[start:end]

    keyboard = {"inline_keyboard": []}

    for tid, info, paused in page_items:
        status_icon = "⏸" if paused else "▶"
        q = info.get("query", "")[:30]
        interval = info.get("interval", "?")
        button_text = f"{status_icon} #{tid} {q}... ({interval} мин)"
        keyboard["inline_keyboard"].append(
            [{"text": button_text, "callback_data": f"task_{tid}"}]
        )

    # Кнопки фильтрации
    filter_row = []
    if filter_state == "active":
        filter_row.append({"text": "✅ Все", "callback_data": "tasks_filter_all"})
        filter_row.append({"text": "⏸ На паузе", "callback_data": "tasks_filter_paused"})
    elif filter_state == "paused":
        filter_row.append({"text": "✅ Все", "callback_data": "tasks_filter_all"})
        filter_row.append({"text": "▶ Активные", "callback_data": "tasks_filter_active"})
    else:
        filter_row.append({"text": "▶ Активные", "callback_data": "tasks_filter_active"})
        filter_row.append({"text": "⏸ На паузе", "callback_data": "tasks_filter_paused"})
    keyboard["inline_keyboard"].append(filter_row)

    # Кнопки поиска
    search_label = "🔍 Поиск" if not search else f"🔍 Поиск: {search[:15]}"
    keyboard["inline_keyboard"].append(
        [{"text": search_label, "callback_data": "tasks_search"}]
    )
    if search:
        keyboard["inline_keyboard"].append(
            [{"text": "✖ Сбросить поиск", "callback_data": "tasks_search_clear"}]
        )

    # Навигационные кнопки
    nav_row = []
    if page > 0:
        nav_row.append({"text": "⬅️ Назад", "callback_data": f"tasks_page_{page-1}"})
    if page < total_pages - 1:
        nav_row.append({"text": "➡️ Вперёд", "callback_data": f"tasks_page_{page+1}"})
    if nav_row:
        keyboard["inline_keyboard"].append(nav_row)

    # Кнопка главного меню
    keyboard["inline_keyboard"].append(
        [{"text": "🏠 Главное меню", "callback_data": "menu"}]
    )

    # Заголовок
    if not filtered:
        title = "📋 <b>Ничего не найдено.</b>"
    else:
        title = "📋 <b>Ваши задачи</b>"
        if filter_state == "active":
            title += " — активные"
        elif filter_state == "paused":
            title += " — на паузе"
        if search:
            title += f" (поиск: {search})"

    send_telegram(
        chat_id,
        f"{title}\nСтраница {page+1}/{total_pages}.",
        parse_mode="HTML",
        reply_markup=keyboard
    )


def send_task_actions(chat_id, task_id):
    """Показывает информацию о задаче и кнопки действий."""
    info = get_task_info(task_id)
    if not info:
        send_telegram(chat_id, "Задача не найдена.")
        return

    paused = info.get("paused") == "1"
    status = "⏸ на паузе" if paused else "▶ активна"
    text = (
        f"<b>Задача #{task_id}</b>\n"
        f"Запрос: {info['query']}\n"
        f"Интервал: {info['interval']} мин\n"
        f"Статус: {status}"
    )

    keyboard = {"inline_keyboard": []}
    if paused:
        keyboard["inline_keyboard"].append(
            [{"text": "▶ Возобновить", "callback_data": f"resume_{task_id}"}]
        )
    else:
        keyboard["inline_keyboard"].append(
            [{"text": "⏸ Пауза", "callback_data": f"pause_{task_id}"}]
        )
    keyboard["inline_keyboard"].append(
        [{"text": "🗑 Удалить", "callback_data": f"delete_{task_id}"}]
    )
    keyboard["inline_keyboard"].append(
        [{"text": "🔙 Назад к списку", "callback_data": "tasks"}]
    )

    send_telegram(chat_id, text, parse_mode="HTML", reply_markup=keyboard)