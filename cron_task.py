import datetime
from datetime import timedelta
import logging
logger = logging.getLogger(__name__)

from shared import (
    ADMIN_CHAT_ID, send_telegram,
    get_task_info, update_last_run, set_task_paused,
    get_all_task_ids, check_deepseek, get_donation_message,
    now_msk, format_msk
)


def process_due_tasks():
    now = now_msk()
    now_iso = now.isoformat()
    # Получаем ВСЕ задачи (из общего набора, т.к. нам нужно проверить каждого пользователя)
    all_task_ids = get_all_task_ids()   # эта функция осталась в shared (возвращает все ID из TASKS_SET)
    for tid in all_task_ids:
        info = get_task_info(tid)
        if not info or info.get("paused") == "1":
            continue
        last_run_str = info.get('last_run', '')
        if last_run_str:
            last_run_str = format_msk(last_run_str)
        else:
            last_run_str = '?'
        # и подставляем в f-строку
        if not last_run_str:
            last_run = now - timedelta(days=1)
        else:
            try:
                last_run = datetime.datetime.fromisoformat(last_run_str)
            except:
                last_run = now - timedelta(days=1)
        interval = int(info.get("interval", "1440"))
        if (now - last_run).total_seconds() >= interval * 60:
            logger.info(f"Checking task {tid}")
            found, news = check_deepseek(info["query"])
            owner_chat_id = int(info.get("chat_id", ADMIN_CHAT_ID))
            if found:
                don_msg = get_donation_message()
                msg_time = now.strftime("%d.%m.%Y %H:%M (МСК)")
                send_telegram(owner_chat_id,
                              f"🔔 <b>Новость по задаче #{tid}</b>\n{news}\n\n⏸ Задача #{tid} автоматически поставлена на паузу.{don_msg}"
                              f"\n\nПроверено: {msg_time}")
                set_task_paused(tid, True)
            update_last_run(tid, now_iso)

if __name__ == "__main__":
    process_due_tasks()