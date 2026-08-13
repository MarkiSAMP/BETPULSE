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

BOT_TOKEN = os.getenv("BOT_TOKEN", "8958459929:AAEnq2FWercdCYQoSUjA_n37nrY1PNIYo4E")
DATABASE_URL = os.getenv("DATABASE_URL", "postgresql://neondb_owner:npg_hKa5W3yrsevu@ep-polished-cloud-b19g2r39-pooler.c-5.eu-central-1.aws.neon.tech/neondb?sslmode=require&channel_binding=require")
WEBAPP_URL = os.getenv("WEBAPP_URL", "https://betpulse-6knn.onrender.com")
PAYMENT_PROVIDER_TOKEN = os.getenv("PAYMENT_PROVIDER_TOKEN", "")

bot = Bot(token=BOT_TOKEN)
dp = Dispatcher()

def get_clean_db_url(url: str) -> str:
    if not url:
        return ""
    if "?" in url:
        return url.split("?")[0]
    return url

async def get_db():
    clean_url = get_clean_db_url(DATABASE_URL)
    return await asyncpg.connect(clean_url, ssl="require")

async def init_db():
    """Инициализация и автоматическое расширение таблицы пользователей."""
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
        await conn.execute("""
            ALTER TABLE users ADD COLUMN IF NOT EXISTS last_message_id BIGINT;
        """)
        await conn.close()
        print("[DB]: Таблица users успешно синхронизирована в Neon.tech")
    except Exception as e:
        print(f"[DB Error]: Ошибка при инициализации таблицы: {e}")

async def safe_delete_message(chat_id: int, message_id: int):
    """Безопасное удаление сообщения без остановки работы при ошибках."""
    if not message_id:
        return
    try:
        await bot.delete_message(chat_id=chat_id, message_id=message_id)
    except Exception:
        pass

@dp.message(CommandStart())
async def cmd_start(message: Message):
    user_id = message.from_user.id
    username = message.from_user.username or ""
    first_name = message.from_user.first_name or ""
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    # Удаляем само входящее сообщение /start
    await safe_delete_message(message.chat.id, message.message_id)

    is_paid = False
    expires_at = None
    last_msg_id = None

    if DATABASE_URL:
        try:
            conn = await get_db()
            row = await conn.fetchrow("SELECT is_paid, expires_at, last_message_id FROM users WHERE user_id = $1;", user_id)
            
            if row:
                is_paid = row['is_paid']
                expires_at = row['expires_at']
                last_msg_id = row['last_message_id']
                
                await conn.execute("""
                    UPDATE users SET username = $1, first_name = $2 WHERE user_id = $3;
                """, username, first_name, user_id)
            else:
                await conn.execute("""
                    INSERT INTO users (user_id, username, first_name, is_paid)
                    VALUES ($1, $2, $3, FALSE);
                """, user_id, username, first_name)

            await conn.close()
        except Exception as e:
            print(f"[DB Error in /start]: {e}")

    # Удаляем предыдущее сообщение бота, если оно сохранилось в базе
    if last_msg_id:
        await safe_delete_message(message.chat.id, last_msg_id)

    # Если подписка активна
    if is_paid and expires_at and expires_at > now:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="🚀 Открыть BETPULSE App", web_app=WebAppInfo(url=WEBAPP_URL))]
        ])
        sent_msg = await message.answer(
            f"Добро пожаловать обратно, <b>{first_name}</b>!\n\n"
            f"✅ Ваша подписка активна до: <b>{expires_at.strftime('%d.%m.%Y %H:%M')} (МСК)</b>",
            reply_markup=kb,
            parse_mode="HTML"
        )
    else:
        # Презентация проекта
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="show_subscription")]
        ])
        
        presentation_text = (
            f"👋 <b>Добро пожаловать в BETPULSE!</b>\n\n"
            f"<b>BETPULSE</b> — это инновационная информационно-аналитическая платформа для комплексного анализа футбольной статистики и матчей в режиме реального времени.\n\n"
            f"🧠 <b>Что вы получаете внутри нашего Mini App:</b>\n"
            f"• 📊 <b>Live-аналитика матчей:</b> динамический трекинг коэффициентов, опасных моментов и статистических трендов прямо во время игры.\n"
            f"• 🔍 <b>Глубокий разбор в 5 шагов:</b> история очных встреч (H2H), актуальная форма команд, психологическое состояние, ротация составов и грамотное управление банком.\n"
            f"• ⚽ <b>Покрытие ведущих лиг:</b> Лига Чемпионов, АПЛ, Ла Лига, Серия А, Бундеслига, РПЛ и международные турниры.\n"
            f"• 🛡 <b>Математический алгоритм:</b> объективный расчет вероятностей без эмоционального фактора.\n\n"
            f"💡 <i>Сервис создан для информационной поддержки и автоматизации анализа спортивных данных.</i>\n\n"
            f"Нажмите кнопку ниже, чтобы оформить доступ к платформе:"
        )
        
        sent_msg = await message.answer(presentation_text, reply_markup=kb, parse_mode="HTML")

    # Сохраняем ID отправленного сообщения в базу
    if DATABASE_URL and sent_msg:
        try:
            conn = await get_db()
            await conn.execute("UPDATE users SET last_message_id = $1 WHERE user_id = $2;", sent_msg.message_id, user_id)
            await conn.close()
        except Exception as e:
            print(f"[DB Save Msg ID Error]: {e}")

