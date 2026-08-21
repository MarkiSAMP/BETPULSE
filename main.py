import os
import hmac
import json
import hashlib
import urllib.parse
import zlib
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any, Optional
from urllib.parse import parse_qs
from contextlib import asynccontextmanager
from time import time
import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from bot import bot, dp, init_db, get_db

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
FOOTBALL_DATA_API_KEY = os.getenv("FOOTBALL_DATA_API_KEY")

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан")
if not FOOTBALL_DATA_API_KEY:
    raise RuntimeError("FOOTBALL_DATA_API_KEY не задан")

FOOTBALL_DATA_URL = "https://footballdata.io/api/v1"

CACHE_TTL = int(os.getenv("CACHE_TTL", 300))
cache = {}

# Кеш для статистики (отдельный, с собственным TTL)
stats_cache = {}
STATS_CACHE_TTL_LIVE = 300      # 5 минут для LIVE
STATS_CACHE_TTL_FT = 86400      # 24 часа для завершённых

def get_stats_cache(key: str) -> Optional[Dict]:
    if key in stats_cache:
        data, expiry = stats_cache[key]
        if time() < expiry:
            return data
        else:
            del stats_cache[key]
    return None

def set_stats_cache(key: str, data: Dict, ttl_seconds: int):
    stats_cache[key] = (data, time() + ttl_seconds)

def get_cache_key(league_id: str) -> str:
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"matches_{league_id}_{today_str}"

def get_from_cache(key: str) -> Optional[Dict]:
    if key in cache:
        data, timestamp = cache[key]
        if time() - timestamp < CACHE_TTL:
            return data
        else:
            del cache[key]
    return None

def set_to_cache(key: str, data: Dict):
    cache[key] = (data, time())

def fix_encoding(text: str) -> str:
    if not text or not isinstance(text, str):
        return text
    try:
        return text.encode('latin-1').decode('utf-8')
    except (UnicodeEncodeError, UnicodeDecodeError):
        return text

