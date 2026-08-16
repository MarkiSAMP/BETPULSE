import os
import asyncio
import asyncpg
import random
import httpx
from datetime import datetime, timedelta, timezone
from aiogram import Bot, Dispatcher, F
from aiogram.filters import CommandStart
from aiogram.types import (
    Message,
    CallbackQuery,
    InlineKeyboardMarkup,
    InlineKeyboardButton,
    WebAppInfo,
    LabeledPrice,
    PreCheckoutQuery,
    MenuButtonWebApp,
    MenuButtonDefault,
)
from aiogram.enums import ParseMode

BOT_TOKEN = os.getenv("BOT_TOKEN")
DATABASE_URL = os.getenv("DATABASE_URL")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://betpulse-6knn.onrender.com")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")

DONATIONALERTS_PAGE_URL = os.getenv("DONATIONALERTS_PAGE_URL")
DONATIONALERTS_ACCESS_TOKEN = os.getenv("DONATIONALERTS_ACCESS_TOKEN")
DONATIONALERTS_API_URL = "https://www.donationalerts.com/api/v1"

if not BOT_TOKEN:
    raise RuntimeError("BOT_TOKEN не задан")
if not DATABASE_URL:
    raise RuntimeError("DATABASE_URL не задан")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

# ===== БАЗА ДАННЫХ =====
def get_clean_db_url(url: str) -> str:
    if not url:
        return ""
    return url.split("?")[0] if "?" in url else url

async def get_db():
    clean_url = get_clean_db_url(DATABASE_URL)
    return await asyncpg.connect(clean_url, ssl="require")

async def init_db():
    if not DATABASE_URL:
        return
    try:
        conn = await get_db()
        await conn.execute("""
            CREATE TABLE IF NOT EXISTS users (
                user_id BIGINT PRIMARY KEY,
                username TEXT,
                first_name TEXT,
                is_paid BOOLEAN DEFAULT FALSE,
                expires_at TIMESTAMP,
                last_message_id BIGINT
            );
        """)
        await conn.execute("ALTER TABLE users ADD COLUMN IF NOT EXISTS last_message_id BIGINT;")
        await conn.close()
        print("[DB]: Таблица users синхронизирована.")
    except Exception as e:
        print(f"[DB Error]: {e}")

async def safe_delete_message(chat_id: int, message_id: int):
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

def get_expires_at(days: int = 30) -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=days)

# ===== DONATION ALERTS =====
user_amounts = {}

def generate_unique_amount(user_id: int) -> float:
    random_cents = random.randint(1, 99)
    amount = 500.00 + random_cents / 100
    user_amounts[user_id] = amount
    return amount

async def check_recent_donation(user_id: int) -> bool:
    if not DONATIONALERTS_ACCESS_TOKEN:
        return False

    expected_amount = user_amounts.get(user_id)
    if not expected_amount:
        return False

    headers = {"Authorization": f"Bearer {DONATIONALERTS_ACCESS_TOKEN}"}
    params = {"limit": 10, "order": "desc"}

    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            res = await client.get(
                f"{DONATIONALERTS_API_URL}/donations",
                headers=headers,
                params=params
            )
            if res.status_code != 200:
                return False

            data = res.json()
            donations = data.get("data", [])
            now = datetime.now(timezone.utc)
            threshold = now - timedelta(seconds=60)  # 60 секунд

            for donation in donations:
                created_at = donation.get("created_at")
                if not created_at:
                    continue
                try:
                    don_time = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
                except:
                    continue

                if don_time < threshold:
                    continue

                amount = float(donation.get("amount", 0))
                if abs(amount - expected_amount) < 0.01:
                    if user_id in user_amounts:
                        del user_amounts[user_id]
                    return True

            return False

    except Exception:
        return False

