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

    if not client_id:
        logger.warning("ADMITAD_CLIENT_ID is not set")
    else:
        logger.info(f"ADMITAD_CLIENT_ID is set (length={len(client_id)}, prefix={client_id[:4]}...)")

    if not client_secret:
        logger.warning("ADMITAD_CLIENT_SECRET is not set")
    else:
        logger.info(f"ADMITAD_CLIENT_SECRET is set (length={len(client_secret)}, prefix={client_secret[:4]}...)")

    if not client_id or not client_secret:
        logger.warning("Admitad credentials incomplete")
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


def fetch_admitad_link(query, limit=1):
    token = get_admitad_access_token()
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}"}

    # Сначала пробуем ключевые слова от DeepSeek
    keywords = extract_keywords_via_deepseek(query)
    candidates = keywords + [query]  # добавляем исходный запрос как запасной вариант

    for candidate in candidates:
        if not candidate:
            continue
        params = {
            "q": candidate,
            "limit": limit,
            "has_tool": "true",
            "language": "ru",
            "connection_status": "active"
        }
        try:
            resp = requests.get(ADMITAD_PROGRAMS_URL, headers=headers, params=params, timeout=10)
            logger.info(
                f"Searching Admitad for '{candidate}': status {resp.status_code}, results: {len(resp.json().get('results', []))}")
            if resp.status_code == 200:
                data = resp.json()
                programs = data.get("results", [])
                if programs:
                    prog = programs[0]
                    name = prog.get("name", "Партнёр")
                    goto = prog.get("goto_link", "")
                    if goto:
                        logger.info(f"Found affiliate link for '{candidate}': {name} -> {goto[:60]}...")
                        return name, goto
            # Если 404 или пусто, пробуем следующего кандидата
        except Exception as e:
            logger.warning(f"Search exception for '{candidate}': {e}")
    return None

def extract_keywords_via_deepseek(query, max_keywords=3):
    """Извлекает ключевые слова для поиска партнёрской программы."""
    deepseek_key = os.environ.get("DEEPSEEK_API_KEY")
    if not deepseek_key:
        logger.warning("DEEPSEEK_API_KEY not set, fallback to simple extraction")
        return []

    headers = {
        "Authorization": f"Bearer {deepseek_key}",
        "Content-Type": "application/json"
    }
    payload = {
        "model": "deepseek-chat",
        "input": (
            f"Извлеки из запроса пользователя {max_keywords} ключевых слов или фраз, "
            "которые лучше всего подходят для поиска партнёрской программы в Admitad. "
            "Верни только ключевые слова через запятую, без пояснений.\n"
            f"Запрос: {query}"
        ),
        "temperature": 0.0,
        "max_output_tokens": 30
    }
    try:
        resp = requests.post("https://api.deepseek.com/v1/responses", headers=headers, json=payload, timeout=10)
        if resp.status_code == 200:
            data = resp.json()
            answer = ""
            for item in data.get("output", []):
                if item.get("type") == "message":
                    answer = item.get("content", [{}])[0].get("text", "").strip()
                    break
            if answer:
                # Разделяем по запятой и очищаем
                keywords = [k.strip() for k in answer.split(",") if k.strip()]
                logger.info(f"DeepSeek extracted keywords: {keywords}")
                return keywords
    except Exception as e:
        logger.warning(f"DeepSeek keyword extraction failed: {e}")
    return []