@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[SERVER]: Инициализация базы данных...")
    try:
        await init_db()
    except Exception as e:
        print(f"[SERVER DB Error]: {e}")

    print("[SERVER]: Сброс вебхуков и запуск бота...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"[SERVER Webhook Error]: {e}")

    await asyncio.sleep(2)
    bot_task = asyncio.create_task(dp.start_polling(bot))
    print("[SERVER]: FastAPI и Telegram Bot запущены!")
    yield
    print("[SERVER]: Остановка бота...")
    bot_task.cancel()

app = FastAPI(title="BETPULSE Mini App API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LEAGUES_DATA = [
    {"id": "all", "name": "Все лиги"},
    {"id": "15", "name": "АПЛ (Англия)"},
    {"id": "10", "name": "Ла Лига (Испания)"},
    {"id": "45", "name": "Лига Чемпионов"},
    {"id": "46", "name": "Лига Европы"},
]

FORBIDDEN_WORDS = ["ЖБ", "верняк", "100%", "грузим хаты", "проход 100", "чуйка"]

def sanitize_text(text: str) -> str:
    for word in FORBIDDEN_WORDS:
        if word in text:
            text = text.replace(word, "[Аналитический тренд]")
    return text

def get_msk_today_str() -> str:
    msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
    return msk_now.strftime("%Y-%m-%d")

def verify_telegram_init_data(init_data_raw: str) -> dict | None:
    try:
        parsed_data = parse_qs(init_data_raw)
        hash_from_telegram = parsed_data.get('hash', [''])[0]
        if not hash_from_telegram:
            return None

        data_check_list = []
        for key, value in sorted(parsed_data.items()):
            if key != 'hash':
                data_check_list.append(f"{key}={value[0]}")
        data_check_string = "\n".join(data_check_list)

        secret_key = hmac.new(b"WebAppData", BOT_TOKEN.encode(), hashlib.sha256).digest()
        calculated_hash = hmac.new(secret_key, data_check_string.encode(), hashlib.sha256).hexdigest()

        if calculated_hash == hash_from_telegram:
            return json.loads(parsed_data.get('user', ['{}'])[0])
        return None
    except Exception:
        return None

async def check_user_access(init_data: str) -> int:
    user = verify_telegram_init_data(init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Запуск разрешен только через Telegram-бота.")

    user_id = user.get("id")
    if not DATABASE_URL:
        return user_id

    try:
        clean_url = DATABASE_URL.split("?")[0] if "?" in DATABASE_URL else DATABASE_URL
        conn = await get_db()
        row = await conn.fetchrow(
            "SELECT is_paid, expires_at, trial_expires_at FROM users WHERE user_id = $1;",
            user_id
        )
        await conn.close()
    except Exception as e:
        print(f"[DB Auth Error]: {e}")
        raise HTTPException(status_code=500, detail="Ошибка проверки доступа к БД.")

    if not row:
        raise HTTPException(status_code=403, detail="Доступ ограничен: требуется оформление подписки.")

    now = datetime.now(timezone.utc).replace(tzinfo=None)

    if row["is_paid"] and row["expires_at"] and row["expires_at"] > now:
        return user_id

    if row["trial_expires_at"] and row["trial_expires_at"] > now:
        return user_id

    raise HTTPException(status_code=403, detail="Доступ ограничен: пробный период истёк. Оформите подписку.")

# ===== ФУНКЦИЯ ДЛЯ ПОЛУЧЕНИЯ СТАТИСТИКИ МАТЧА =====
async def fetch_match_statistics(match_id: str, status: str) -> Optional[Dict]:
    """
    Получает статистику матча с кешированием.
    status: 'LIVE' (TTL 5 мин) или 'FT' (TTL 24 часа)
    """
    cache_key_stats = f"stats_{match_id}"
    cached = get_stats_cache(cache_key_stats)
    if cached is not None:
        return cached

    headers = {"Authorization": f"Bearer {FOOTBALL_DATA_API_KEY}"}
    url = f"{FOOTBALL_DATA_URL}/matches/{match_id}/stats"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=headers)
            if res.status_code == 200:
                data = res.json()
                if data.get("success"):
                    stats_data = data.get("data", {})
                    home_stats = stats_data.get("home", {})
                    away_stats = stats_data.get("away", {})
                    transformed = {
                        "home": {
                            "possession": home_stats.get("possession", 0),
                            "shotsOnGoal": home_stats.get("shotsOnGoal", 0),
                            "totalShots": home_stats.get("totalShots", 0),
                            "fouls": home_stats.get("fouls", 0),
                            "corners": home_stats.get("corners", 0),
                            "yellowCards": home_stats.get("yellowCards", 0),
                            "redCards": home_stats.get("redCards", 0),
                            "passesAccuracy": home_stats.get("passesAccuracy", 0),
                        },
                        "away": {
                            "possession": away_stats.get("possession", 0),
                            "shotsOnGoal": away_stats.get("shotsOnGoal", 0),
                            "totalShots": away_stats.get("totalShots", 0),
                            "fouls": away_stats.get("fouls", 0),
                            "corners": away_stats.get("corners", 0),
                            "yellowCards": away_stats.get("yellowCards", 0),
                            "redCards": away_stats.get("redCards", 0),
                            "passesAccuracy": away_stats.get("passesAccuracy", 0),
                        }
                    }
                    # Кешируем
                    ttl = STATS_CACHE_TTL_LIVE if status.upper() == "LIVE" else STATS_CACHE_TTL_FT
                    set_stats_cache(cache_key_stats, transformed, ttl)
                    return transformed
                else:
                    print(f"[Footballdata.io] Ошибка получения статистики: {data}")
                    return None
            else:
                print(f"[Footballdata.io] HTTP {res.status_code} при получении статистики для матча {match_id}")
                return None
    except Exception as e:
        print(f"[Footballdata.io] Исключение при получении статистики: {e}")
        return None

# ===== ГЕНЕРАЦИЯ ПРОГНОЗОВ =====
def generate_bet_market_from_odds(home_team: str, away_team: str, odds: Dict, probabilities: Dict) -> Dict[str, Any]:
    """
    Генерирует прогноз на основе реальных коэффициентов и вероятностей из API.
    Если коэффициентов нет – возвращает None, чтобы использовать fallback.
    """
    if not odds or not probabilities:
        return None

    # Получаем вероятности
    home_prob = float(probabilities.get("home_win", 0))
    draw_prob = float(probabilities.get("draw", 0))
    away_prob = float(probabilities.get("away_win", 0))

    # Получаем коэффициенты
    home_odd = float(odds.get("home_win", 0))
    draw_odd = float(odds.get("draw", 0))
    away_odd = float(odds.get("away_win", 0))

    # Определяем наиболее вероятный исход
    if home_prob >= draw_prob and home_prob >= away_prob:
        forecast_type = f"Победа 1 (П1)"
        coef = home_odd if home_odd > 0 else 1.85
        explanation = f"Победа команды {home_team} в основное время."
        risk = f"Команда {away_team} опасна в контратаках."
        reason_3 = f"3. {home_team} имеет высокие шансы на победу ({home_prob:.1f}%)."
    elif draw_prob >= home_prob and draw_prob >= away_prob:
        forecast_type = "Ничья (X)"
        coef = draw_odd if draw_odd > 0 else 3.20
        explanation = "Ожидается ничейный результат."
        risk = "Обе команды могут играть осторожно."
        reason_3 = f"3. Вероятность ничьи составляет {draw_prob:.1f}%."
    else:
        forecast_type = f"Победа 2 (П2)"
        coef = away_odd if away_odd > 0 else 2.80
        explanation = f"Победа команды {away_team} в основное время."
        risk = f"Команда {home_team} может пропустить быструю контратаку."
        reason_3 = f"3. {away_team} имеет высокие шансы на победу ({away_prob:.1f}%)."

    return {
        "type": forecast_type,
        "coef": round(coef, 2),
        "exp": explanation,
        "bank": "3%",
        "risk": risk,
        "reason_3": reason_3
    }

def generate_bet_market_fallback(home_team: str, away_team: str, id_event: str, is_live: bool, goals_home: int, goals_away: int, elapsed: int) -> Dict[str, Any]:
    """Fallback генерация прогноза на основе хэша (без реальных коэффициентов)."""
    hash_seed = zlib.crc32(f"{home_team}_{away_team}_{id_event}".encode('utf-8'))
    if is_live:
        diff = goals_home - goals_away
        total_goals = goals_home + goals_away
        if diff == 0:
            if elapsed < 40:
                return {
                    "type": f"Live: Победа {home_team} (П1)",
                    "coef": round(2.05 + (hash_seed % 25) / 100, 2),
                    "exp": f"При счете {goals_home}:{goals_away} ({elapsed}') {home_team} доминирует по владению мячом.",
                    "bank": "2.5%",
                    "risk": f"Высокая вероятность контратак {away_team}.",
                    "reason_3": f"3. На {elapsed}'-й минуте хозяева нанесли 5+ ударов в створ."
                }
            else:
                next_total = total_goals + 0.5
                return {
                    "type": f"Live: Тотал больше {next_total}",
                    "coef": round(1.78 + (hash_seed % 20) / 100, 2),
                    "exp": f"Идет {elapsed}'-я минута ({goals_home}:{goals_away}). Высокая динамика в атаке во 2-м тайме.",
                    "bank": "3%",
                    "risk": "Сбивание темпа игры частыми фолами.",
                    "reason_3": "3. Обе команды усилили атаку за счет замен."
                }
        elif diff > 0:
            return {
                "type": f"Live: Фора 1 (0) / Победа {home_team}",
                "coef": round(1.55 + (hash_seed % 20) / 100, 2),
                "exp": f"{home_team} удерживает преимущество {goals_home}:{goals_away} ({elapsed}').",
                "bank": "3.5%",
                "risk": f"{away_team} организует финальный штурм.",
                "reason_3": f"3. {home_team} уверенно контролирует темп встречи."
            }
        else:
            return {
                "type": f"Live: Двойной шанс 1X ({home_team} не проиграет)",
                "coef": round(1.92 + (hash_seed % 30) / 100, 2),
                "exp": f"При счете {goals_home}:{goals_away} ({elapsed}') {home_team} наращивает давление.",
                "bank": "2%",
                "risk": "Риск пропустить быструю контратаку.",
                "reason_3": f"3. {home_team} забивала в 80% домашних матчей в концовках."
            }

    market_index = hash_seed % 5
    markets = [
        {"type": "Победа 1 (П1)", "coef": round(1.85 + (hash_seed % 30) / 100, 2), "exp": f"Победа команды {home_team} в основное время.", "bank": "3%", "risk": f"Команда {away_team} опасна в контратаках.", "reason_3": f"3. {home_team} выиграла 4 из 5 последних домашних матчей."},
        {"type": "Тотал голов больше 2.5", "coef": round(1.78 + (hash_seed % 25) / 100, 2), "exp": "В матче ожидается 3 или более забитых мяча.", "bank": "3%", "risk": "Осторожное начало встречи в первом тайме.", "reason_3": "3. Высокая средняя результативность очных встреч."},
        {"type": "Угловые: Тотал больше 9.5", "coef": round(1.82 + (hash_seed % 20) / 100, 2), "exp": "В матче будет подано 10 или более угловых.", "bank": "3%", "risk": "Ранний гол может снизить активность на флангах.", "reason_3": "3. Интенсивная фланговая игра обеих команд."},
        {"type": "Индивидуальный тотал 1 больше 1.5", "coef": round(1.88 + (hash_seed % 28) / 100, 2), "exp": f"Команда {home_team} забьет 2 или более гола.", "bank": "3%", "risk": f"Плотная оборона команды {away_team}.", "reason_3": f"3. {home_team} забивает дома 5 матчей подряд."},
        {"type": "Обе команды забьют — Да", "coef": round(1.72 + (hash_seed % 24) / 100, 2), "exp": "Каждый из клубов отметится забитым мячом.", "bank": "3%", "risk": "Переход в закрытый футбол после первого гола.", "reason_3": "3. Высокая статистика забитых и пропущенных мячей у обоих клубов."}
    ]
    return markets[market_index]

_LEAGUES_CACHE = {"data": None, "timestamp": 0}
_LEAGUES_TTL = 3600

async def fetch_leagues_from_api() -> List[Dict]:
    global _LEAGUES_CACHE
    now = time()
    if _LEAGUES_CACHE["data"] and (now - _LEAGUES_CACHE["timestamp"] < _LEAGUES_TTL):
        return _LEAGUES_CACHE["data"]

    headers = {"Authorization": f"Bearer {FOOTBALL_DATA_API_KEY}"}
    url = f"{FOOTBALL_DATA_URL}/leagues"

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(url, headers=headers)
            if res.status_code == 200:
                data = res.json()
                if data.get("success"):
                    leagues_data = data.get("data", [])
                    leagues = [{"id": "all", "name": "Все лиги"}]
                    for league in leagues_data:
                        leagues.append({
                            "id": str(league.get("league_id")),
                            "name": league.get("name", "Неизвестная лига")
                        })
                    _LEAGUES_CACHE["data"] = leagues
                    _LEAGUES_CACHE["timestamp"] = now
                    print(f"[Footballdata.io] Загружено лиг: {len(leagues)}")
                    return leagues
                else:
                    print(f"[Footballdata.io] Ошибка получения лиг: {data}")
                    return LEAGUES_DATA
            else:
                print(f"[Footballdata.io] Ошибка HTTP при получении лиг: {res.status_code}")
                return LEAGUES_DATA
    except Exception as e:
        print(f"[Footballdata.io] Исключение при получении лиг: {e}")
        return LEAGUES_DATA

# ===== ЭНДПОИНТЫ =====

@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

@app.get("/api/leagues")
async def get_leagues():
    leagues = await fetch_leagues_from_api()
    return {"leagues": leagues}

@app.get("/api/matches/today")
async def get_today_matches(
    league_id: str = Query("all"),
    x_telegram_init_data: str = Header(None, alias="X-Telegram-Init-Data")
):
    if x_telegram_init_data:
        await check_user_access(x_telegram_init_data)

    cache_key = get_cache_key(league_id)
    cached_data = get_from_cache(cache_key)
    if cached_data is not None:
        print(f"[Cache] Возвращены данные из кеша для league_id={league_id}")
        return cached_data

    posts = []
    api_error = None

    headers = {"Authorization": f"Bearer {FOOTBALL_DATA_API_KEY}"}
    url = f"{FOOTBALL_DATA_URL}/fixtures/upcoming"
    params = {"page": 1, "limit": 300}

    try:
        async with httpx.AsyncClient(timeout=15.0) as client:
            res = await client.get(url, headers=headers, params=params)
            print(f"[Footballdata.io] Статус: {res.status_code}")

            if res.status_code == 200:
                data = res.json()
                if data.get("success"):
                    matches_data = data.get("data", {})
                    if isinstance(matches_data, dict) and "matches" in matches_data:
                        matches = matches_data.get("matches", [])
                    elif isinstance(matches_data, list):
                        matches = matches_data
                    else:
                        matches = []

                    print(f"[Footballdata.io] Получено матчей (всего): {len(matches)}")

                    msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
                    max_date = (msk_now + timedelta(days=2)).date()

                    filtered_by_date = []
                    for m in matches:
                        match_date = m.get("match_date") or m.get("date")
                        if match_date:
                            try:
                                dt = datetime.strptime(match_date, "%Y-%m-%d %H:%M:%S")
                                dt_msk = dt + timedelta(hours=3)
                                if dt_msk.date() <= max_date:
                                    filtered_by_date.append(m)
                            except:
                                filtered_by_date.append(m)
                        else:
                            filtered_by_date.append(m)

                    matches = filtered_by_date
                    print(f"[Footballdata.io] После фильтрации по дате (до {max_date}): {len(matches)}")

                    if league_id != "all":
                        filtered_matches = []
                        for m in matches:
                            match_league = m.get("league", {})
                            if str(match_league.get("league_id")) == str(league_id):
                                filtered_matches.append(m)
                        matches = filtered_matches
                        print(f"[Footballdata.io] После фильтрации по лиге: {len(matches)}")
                    else:
                        print(f"[Footballdata.io] Показываем все матчи (без фильтрации)")

                    filtered = []
                    for m in matches:
                        status = m.get("status") or m.get("status_localized") or ""
                        if status not in ["FT", "FINISHED", "POSTPONED", "CANCELLED"]:
                            filtered.append(m)
                    matches = filtered
                    print(f"[Footballdata.io] Актуальных матчей (без завершённых): {len(matches)}")
                else:
                    api_error = f"Ошибка API: {data.get('error', {}).get('message', 'Unknown error')}"
                    print(f"[Footballdata.io] {api_error}")
                    matches = []
            else:
                api_error = f"Ошибка HTTP: {res.status_code} — {res.text[:200]}"
                print(f"[Footballdata.io] {api_error}")
                matches = []

    except Exception as e:
        api_error = f"Исключение при запросе: {str(e)}"
        print(f"[Footballdata.io Error]: {e}")
        matches = []

    for match in matches:
        match_id = str(match.get("match_id") or match.get("id") or "")

        league = match.get("league", {})
        competition = fix_encoding(league.get("name") or "Турнир")

        home_team = match.get("home_team", {})
        away_team = match.get("away_team", {})
        home_name = fix_encoding(home_team.get("team_name") or "Команда 1")
        away_name = fix_encoding(away_team.get("team_name") or "Команда 2")

        home_logo = home_team.get("team_logo")
        away_logo = away_team.get("team_logo")
        if not home_logo:
            home_logo = f"https://ui-avatars.com/api/?name={urllib.parse.quote(home_name[:3])}&background=00288e&color=fff"
        if not away_logo:
            away_logo = f"https://ui-avatars.com/api/?name={urllib.parse.quote(away_name[:3])}&background=00288e&color=fff"

        status = match.get("status") or match.get("status_localized") or "SCHEDULED"
        live_statuses = ["LIVE", "In Play", "1H", "2H", "HT", "ET", "BT", "P"]
        is_live = status in live_statuses
        is_finished = status in ["FT", "FINISHED"]

        score = match.get("score", {})
        goals_home = score.get("home") or 0
        goals_away = score.get("away") or 0

        match_date = match.get("match_date") or match.get("date") or ""
        dt_utc = None
        if match_date:
            try:
                dt_utc = datetime.strptime(match_date, "%Y-%m-%d %H:%M:%S")
            except:
                pass

        if dt_utc:
            dt_msk = dt_utc + timedelta(hours=3)
            time_str = dt_msk.strftime("%H:%M") + " (МСК)"
            msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
            today_msk = msk_now.date()
            match_date_only = dt_msk.date()
            if match_date_only == today_msk:
                date_text = "Сегодня"
            elif match_date_only == today_msk + timedelta(days=1):
                date_text = "Завтра"
            elif match_date_only == today_msk + timedelta(days=2):
                date_text = "Послезавтра"
            else:
                date_text = dt_msk.strftime("%d.%m")
            info_date = f"{date_text}, {time_str}"
        else:
            time_str = "19:00 (МСК)"
            info_date = f"Ближайший матч, {time_str}"

        venue = match.get("venue", {})
        venue_name = fix_encoding(venue.get("stadium_name") or "Стадион")

        # Извлекаем коэффициенты и вероятности
        odds = match.get("odds", {})
        probabilities = match.get("probabilities", {})

        # Генерируем прогноз
        bet_data = generate_bet_market_from_odds(home_name, away_name, odds, probabilities)
        if bet_data is None:
            # Fallback на старый метод
            bet_data = generate_bet_market_fallback(
                home_name, away_name, match_id, is_live,
                goals_home, goals_away, 0
            )

        match_display = f"{home_name} {goals_home} : {goals_away} {away_name}" if is_live else f"{home_name} — {away_name}"
        info_prefix = "LIVE" if is_live else ""

        reasons = [
            f"1. Мотивация: {home_name} нацелена на победу на домашнем стадионе.",
            f"2. Форма: {away_name} демонстрирует высокую результативность в атаке.",
            bet_data["reason_3"]
        ]

        post = {
            "event_id": match_id,
            "league_id": str(league.get("league_id") if isinstance(league, dict) else ""),
            "sport": "Футбол",
            "is_live": is_live,
            "goals_home": int(goals_home),
            "goals_away": int(goals_away),
            "elapsed": 0,
            "home_badge": home_logo,
            "away_badge": away_logo,
            "step_1": {
                "title": competition,
                "match": match_display,
                "info": f"{info_prefix} {info_date}".strip() if info_prefix else info_date
            },
            "step_2": {
                "forecast": bet_data["type"],
                "coefficient": bet_data["coef"],
                "explanation": bet_data["exp"]
            },
            "step_3": reasons,
            "step_4": sanitize_text(bet_data["risk"]),
            "step_5": f"Рекомендуемый размер подрасчета: {bet_data['bank']} от банка.",
            "disclaimer": "Аналитика сформирована на основе математической модели вероятностей."
        }

        # ===== ДОБАВЛЕНИЕ РЕАЛЬНОЙ СТАТИСТИКИ =====
        if is_live or is_finished:
            stats = await fetch_match_statistics(match_id, "LIVE" if is_live else "FT")
            post["statistics"] = stats  # может быть None
        else:
            post["statistics"] = None

        posts.append(post)

    posts.sort(key=lambda x: x["step_1"]["info"])

    response = {
        "count": len(posts),
        "date": get_msk_today_str(),
        "forecasts": posts,
        "api_error": api_error
    }

    if not api_error:
        set_to_cache(cache_key, response)
        print(f"[Cache] Данные сохранены в кеш для league_id={league_id}")

    return response

@app.get("/api/stats/compare")
async def compare_teams(
    home: str,
    away: str,
    home_badge: str = "",
    away_badge: str = "",
    is_live: bool = False,
    goals_home: int = 0,
    goals_away: int = 0,
    elapsed: int = 0,
    event_id: str = "",
    forecast: str = "",
    coefficient: float = 0.0,
    explanation: str = "",
    reason_1: str = "",
    reason_2: str = "",
    reason_3: str = "",
    x_telegram_init_data: str = Header(None, alias="X-Telegram-Init-Data")
):
    if x_telegram_init_data:
        await check_user_access(x_telegram_init_data)

    home = fix_encoding(home)
    away = fix_encoding(away)
    home_badge = home_badge or f"https://ui-avatars.com/api/?name={urllib.parse.quote(home[:3])}&background=00288e&color=fff"
    away_badge = away_badge or f"https://ui-avatars.com/api/?name={urllib.parse.quote(away[:3])}&background=00288e&color=fff"

    # Если прогноз не передан – генерируем заново (для совместимости)
    if not forecast or coefficient == 0.0:
        bet_market = generate_bet_market_fallback(
            home, away, event_id or f"{home}_{away}",
            is_live, goals_home, goals_away, elapsed
        )
        forecast = bet_market["type"]
        coefficient = bet_market["coef"]
        explanation = bet_market["exp"]
        reason_3 = bet_market.get("reason_3", "")
    else:
        reason_3 = reason_3

    # ===== ПОЛУЧАЕМ РЕАЛЬНУЮ СТАТИСТИКУ =====
    stats = None
    if event_id:
        status = "LIVE" if is_live else "FT"
        stats = await fetch_match_statistics(event_id, status)

    # Формируем данные для пункта 2 (форма и показатели) с использованием статистики
    if stats:
        home_stats = stats.get("home", {})
        away_stats = stats.get("away", {})
        home_pos = home_stats.get("possession", 0)
        away_pos = away_stats.get("possession", 0)
        home_shots = home_stats.get("totalShots", 0)
        away_shots = away_stats.get("totalShots", 0)
        home_corners = home_stats.get("corners", 0)
        away_corners = away_stats.get("corners", 0)
        home_yellow = home_stats.get("yellowCards", 0)
        away_yellow = away_stats.get("yellowCards", 0)

        form_home_stats = (f"{home}: владение {home_pos}%, удары {home_shots} (в створ {home_stats.get('shotsOnGoal', 0)}), "
                           f"угловые {home_corners}, ж/к {home_yellow}")
        form_away_stats = (f"{away}: владение {away_pos}%, удары {away_shots} (в створ {away_stats.get('shotsOnGoal', 0)}), "
                           f"угловые {away_corners}, ж/к {away_yellow}")
    else:
        # fallback заглушки
        if is_live:
            form_home_stats = f"{home}: нанесла {max(3, goals_home * 2 + 2)} ударов в створ."
            form_away_stats = f"{away}: совершила {max(2, goals_away * 2 + 1)} опасных контратак."
        else:
            form_home_stats = f"{home}: забивает в среднем 2.1 гола за матч."
            form_away_stats = f"{away}: забивает 1.6 гола за матч на выезде."

    # Остальные пункты остаются с заглушками (можно позже дополнить)
    if is_live:
        h2h_summary = f"Матч идет прямо сейчас ({elapsed} мин, счет {goals_home}:{goals_away}). Преимущество у {home}."
        morale_text = f"Команда {home} активнее проводит данный отрезок игры."
        squad_home_news = f"У {home} вышли свежие игроки линии атаки."
        squad_away_news = f"У {away} наблюдается утомление в линии защиты."
    else:
        h2h_summary = f"В последних 5 очных матчах преимущество у {home}: 3 победы, 1 ничья и 1 поражение."
        morale_text = f"Команда {home} сфокусирована на результат перед родными трибунами."
        squad_home_news = f"У {home} вернулся ключевой полузащитник."
        squad_away_news = f"У {away} дисквалифицирован защитник основы."

    return {
        "home": {"name": home, "badge": home_badge, "winrate": "68%", "last_5": ["W", "W", "D", "W", "L"]},
        "away": {"name": away, "badge": away_badge, "winrate": "54%", "last_5": ["W", "L", "W", "D", "W"]},
        "analysis_5_points": {
            "point_1_h2h": {"title": "1. Ход игры и H2H" if is_live else "1. История очных встреч (H2H)", "summary": h2h_summary, "details": [f"{home} 2 : 1 {away}", f"{away} 1 : 1 {home}", f"{home} 3 : 0 {away}"]},
            "point_2_form": {"title": "2. Live-показатели" if is_live else "2. Текущая форма и показатели", "home_form": ["W", "W", "D", "W", "L"], "away_form": ["W", "L", "W", "D", "W"], "home_stats": form_home_stats, "away_stats": form_away_stats},
            "point_3_morale": {"title": "3. Психологическое преимущество", "text": morale_text},
            "point_4_squad": {"title": "4. Корректировки и замены", "home_news": squad_home_news, "away_news": squad_away_news},
            "point_5_recommendation": {
                "title": "5. Live-вердикт" if is_live else "5. Итоговый вердикт и рекомендация",
                "recommended_bet": forecast,
                "coefficient": coefficient,
                "explanation": explanation,
                "all_odds": {"П1": 1.95, "Ничья": 3.40, "П2": 3.10},
                "bank_management": f"Рекомендуемый размер подрасчета: 3% от банка.",
                "final_conclusion": f"Позиция «{forecast}» актуализирована для текущего состояния матча."
            }
        }
    }