# ===== ОБРАБОТЧИКИ =====
@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    await safe_delete_message(message.chat.id, message.message_id)

    is_paid = False
    expires_at = None
    last_msg_id = None

    if DATABASE_URL:
        try:
            conn = await get_db()
            row = await conn.fetchrow(
                "SELECT is_paid, expires_at, last_message_id FROM users WHERE user_id = $1;",
                user_id
            )
            if row:
                is_paid = row["is_paid"]
                expires_at = row["expires_at"]
                last_msg_id = row["last_message_id"]
                await conn.execute(
                    "UPDATE users SET username = $1, first_name = $2 WHERE user_id = $3;",
                    username, first_name, user_id
                )
            else:
                await conn.execute(
                    "INSERT INTO users (user_id, username, first_name, is_paid) VALUES ($1, $2, $3, FALSE);",
                    user_id, username, first_name
                )
            await conn.close()
        except Exception as e:
            print(f"[DB Error in /start]: {e}")

    if last_msg_id:
        await safe_delete_message(message.chat.id, last_msg_id)

    if is_paid and expires_at and expires_at > now:
        await bot.set_chat_menu_button(
            chat_id=user_id,
            menu_button=MenuButtonWebApp(
                text="🚀 Открыть BETPULSE",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Открыть BETPULSE App", web_app=WebAppInfo(url=WEBAPP_URL))]
        ])
        sent_msg = await message.answer(
            f"Добро пожаловать обратно, <b>{first_name}</b>!\n\n"
            f"✅ Ваша подписка активна до: <b>{expires_at.strftime('%d.%m.%Y %H:%M')} (МСК)</b>",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
    else:
        await bot.set_chat_menu_button(
            chat_id=user_id,
            menu_button=MenuButtonDefault()
        )
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="show_subscription")]
        ])
        text = (
            f"👋 <b>Добро пожаловать в BETPULSE!</b>\n\n"
            f"<b>BETPULSE</b> — инновационная платформа для анализа футбольной статистики.\n\n"
            f"🧠 <b>Что вы получаете:</b>\n"
            f"• 📊 Live-аналитика матчей\n"
            f"• 🔍 Глубокий разбор в 5 шагов\n"
            f"• ⚽ Покрытие топ-лиг\n"
            f"• 🛡 Математический алгоритм\n\n"
            f"Нажмите кнопку ниже, чтобы оформить доступ."
            f"📞 По вопросам поддержки пишите: @swet25on"
        )
        sent_msg = await message.answer(text, reply_markup=kb, parse_mode=ParseMode.HTML)

    if DATABASE_URL and sent_msg:
        try:
            conn = await get_db()
            await conn.execute(
                "UPDATE users SET last_message_id = $1 WHERE user_id = $2;",
                sent_msg.message_id, user_id
            )
            await conn.close()
        except Exception as e:
            print(f"[DB Save Msg ID Error]: {e}")

@dp.callback_query(F.data == "show_subscription")
async def process_show_subscription(callback: CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="⭐ Telegram Stars (авто)", callback_data="pay_stars")],
        [InlineKeyboardButton(text="💳 Donation Alerts (карта/СБП)", callback_data="pay_donationalerts")],
        [InlineKeyboardButton(text="« Назад", callback_data="back_to_main")]
    ])
    text = (
        "📋 <b>Подписка BETPULSE PRO</b>\n\n"
        "• <b>Срок действия:</b> 30 дней\n"
        "• <b>Функционал:</b> Полный доступ к аналитике\n"
        "• <b>Стоимость:</b> 500 ₽\n\n"
        "Выберите способ оплаты:"
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        pass

@dp.callback_query(F.data == "back_to_main")
async def process_back_to_main(callback: CallbackQuery):
    await callback.answer()
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="show_subscription")]
    ])
    text = (
        f"👋 <b>Добро пожаловать в BETPULSE!</b>\n\n"
        f"<b>BETPULSE</b> — инновационная платформа для анализа футбольной статистики.\n\n"
        f"🧠 <b>Что вы получаете:</b>\n"
        f"• 📊 Live-аналитика матчей\n"
        f"• 🔍 Глубокий разбор в 5 шагов\n"
        f"• ⚽ Покрытие топ-лиг\n"
        f"• 🛡 Математический алгоритм\n\n"
        f"Нажмите кнопку ниже, чтобы оформить доступ."
    )
    try:
        await callback.message.edit_text(text, reply_markup=kb, parse_mode=ParseMode.HTML)
    except Exception:
        pass

