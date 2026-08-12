import os
import base64
import datetime
import requests
import logging

logger = logging.getLogger(__name__)

ADMITAD_TOKEN_URL = "https://api.admitad.com/token/"
ADMITAD_WEBSITES_URL = "https://api.admitad.com/websites/v2/"
ADMITAD_WEBSITE_PROGRAMS_URL = "https://api.admitad.com/advcampaigns/website/{website_id}/"

_admitad_token = None
_admitad_token_exp = None
_website_id = None
_joined_programs_cache = None
_joined_programs_exp = None


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

    credentials = f"{client_id}:{client_secret}"
    encoded = base64.b64encode(credentials.encode()).decode()
    headers = {
        "Authorization": f"Basic {encoded}",
        "Content-Type": "application/x-www-form-urlencoded"
    }
    data = {
        "grant_type": "client_credentials",
        "client_id": client_id,
        "scope": "websites advcampaigns_for_website"
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


def get_website_id():
    """Возвращает ID площадки routine_watch_bot (или первой активной)."""
    global _website_id

    if _website_id:
        return _website_id

    # Если задана переменная окружения, используем её
    env_website_id = os.environ.get("ADMITAD_WEBSITE_ID")
    if env_website_id:
        _website_id = int(env_website_id)
        logger.info(f"Using ADMITAD_WEBSITE_ID={_website_id}")
        return _website_id

    token = get_admitad_access_token()
    if not token:
        return None

    headers = {"Authorization": f"Bearer {token}"}
    try:
        resp = requests.get(ADMITAD_WEBSITES_URL, headers=headers, timeout=15)
        if resp.status_code == 200:
            websites = resp.json()
            # Ищем по имени или берём первый активный
            target = None
            for site in websites:
                if site.get("name", "").lower() == "routine_watch_bot":
                    target = site
                    break
            if not target:
                for site in websites:
                    if site.get("status", "").lower() == "active":
                        target = site
                        break
            if target:
                _website_id = int(target["id"])
                logger.info(f"Found website_id={_website_id} (name={target.get('name')})")
                return _website_id
            else:
                logger.warning("No active websites found")
        else:
            logger.warning(f"Failed to get websites: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"Get websites exception: {e}")
    return None


def get_joined_programs():
    """Возвращает список подключённых программ (кэш 1 час)."""
    global _joined_programs_cache, _joined_programs_exp

    if _joined_programs_cache and _joined_programs_exp and datetime.datetime.now() < _joined_programs_exp:
        return _joined_programs_cache

    token = get_admitad_access_token()
    if not token:
        return []

    website_id = get_website_id()
    if not website_id:
        return []

    url = ADMITAD_WEBSITE_PROGRAMS_URL.format(website_id=website_id)
    headers = {"Authorization": f"Bearer {token}"}
    params = {"limit": 100}

    try:
        resp = requests.get(url, headers=headers, params=params, timeout=15)
        if resp.status_code == 200:
            data = resp.json()
            programs = data.get("results", [])
            logger.info(f"Fetched {len(programs)} joined programs")
            _joined_programs_cache = programs
            _joined_programs_exp = datetime.datetime.now() + datetime.timedelta(seconds=3600)
            return programs
        else:
            logger.warning(f"Failed to get joined programs: {resp.status_code} {resp.text}")
    except Exception as e:
        logger.warning(f"Get joined programs exception: {e}")
    return []


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
                keywords = [k.strip() for k in answer.split(",") if k.strip()]
                logger.info(f"DeepSeek extracted keywords: {keywords}")
                return keywords
    except Exception as e:
        logger.warning(f"DeepSeek keyword extraction failed: {e}")
    return []


def fetch_admitad_link(query):
    """Ищет партнёрскую программу по ключевым словам (локально среди подключённых)."""
    keywords = extract_keywords_via_deepseek(query)
    if not keywords:
        # fallback: разбиваем запрос на слова
        keywords = [w for w in query.split() if len(w) > 2]

    programs = get_joined_programs()
    if not programs:
        logger.warning("No joined programs available")
        return None

    for kw in keywords:
        kw_lower = kw.lower()
        for prog in programs:
            name = prog.get("name", "").lower()
            categories = prog.get("categories", [])
            if kw_lower in name:
                goto = prog.get("goto_link", "")
                if goto:
                    logger.info(f"Found affiliate program by keyword '{kw}': {prog.get('name')}")
                    return prog.get("name"), goto
            # проверка в категориях
            for cat in categories:
                if kw_lower in cat.get("name", "").lower():
                    goto = prog.get("goto_link", "")
                    if goto:
                        logger.info(f"Found affiliate program by category keyword '{kw}': {prog.get('name')}")
                        return prog.get("name"), goto
    return None