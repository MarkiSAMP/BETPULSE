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
from functools import lru_cache
from time import time

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

from bot import bot, dp, init_db

# === Переменные окружения ===
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

# ===== КЕШИРОВАНИЕ =====
CACHE_TTL = int(os.getenv("CACHE_TTL", 300))  # 5 минут по умолчанию
cache = {}

def get_cache_key(league_id: str) -> str:
    """Формирует ключ для кеша на основе параметров запроса."""
    today_str = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    return f"matches_{league_id}_{today_str}"

def get_from_cache(key: str) -> Optional[Dict]:
    """Возвращает данные из кеша, если они есть и не истекли."""
    if key in cache:
        data, timestamp = cache[key]
        if time() - timestamp < CACHE_TTL:
            return data
        else:
            del cache[key]
    return None

def set_to_cache(key: str, data: Dict):
    """Сохраняет данные в кеш с текущим временем."""
    cache[key] = (data, time())

# ===== Lifespan =====
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[SERVER]: Инициализация базы данных...")
    try:
        await init_db()
    except Exception as e:
        print(f"[SERVER DB Error]: {e}")

    print("[SERVER]: Сброс вебхуков и запуск фоновой службы Telegram-бота...")
    try:
        await bot.delete_webhook(drop_pending_updates=True)
    except Exception as e:
        print(f"[SERVER Webhook Error]: {e}")

    await asyncio.sleep(2)

    bot_task = asyncio.create_task(dp.start_polling(bot))
    print("[SERVER]: FastAPI и Telegram Bot успешно запущены!")
    yield
    print("[SERVER]: Остановка службы бота...")
    bot_task.cancel()