@dp.callback_query(F.data == "pay_donationalerts")
async def process_pay_donationalerts(callback: CallbackQuery):
    await callback.answer()
    if not DONATIONALERTS_PAGE_URL or not DONATIONALERTS_ACCESS_TOKEN:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="show_subscription")]
        ])
        await callback.message.edit_text(
            "⚠️ <b>Donation Alerts временно недоступен.</b>\n"
            "Пожалуйста, выберите другой способ оплаты.",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
        return

    amount = generate_unique_amount(callback.from_user.id)
    amount_str = f"{amount:.2f}"

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Перейти к донату", url=DONATIONALERTS_PAGE_URL)],
        [InlineKeyboardButton(text="✅ Я оплатил (проверить)", callback_data="check_donationalerts")],
        [InlineKeyboardButton(text="« Назад", callback_data="show_subscription")]
    ])
    await safe_delete_message(callback.message.chat.id, callback.message.message_id)
    sent_msg = await callback.message.answer(
        f"💳 <b>Оплата через Donation Alerts</b>\n\n"
        f"Переведите ровно <b>{amount_str} ₽</b> на страницу доната.\n"
        "Нажмите кнопку ниже, чтобы перейти к оплате.\n\n"
        "После перевода нажмите «Я оплатил» для автоматической проверки.",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )
    if DATABASE_URL and sent_msg:
        try:
            conn = await get_db()
            await conn.execute(
                "UPDATE users SET last_message_id = $1 WHERE user_id = $2;",
                sent_msg.message_id, callback.from_user.id
            )
            await conn.close()
        except Exception:
            pass

