import os
import asyncio
import re
import httpx
import json
from datetime import datetime, timezone, timedelta
from fastapi import FastAPI, BackgroundTasks
# ... (остальные импорты у вас уже есть)

# ===== Donation Alerts =====
DONATIONALERTS_TOKEN = os.getenv("DONATIONALERTS_ACCESS_TOKEN")
DONATIONALERTS_API_URL = "https://www.donationalerts.com/api/v1"

# Храним ID последнего обработанного доната, чтобы не дублировать
last_processed_donation_id = None

async def check_donations():
    """
    Фоновая задача: опрашивает API Donation Alerts каждые 2 минуты
    и активирует подписки для новых донатов >= 500 ₽ с указанием ID.
    """
    global last_processed_donation_id

    if not DONATIONALERTS_TOKEN:
        print("[DonationAlerts] Token not set, skipping")
        return

    headers = {"Authorization": f"Bearer {DONATIONALERTS_TOKEN}"}
    params = {"limit": 10, "order": "desc"}  # последние 10 донатов

    async with httpx.AsyncClient(timeout=10.0) as client:
        try:
            res = await client.get(
                f"{DONATIONALERTS_API_URL}/donations",
                headers=headers,
                params=params
            )
            if res.status_code != 200:
                print(f"[DonationAlerts] API error: {res.status_code}")
                return

            data = res.json()
            donations = data.get("data", [])
            if not donations:
                return

            # Проходим по донатам (от новых к старым)
            for donation in donations:
                donation_id = donation.get("id")
                # Если донат уже обработан – пропускаем
                if donation_id == last_processed_donation_id:
                    break
                if last_processed_donation_id is None:
                    # При первом запуске запоминаем последний ID и выходим
                    last_processed_donation_id = donation_id
                    return

                amount = float(donation.get("amount", 0))
                message = donation.get("message", "")

                # Проверяем сумму и наличие ID
                if amount < 500:
                    continue

                match = re.search(r"ID:\s*(\d+)", message)
                if not match:
                    continue

                user_id = int(match.group(1))
                # Активируем подписку
                await activate_subscription(user_id, donation_id)

            # Обновляем последний обработанный ID
            if donations:
                last_processed_donation_id = donations[0].get("id")

        except Exception as e:
            print(f"[DonationAlerts] Error in check_donations: {e}")

async def activate_subscription(user_id: int, donation_id: int):
    """Активирует подписку для пользователя и уведомляет его."""
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)

    if DATABASE_URL:
        try:
            conn = await get_db()
            await conn.execute(
                "UPDATE users SET is_paid = TRUE, expires_at = $1 WHERE user_id = $2;",
                expires_at, user_id
            )
            await conn.close()
            print(f"[DonationAlerts] Subscription activated for user {user_id} (donation {donation_id})")
        except Exception as e:
            print(f"[DonationAlerts] DB error: {e}")
            return

    # Уведомляем пользователя
    try:
        await bot.send_message(
            chat_id=user_id,
            text=(
                "🎉 <b>Спасибо за донат!</b>\n\n"
                f"Вам открыт полный доступ на 30 дней (до {expires_at.strftime('%d.%m.%Y')}).\n"
                "Наслаждайтесь аналитикой BETPULSE."
            ),
            parse_mode="HTML"
        )
    except Exception as e:
        print(f"[DonationAlerts] Failed to notify user: {e}")

# ===== ЗАПУСК ФОНОВОЙ ЗАДАЧИ =====
@app.on_event("startup")
async def startup_event():
    """Запускаем фоновый цикл проверки донатов при старте сервера."""
    asyncio.create_task(donation_check_loop())

async def donation_check_loop():
    """Бесконечный цикл проверки донатов (раз в 2 минуты)."""
    while True:
        await check_donations()
        await asyncio.sleep(120)  # 2 минуты

# ===== ОСТАЛЬНЫЕ ЭНДПОИНТЫ (без изменений) =====
# ... ваш код main.py продолжается здесь