@dp.callback_query(F.data == "show_subscription")
async def process_show_subscription(callback: CallbackQuery):
    await callback.answer()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оплатить 500 рублей", callback_data="pay_500")],
        [InlineKeyboardButton(text="« Назад", callback_data="back_to_main")]
    ])
    
    sub_text = (
        "📋 <b>Подписка BETPULSE PRO</b>\n\n"
        "• <b>Срок действия:</b> 30 дней\n"
        "• <b>Функционал:</b> Полный неограниченный доступ к Mini App, аналитическим разборам и Live-фильтрам\n"
        "• <b>Стоимость:</b> 500 ₽\n\n"
        "Нажмите кнопку ниже для безопасной оплаты через ЮKassa:"
    )
    
    try:
        await callback.message.edit_text(sub_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "back_to_main")
async def process_back_to_main(callback: CallbackQuery):
    await callback.answer()
    
    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="💳 Оформить подписку", callback_data="show_subscription")]
    ])
    
    presentation_text = (
        f"👋 <b>Добро пожаловать в BETPULSE!</b>\n\n"
        f"<b>BETPULSE</b> — это инновационная информационно-аналитическая платформа для комплексного анализа футбольной статистики и матчей в режиме реального времени.\n\n"
        f"🧠 <b>Что вы получаете внутри нашего Mini App:</b>\n"
        f"• 📊 <b>Live-аналитика матчей:</b> динамический трекинг коэффициентов, опасных моментов и статистических трендов прямо во время игры.\n"
        f"• 🔍 <b>Глубокий разбор в 5 шагов:</b> история очных встреч (H2H), актуальная форма команд, психологическое состояние, ротация составов и грамотное управление банком.\n"
        f"• ⚽ <b>Покрытие ведущих лиг:</b> Лига Чемпионов, АПЛ, Ла Лига, Серия А, Бундеслига, РПЛ и международные турниры.\n"
        f"• 🛡 <b>Математический алгоритм:</b> объективный расчет вероятностей без эмоционального фактора.\n\n"
        f"💡 <i>Сервис создан для информационной поддержки и автоматизации анализа спортивных данных.</i>\n\n"
        f"Нажмите кнопку ниже, чтобы оформить доступ к платформе:"
    )
    
    try:
        await callback.message.edit_text(presentation_text, reply_markup=kb, parse_mode="HTML")
    except Exception:
        pass

@dp.callback_query(F.data == "pay_500")
async def process_pay_500(callback: CallbackQuery):
    await callback.answer()
    
    if not PAYMENT_PROVIDER_TOKEN:
        kb = InlineKeyboardMarkup(inline_keyboard=[
            [InlineKeyboardButton(text="« Назад", callback_data="show_subscription")]
        ])
        try:
            await callback.message.edit_text(
                "⚠️ <b>Платежный шлюз временно в процессе настройки.</b>\n"
                "Пожалуйста, попробуйте немного позже или обратитесь к администратору.",
                reply_markup=kb,
                parse_mode="HTML"
            )
        except Exception:
            pass
        return

    prices = [LabeledPrice(label="Доступ к BETPULSE PRO (30 дней)", amount=50000)]
    
    # Удаляем предыдущее меню перед отправкой счета оплаты
    await safe_delete_message(callback.message.chat.id, callback.message.message_id)

    sent_msg = await bot.send_invoice(
        chat_id=callback.from_user.id,
        title="Подписка BETPULSE PRO",
        description="Доступ к информационно-аналитической платформе на 30 дней",
        provider_token=PAYMENT_PROVIDER_TOKEN,
        currency="RUB",
        prices=prices,
        payload=f"sub_{callback.from_user.id}",
        start_parameter="betpulse_sub"
    )
    
    if DATABASE_URL and sent_msg:
        try:
            conn = await get_db()
            await conn.execute("UPDATE users SET last_message_id = $1 WHERE user_id = $2;", sent_msg.message_id, callback.from_user.id)
            await conn.close()
        except Exception:
            pass

@dp.pre_checkout_query()
async def pre_checkout_handler(pre_checkout_q: PreCheckoutQuery):
    await bot.answer_pre_checkout_query(pre_checkout_q.id, ok=True)

@dp.message(F.successful_payment)
async def successful_payment_handler(message: Message):
    user_id = message.from_user.id
    expires_at = datetime.now(timezone.utc).replace(tzinfo=None) + timedelta(days=30)

    await safe_delete_message(message.chat.id, message.message_id)

    if DATABASE_URL:
        try:
            conn = await get_db()
            row = await conn.fetchrow("SELECT last_message_id FROM users WHERE user_id = $1;", user_id)
            if row and row['last_message_id']:
                await safe_delete_message(message.chat.id, row['last_message_id'])

            await conn.execute("""
                UPDATE users 
                SET is_paid = TRUE, expires_at = $1 
                WHERE user_id = $2;
            """, expires_at, user_id)
            await conn.close()
        except Exception as e:
            print(f"[DB Update Error]: {e}")

    kb = InlineKeyboardMarkup(inline_keyboard=[
        [InlineKeyboardButton(text="🚀 Открыть BETPULSE App", web_app=WebAppInfo(url=WEBAPP_URL))]
    ])
    
    sent_msg = await message.answer(
        "🎉 <b>Оплата успешно завершена!</b>\n\n"
        f"Вам открыт полный доступ к аналитике на 30 дней (до {expires_at.strftime('%d.%m.%Y')}).\n\n"
        "Нажмите кнопку ниже, чтобы запустить приложение:",
        reply_markup=kb,
        parse_mode="HTML"
    )

    if DATABASE_URL and sent_msg:
        try:
            conn = await get_db()
            await conn.execute("UPDATE users SET last_message_id = $1 WHERE user_id = $2;", sent_msg.message_id, user_id)
            await conn.close()
        except Exception:
            pass

async def main():
    await init_db()
    await bot.delete_webhook(drop_pending_updates=True)
    print("[BOT]: Бот BETPULSE запущен и готов к работе!")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
