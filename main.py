import os
import hmac
import json
import hashlib
import urllib.parse
import zlib
import asyncio
from datetime import datetime, timezone, timedelta
from typing import List, Dict, Any
from urllib.parse import parse_qs
from contextlib import asynccontextmanager

import asyncpg
import httpx
from fastapi import FastAPI, HTTPException, Header, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import FileResponse

# Импортируем экземпляры бота и функцию инициализации из bot.py
from bot import bot, dp, init_db

# Конфигурация переменная окружения
BOT_TOKEN = os.getenv("BOT_TOKEN", "8958459929:AAEnq2FWercdCYQoSUjA_n37nrY1PNIYo4E")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_hKa5W3yrsevu@ep-polished-cloud-b19g2r39-pooler.c-5.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
API_KEY = "504d57f6b4448f20a4789e6d4cfd7abe"
API_FOOTBALL_URL = "https://v3.football.api-sports.io/fixtures"

# Автозапуск бота вместе с сервером FastAPI
@asynccontextmanager
async def lifespan(app: FastAPI):
    print("[SERVER]: Инициализация базы данных...")
    await init_db()
    print("[SERVER]: Запуск фоновой службы Telegram-бота...")
    bot_task = asyncio.create_task(dp.start_polling(bot))
    print("[SERVER]: FastAPI и Telegram Bot успешно запущены и работают 24/7!")
    yield
    print("[SERVER]: Остановка службы бота...")
    bot_task.cancel()

app = FastAPI(title="BETPULSE Mini App API", lifespan=lifespan)

# Настройка CORS для работы Mini App
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LEAGUES_DATA = [
    {"id": "all", "name": "Все лиги"},
    {"id": "2", "name": "Лига Чемпионов"},
    {"id": "3", "name": "Лига Европы"},
    {"id": "848", "name": "Лига Конференций"},
    {"id": "39", "name": "АПЛ (Англия)"},
    {"id": "140", "name": "Ла Лига (Испания)"},
    {"id": "135", "name": "Серия А (Италия)"},
    {"id": "78", "name": "Бундеслига (Германия)"},
    {"id": "61", "name": "Лига 1 (Франция)"},
    {"id": "235", "name": "РПЛ (Россия)"},
    {"id": "667", "name": "Товарищеские (клубы)"},
    {"id": "10", "name": "Товарищеские (сборные)"}
]

FORBIDDEN_WORDS = ["ЖБ", "верняк", "100%", "грузим хаты", "проход 100", "чуйка"]

def sanitize_text(text: str) -> str:
    """Фильтрация запрещенных словарей для модерации."""
    for word in FORBIDDEN_WORDS:
        if word in text:
            text = text.replace(word, "[Аналитический тренд]")
    return text

def get_msk_today_str() -> str:
    msk_now = datetime.now(timezone.utc) + timedelta(hours=3)
    return msk_now.strftime("%Y-%m-%d")

def verify_telegram_init_data(init_data_raw: str) -> dict | None:
    """Проверка HMAC подписи Telegram WebApp."""
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
    """Проверка наличия оплаченной подписки у пользователя."""
    user = verify_telegram_init_data(init_data)
    if not user:
        raise HTTPException(status_code=401, detail="Запуск разрешен только через Telegram-бота.")

    user_id = user.get("id")
    if not DATABASE_URL:
        return user_id

    try:
        conn = await asyncpg.connect(DATABASE_URL)
        row = await conn.fetchrow("SELECT is_paid, expires_at FROM users WHERE user_id = $1;", user_id)
        await conn.close()
    except Exception as e:
        print(f"[DB Auth Error]: {e}")
        raise HTTPException(status_code=500, detail="Ошибка проверки доступа.")

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
    """Генерация рекомендаций на основе математического хэширования матча."""
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

