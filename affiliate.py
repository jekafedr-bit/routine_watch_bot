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

import base64

def get_admitad_access_token():
    """Получает access token для Admitad API (кэширует на 50 минут)."""
    global _admitad_token, _admitad_token_exp

    if _admitad_token and _admitad_token_exp and datetime.datetime.now() < _admitad_token_exp:
        return _admitad_token

    client_id = os.environ.get("ADMITAD_CLIENT_ID")
    client_secret = os.environ.get("ADMITAD_CLIENT_SECRET")
    if not client_id or not client_secret:
        logger.warning("ADMITAD_CLIENT_ID or ADMITAD_CLIENT_SECRET not set")
        return None

    # Формируем Basic Auth: base64(client_id:client_secret)
    credentials = f"{client_id}:{client_secret}"
    encoded = base64.b64encode(credentials.encode()).decode()
    headers = {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/x-www-form-urlencoded"
    }

    # Тело запроса: grant_type=client_credentials, client_id и scope
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "scope": "advcampaigns banners websites"  # минимальный набор прав
    }

    try:
        resp = requests.post(ADMITAD_TOKEN_URL, headers=headers, data=data, timeout=15)
        if resp.status_code == 200:
            token_data = resp.json()
            _admitad_token = token_data["access_token"]
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