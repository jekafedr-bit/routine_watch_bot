import os
import datetime
import requests
import logging

logger = logging.getLogger(__name__)

ADMITAD_TOKEN_URL = "https://api.admitad.com/token/"
ADMITAD_PROGRAMS_URL = "https://api.admitad.com/programs/"

# Кэш токена
_admitad_token = None
_admitad_token_exp = None


def get_admitad_access_token():
    """Получает access token для Admitad API (кэширует на 50 минут)."""
    global _admitad_token, _admitad_token_exp

    if _admitad_token and _admitad_token_exp and datetime.datetime.now() < _admitad_token_exp:
        return _admitad_token

    basic_auth = os.environ.get("ADMITAD_BASIC_AUTH")
    if not basic_auth:
        logger.warning("ADMITAD_BASIC_AUTH not set")
        return None

    # Логируем только длину и первые символы для проверки корректности
    logger.info(f"ADMITAD_BASIC_AUTH length: {len(basic_auth)}, prefix: {basic_auth[:10]}...")

    headers = {
        "Authorization": basic_auth,
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {"grant_type": "client_credentials"}

    try:
        resp = requests.post(ADMITAD_TOKEN_URL, headers=headers, data=data, timeout=15)
        if resp.status_code == 200:
            token_data = resp.json()
            _admitad_token = token_data["access_token"]
            # Обновляем за 10 минут до истечения (3600 секунд)
            _admitad_token_exp = datetime.datetime.now() + datetime.timedelta(seconds=3000)
            logger.info("Admitad access token obtained")
            return _admitad_token
        else:
            logger.warning(f"Failed to get Admitad token: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"Admitad token exception: {e}")
    return None


def fetch_admitad_link(keyword, limit=1):
    """
    Ищет партнёрскую программу по ключевому слову.
    Возвращает (название, ссылку) или None.
    """
    token = get_admitad_access_token()
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}"}
    params = {
        "q": keyword,
        "limit": limit,
        "has_tool": "true",          # только программы с готовыми партнёрскими ссылками
        "language": "ru",
        "connection_status": "active"  # только активные подключения
    }

    try:
        resp = requests.get(ADMITAD_PROGRAMS_URL, headers=headers, params=params, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            programs = data.get("results", [])
            if programs:
                prog = programs[0]
                name = prog.get("name", "Партнёр")
                goto = prog.get("goto_link", "")
                if goto:
                    logger.info(f"Found affiliate link for '{keyword}': {name} -> {goto[:60]}...")
                    return name, goto
        else:
            logger.warning(f"Admitad search error: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"Admitad search exception: {e}")
    return None