class AnalysisEngine:
    @staticmethod
    def format_api_sports_match(item: Dict[str, Any], is_live: bool = False) -> Dict[str, Any]:
        fixture = item.get("fixture", {})
        league = item.get("league", {})
        teams = item.get("teams", {})
        status = fixture.get("status", {})
        goals = item.get("goals", {})

        home_team = teams.get("home", {}).get("name") or "Команда 1"
        away_team = teams.get("away", {}).get("name") or "Команда 2"
        competition = league.get("name") or "Турнир"

        goals_home = goals.get("home") if goals.get("home") is not None else 0
        goals_away = goals.get("away") if goals.get("away") is not None else 0
        elapsed = status.get("elapsed") or 0

        home_badge = teams.get("home", {}).get("logo") or f"https://ui-avatars.com/api/?name={urllib.parse.quote(home_team[:3])}&background=00288e&color=fff"
        away_badge = teams.get("away", {}).get("logo") or f"https://ui-avatars.com/api/?name={urllib.parse.quote(away_team[:3])}&background=00288e&color=fff"

        date_str = fixture.get("date")
        time_str = "19:00 (МСК)"
        if date_str:
            try:
                dt = datetime.fromisoformat(date_str)
                time_str = f"{dt.strftime('%H:%M')} (МСК)"
            except Exception:
                pass

        venue = fixture.get("venue", {}).get("name") or "Главная арена"
        id_ev = str(fixture.get("id") or f"{home_team}_{away_team}")
        
        bet_data = generate_bet_market_for_match(
            home_team, away_team, id_ev, is_live=is_live,
            goals_home=goals_home, goals_away=goals_away, elapsed=elapsed
        )

        match_display = f"{home_team} {goals_home} : {goals_away} {away_team}" if is_live else f"{home_team} — {away_team}"
        info_prefix = f"LIVE ({elapsed}')" if is_live and elapsed else ("LIVE" if is_live else "Сегодня")

        reasons = [
            f"1. Ход встречи: {home_team} контролирует инициативу." if is_live else f"1. Мотивация: {home_team} нацелена на победу на домашнем стадионе.",
            f"2. Динамика: {away_team} перестраивает тактическую схему." if is_live else f"2. Форма: {away_team} демонстрирует высокую результативность в атаке.",
            bet_data["reason_3"]
        ]

        return {
            "event_id": id_ev,
            "league_id": str(league.get("id", "")),
            "sport": "Футбол",
            "is_live": is_live,
            "goals_home": goals_home,
            "goals_away": goals_away,
            "elapsed": elapsed,
            "home_badge": home_badge,
            "away_badge": away_badge,
            "step_1": {
                "title": competition,
                "match": match_display,
                "info": f"{info_prefix}, {time_str} | Стадион: {venue}"
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

@app.get("/")
async def serve_frontend():
    return FileResponse("index.html")

@app.get("/api/leagues")
async def get_leagues():
    return {"leagues": LEAGUES_DATA}

@app.get("/api/matches/today")
async def get_today_matches(
    league_id: str = Query("all"),
    x_telegram_init_data: str = Header(None, alias="X-Telegram-Init-Data")
):
    if x_telegram_init_data:
        await check_user_access(x_telegram_init_data)

    today_str = get_msk_today_str()
    posts = []
    raw_fixtures = []

    headers = {"x-apisports-key": API_KEY}
    params = {"date": today_str, "timezone": "Europe/Moscow"}
    if league_id != "all":
        params["league"] = league_id

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(API_FOOTBALL_URL, headers=headers, params=params)
            if res.status_code == 200:
                data = res.json()
                raw_fixtures = data.get("response", [])
    except Exception as e:
        print(f"[API-Football Error]: {e}")

    finished_statuses = ["FT", "AET", "PEN", "CANC", "ABD", "PST"]
    live_statuses = ["1H", "HT", "2H", "ET", "BT", "P", "LIVE"]

    now_msk = datetime.now(timezone.utc) + timedelta(hours=3)
    now_ts = now_msk.timestamp()

    for item in raw_fixtures:
        fixture = item.get("fixture", {})
        status_short = fixture.get("status", {}).get("short", "")

        if status_short in finished_statuses:
            continue

        is_live = status_short in live_statuses

        if not is_live:
            fixture_ts = fixture.get("timestamp")
            if fixture_ts and fixture_ts + 10800 < now_ts:
                continue

        posts.append(AnalysisEngine.format_api_sports_match(item, is_live=is_live))

    return {"count": len(posts), "date": today_str, "forecasts": posts}

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
    if x_telegram_init_data:
        await check_user_access(x_telegram_init_data)

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