@dp.callback_query(F.data == "check_donationalerts")
async def check_donationalerts(callback: CallbackQuery):
    await callback.answer()
    user_id = callback.from_user.id
    found = await check_recent_donation(user_id)

    if found:
        expires_at = get_expires_at()
        if DATABASE_URL:
            try:
                conn = await get_db()
                await conn.execute(
                    "UPDATE users SET is_paid = TRUE, expires_at = $1 WHERE user_id = $2;",
                    expires_at, user_id
                )
                await conn.close()
            except Exception as e:
                await callback.message.answer("❌ Ошибка активации подписки. Обратитесь в поддержку.")
                return

        if DATABASE_URL:
            try:
                conn = await get_db()
                row = await conn.fetchrow("SELECT last_message_id FROM users WHERE user_id = $1;", user_id)
                if row and row["last_message_id"]:
                    await safe_delete_message(callback.message.chat.id, row["last_message_id"])
                await conn.close()
            except Exception:
                pass

        await bot.set_chat_menu_button(
            chat_id=user_id,
            menu_button=MenuButtonWebApp(
                text="🚀 Открыть BETPULSE",
                web_app=WebAppInfo(url=WEBAPP_URL)
            )
        )

        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Открыть BETPULSE App", web_app=WebAppInfo(url=WEBAPP_URL))]
        ])
        sent_msg = await callback.message.answer(
            "🎉 <b>Оплата успешно подтверждена!</b>\n\n"
            f"Вам открыт полный доступ на 30 дней (до {expires_at.strftime('%d.%m.%Y')}).\n\n"
            "Нажмите кнопку ниже, чтобы запустить приложение:",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
        if DATABASE_URL and sent_msg:
            try:
                conn = await get_db()
                await conn.execute(
                    "UPDATE users SET last_message_id = $1 WHERE user_id = $2;",
                    sent_msg.message_id, user_id
                )
                await conn.close()
            except Exception:
                pass
    else:
        amount = user_amounts.get(user_id)
        amount_str = f"{amount:.2f}" if amount else "указанную"
        await callback.message.answer(
            f"⏳ <b>Донат не найден или сумма не совпадает.</b>\n\n"
            f"Убедитесь, что вы перевели ровно <b>{amount_str} ₽</b>.\n"
            "Донат должен быть совершён в течение последних 60 секунд.\n\n"
            "Если вы всё сделали правильно, попробуйте подождать 1 минуту и нажать кнопку снова.\n"
            "Если проблема повторяется – отправьте скриншот чека в поддержку.",
            parse_mode=ParseMode.HTML,
            reply_markup=InlineKeyboardMarkup(inline_keyboard=[
                [InlineKeyboardButton(text="🔄 Проверить снова", callback_data="check_donationalerts")],
                [InlineKeyboardButton(text="« Назад", callback_data="show_subscription")]
            ])
        )

@dp.callback_query(F.data == "pay_stars")
async def process_pay_stars(callback: CallbackQuery):
    await callback.answer()
    if not PAYMENT_PROVIDER_TOKEN:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="show_subscription")]
        ])
        await callback.message.edit_text(
            "⚠️ <b>Оплата через Telegram Stars временно недоступна.</b>\n"
            "Пожалуйста, выберите другой способ оплаты.",
            reply_markup=kb,
            parse_mode=ParseMode.HTML
        )
        return

    prices = [LabeledPrice(label="Подписка BETPULSE PRO (30 дней)", amount=50000)]
    await safe_delete_message(callback.message.chat.id, callback.message.message_id)
    sent_msg = await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Подписка BETPULSE PRO",
        description="Доступ к платформе на 30 дней",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
        payload=f"sub_{callback.from_user.id}",
        start_parameter="betpulse_sub"
    )
    if DATABASE_URL and sent_msg:
        try:
            conn = await get_db()
            await conn.execute(
                "UPDATE users SET last_message_id = $1 WHERE user_id = $2;",
                sent_msg.message_id, callback.from_user.id
            )
            await conn.close()
        except Exception:
            pass

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    user_id = message.from_user.id
    expires_at = get_expires_at()

    await safe_delete_message(message.chat.id, message.message_id)

    if DATABASE_URL:
        try:
            conn = await get_db()
            row = await conn.fetchrow("SELECT last_message_id FROM users WHERE user_id = $1;", user_id)
            if row and row["last_message_id"]:
                await safe_delete_message(message.chat.id, row["last_message_id"])
            await conn.execute(
                "UPDATE users SET is_paid = TRUE, expires_at = $1 WHERE user_id = $2;",
                expires_at, user_id
            )
            await conn.close()
        except Exception as e:
            print(f"[DB Update Error]: {e}")

    await bot.set_chat_menu_button(
        chat_id=user_id,
        menu_button=MenuButtonWebApp(
            text="🚀 Открыть BETPULSE",
            web_app=WebAppInfo(url=WEBAPP_URL)
        )
    )

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть BETPULSE App", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    sent_msg = await message.answer(
        "🎉 <b>Оплата успешно завершена!</b>\n\n"
        f"Вам открыт полный доступ на 30 дней (до {expires_at.strftime('%d.%m.%Y')}).\n\n"
        "Нажмите кнопку ниже, чтобы запустить приложение:",
        reply_markup=kb,
        parse_mode=ParseMode.HTML
    )

    if DATABASE_URL and sent_msg:
        try:
            conn = await get_db()
            await conn.execute(
                "UPDATE users SET last_message_id = $1 WHERE user_id = $2;",
                sent_msg.message_id, user_id
            )
            await conn.close()
        except Exception:
            pass

async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    await bot.set_chat_menu_button(menu_button=MenuButtonDefault())
    print("[BOT]: Бот BETPULSE запущен!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
