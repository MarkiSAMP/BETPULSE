import os
import asyncio
import asyncpg
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
    PreCheckoutQuery
)

# Переменные окружения берем из Render / конфигурации
BOT_TOKEN = os.getenv("BOT_TOKEN", "8958459929:AAEnq2FWercdCYQoSUjA_n37nrY1PNIYo4E")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_hKa5W3yrsevu@ep-polished-cloud-b19g2r39-pooler.c-5.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://betpulse-6knn.onrender.com")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")  # Токен ЮKassa из BotFather

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

async def get_db():
    return await asyncpg.connect(DATABASE_URL)

async def init_db():
    """Инициализация таблицы пользователей в базе данных Neon.tech."""
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
                expires_at TIMESTAMP
            );
        """)
        await conn.close()
        print("[DB]: Таблица users успешно инициализирована в Neon.tech")
    except Exception as e:
        print(f"[DB Error]: Ошибка при создании таблицы: {e}")

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Регистрируем пользователя в базе данных, если его там нет
    is_paid = False
    expires_at = None

    if DATABASE_URL:
        try:
            conn = await get_db()
            await conn.execute("""
                INSERT INTO users (user_id, username, first_name, is_paid)
                VALUES ($1, $2, $3, FALSE)
                ON CONFLICT (user_id) DO UPDATE 
                SET username = EXCLUDED.username, 
                    first_name = EXCLUDED.first_name;
            """, user_id, username, first_name)
            
            row = await conn.fetchrow("SELECT is_paid, expires_at FROM users WHERE user_id = $1;", user_id)
            await conn.close()
            
            if row:
                is_paid = row['is_paid']
                expires_at = row['expires_at']
        except Exception as e:
            print(f"[DB Error in /start]: {e}")

    # Если подписка активна — даем прямую кнопку входа в Mini App
    if is_paid and expires_at and expires_at > now:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="?? Открыть BETPULSE App", web_app=WebAppInfo(url=WEBAPP_URL))]
        ])
        await message.answer(
            f"Добро пожаловать обратно, {first_name}!\n\n"
            f"? Ваша подписка активна до: **{expires_at.strftime('%d.%m.%Y %H:%M')} (МСК)**",
            reply_markup=kb,
            parse_mode="Markdown"
        )
    else:
        # Презентация проекта для нового/неоплаченного пользователя
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="?? Оформить подписку", callback_data="show_subscription")]
        ])
        
        presentation_text = (
            f"?? **Добро пожаловать в BETPULSE!**\n\n"
            f"**BETPULSE** — это инновационная информационно-аналитическая платформа для комплексного анализа футбольной статистики и матчей в режиме реального времени.\n\n"
            f"?? **Что вы получаете внутри нашего Mini App:**\n"
            f"• ?? **Live-аналитика матчей:** динамический трекинг коэффициентов, опасных моментов и статистических трендов прямо во время игры.\n"
            f"• ?? **Глубокий разбор в 5 шагов:** история очных встреч (H2H), актуальная форма команд, психологическое состояние, ротация составов и грамотное управление банком.\n"
            f"• ? **Покрытие ведущих лиг:** Лига Чемпионов, АПЛ, Ла Лига, Серия А, Бундеслига, РПЛ и международные турниры.\n"
            f"• ?? **Математический алгоритм:** объективный расчет вероятностей без эмоционального фактора.\n\n"
            f"?? *Сервис создан для информационной поддержки и автоматизации анализа спортивных данных.*\n\n"
            f"Нажмите кнопку ниже, чтобы оформить доступ к платформе:"
        )
        
        await message.answer(presentation_text, reply_markup=kb, parse_mode="Markdown")

# Нажатие на «Оформить подписку» -> Показ тарифа и кнопки оплаты
@dp.callback_query(F.data == "show_subscription")
async def process_show_subscription(callback: CallbackQuery):
    await callback.answer()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="?? Оплатить 500 рублей", callback_data="pay_500")]
    ])
    
    sub_text = (
        "?? **Подписка BETPULSE PRO**\n\n"
        "• **Срок действия:** 30 дней\n"
        "• **Функционал:** Полный неограниченный доступ к Mini App, аналитическим разборам и Live-фильтрам\n"
        "• **Стоимость:** 500 ?\n\n"
        "Нажмите кнопку ниже для безопасной оплаты через ЮKassa:"
    )
    
    await callback.message.answer(sub_text, reply_markup=kb, parse_mode="Markdown")

# Нажатие на «Оплатить 500 рублей» -> Выставление счета ЮKassa
@dp.callback_query(F.data == "pay_500")
async def process_pay_500(callback: CallbackQuery):
    await callback.answer()
    
    if not PAYMENT_PROVIDER_TOKEN:
        await callback.message.answer(
            "?? **Платежный шлюз временно в процессе настройки.**\n"
            "Пожалуйста, попробуйте немного позже или обратитесь к администратору.",
            parse_mode="Markdown"
        )
        return

    prices = [LabeledPrice(label="Доступ к BETPULSE PRO (30 дней)", amount=50000)] # 500.00 RUB в копейках
    
    await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Подписка BETPULSE PRO",
        description="Доступ к информационно-аналитической платформе на 30 дней",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
        payload=f"sub_{callback.from_user.id}",
        start_parameter="betpulse_sub"
    )

# Подтверждение готовности принять платеж
@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)

# Успешная оплата -> Активация подписки и выдача доступа к Mini App
@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    user_id = message.from_user.id
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)

    if DATABASE_URL:
        try:
            conn = await get_db()
            await conn.execute("""
                UPDATE users 
                SET is_paid = TRUE, expires_at = $1 
                WHERE user_id = $2;
            """, expires_at, user_id)
            await conn.close()
        except Exception as e:
            print(f"[DB Update Error]: {e}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="?? Открыть BETPULSE App", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    
    await message.answer(
        "?? **Оплата успешно завершена!**\n\n"
        f"Вам открыт полный доступ к аналитике на 30 дней (до {expires_at.strftime('%d.%m.%Y')}).\n\n"
        "Нажмите кнопку ниже, чтобы запустить приложение:",
        reply_markup=kb,
        parse_mode="Markdown"
    )

async def main():
    await init_db()
    print("[BOT]: Бот BETPULSE запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())