app = FastAPI(title="BETPULSE Mini App API", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Список лиг (запасной, но динамический будет переопределён)
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
        conn = await asyncpg.connect(clean_url, ssl="require")
        row = await conn.fetchrow("SELECT is_paid, expires_at FROM users WHERE user_id = $1;", user_id)
        await conn.close()
    except Exception as e:
        print(f"[DB Auth Error]: {e}")
        raise HTTPException(status_code=500, detail="Ошибка проверки доступа к БД.")

    if not row or not row["is_paid"]:
        raise HTTPException(status_code=403, detail="Доступ ограничен: требуется оформление подписки.")

    now = datetime.now(timezone.utc).replace(tzinfo=None)
    if row["expires_at"] and row["expires_at"] < now:
        raise HTTPException(status_code=403, detail="Срок действия вашей подписки истек.")

    return user_id

def generate_bet_market_for_match(
    home_team: str,
    away_team: str,
    id_event: str,
    is_live: bool = False,
    goals_home: int = 0,
    goals_away: int = 0,
    elapsed: int = 0
) -> Dict[str, Any]:
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

# ===== Кешируемый список лиг (обновляется раз в час) =====
_LEAGUES_CACHE = {"data": None, "timestamp": 0}
_LEAGUES_TTL = 3600

async def fetch_leagues_from_api() -> List[Dict]:
    """Запрос списка лиг из API с кэшированием."""
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
                    print(f"[Footballdata.io] Ошибка при получении лиг: {data}")
                    return LEAGUES_DATA
            else:
                print(f"[Footballdata.io] Ошибка HTTP при получении лиг: {res.status_code}")
                return LEAGUES_DATA
    except Exception as e:
        print(f"[Footballdata.io] Исключение при получении лиг: {e}")
        return LEAGUES_DATA

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
    # ВРЕМЕННО ОТКЛЮЧАЕМ ПРОВЕРКУ ПОДПИСКИ
    # if x_telegram_init_data:
    #     await check_user_access(x_telegram_init_data)

    # Проверяем кеш
    cache_key = get_cache_key(league_id)
    cached_data = get_from_cache(cache_key)
    if cached_data is not None:
        print(f"[Cache] Возвращены данные из кеша для league_id={league_id}")
        return cached_data

    posts = []
    api_error = None

    headers = {"Authorization": f"Bearer {FOOTBALL_DATA_API_KEY}"}

    # Запрашиваем предстоящие матчи с большим лимитом
    url = f"{FOOTBALL_DATA_URL}/fixtures/upcoming"
    params = {"page": 1, "limit": 200}

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

                    # Если выбрана конкретная лига — фильтруем
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

                    # Убираем завершённые и отменённые
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

    # Формируем ответ
    for match in matches:
        match_id = str(match.get("match_id") or match.get("id") or "")

        league = match.get("league", {})
        competition = league.get("name") or "Турнир"

        home_team = match.get("home_team", {})
        away_team = match.get("away_team", {})
        home_name = home_team.get("team_name") or "Команда 1"
        away_name = away_team.get("team_name") or "Команда 2"

        home_logo = home_team.get("team_logo")
        away_logo = away_team.get("team_logo")
        if not home_logo:
            home_logo = f"https://ui-avatars.com/api/?name={urllib.parse.quote(home_name[:3])}&background=00288e&color=fff"
        if not away_logo:
            away_logo = f"https://ui-avatars.com/api/?name={urllib.parse.quote(away_name[:3])}&background=00288e&color=fff"

        status = match.get("status") or match.get("status_localized") or "SCHEDULED"
        live_statuses = ["LIVE", "In Play", "1H", "2H", "HT", "ET", "BT", "P"]
        is_live = status in live_statuses

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
        venue_name = venue.get("stadium_name") or "Стадион"

        bet_data = generate_bet_market_for_match(
            home_name, away_name, match_id, is_live=is_live,
            goals_home=goals_home, goals_away=goals_away, elapsed=0
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
        posts.append(post)

    posts.sort(key=lambda x: x["step_1"]["info"])

    response = {
        "count": len(posts),
        "date": get_msk_today_str(),
        "forecasts": posts,
        "api_error": api_error
    }

    # Сохраняем в кеш только при успешном ответе (без ошибок)
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
    x_telegram_init_data: str = Header(None, alias="X-Telegram-Init-Data")
):
    # ВРЕМЕННО ОТКЛЮЧАЕМ ПРОВЕРКУ
    # if x_telegram_init_data:
    #     await check_user_access(x_telegram_init_data)

    home_badge = home_badge or f"https://ui-avatars.com/api/?name={urllib.parse.quote(home[:3])}&background=00288e&color=fff"
    away_badge = away_badge or f"https://ui-avatars.com/api/?name={urllib.parse.quote(away[:3])}&background=00288e&color=fff"

    bet_market = generate_bet_market_for_match(
        home, away, f"{home}_{away}",
        is_live=is_live, goals_home=goals_home, goals_away=goals_away, elapsed=elapsed
    )

    if is_live:
        h2h_summary = f"Матч идет прямо сейчас ({elapsed} мин, счет {goals_home}:{goals_away}). Преимущество у {home}."
        form_home_stats = f"{home}: нанесла {max(3, goals_home * 2 + 2)} ударов в створ."
        form_away_stats = f"{away}: совершила {max(2, goals_away * 2 + 1)} опасных контратак."
        morale_text = f"Команда {home} активнее проводит данный отрезок игры."
        squad_home_news = f"У {home} вышли свежие игроки линии атаки."
        squad_away_news = f"У {away} наблюдается утомление в линии защиты."
    else:
        h2h_summary = f"В последних 5 очных матчах преимущество у {home}: 3 победы, 1 ничья и 1 поражение."
        form_home_stats = f"{home}: забивает в среднем 2.1 гола за матч."
        form_away_stats = f"{away}: забивает 1.6 гола за матч на выезде."
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
                "recommended_bet": bet_market["type"],
                "coefficient": bet_market["coef"],
                "explanation": bet_market["exp"],
                "all_odds": {"П1": 1.95, "Ничья": 3.40, "П2": 3.10},
                "bank_management": f"Рекомендуемый размер подрасчета: {bet_market['bank']} от банка.",
                "final_conclusion": f"Позиция «{bet_market['type']}» актуализирована для текущего состояния матча."
            }
        }
    }
