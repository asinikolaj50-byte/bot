import os
import json
import asyncio
import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

# Достаем токен из переменных окружения сервера (куда его передаст GitHub)
TOKEN = os.getenv("TELEGRAM_TOKEN")

bot = Bot(token=TOKEN, default_properties=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

# Функция для загрузки твоей базы данных из файла
def load_local_db():
    try:
        with open("database.json", "r", encoding="utf-8") as f:
            return json.load(f)
    except FileNotFoundError:
        return {"users": {}}

# Функция запроса к бесплатному API CoinGecko
async def get_crypto_price(coin_id: str):
    # Койны в CoinGecko ищутся по id (bitcoin, ethereum, solana)
    url = f"https://api.coingecko.com/api/v3/simple/price?ids={coin_id.lower()}&vs_currencies=usd&include_market_cap=true"
    async with httpx.AsyncClient() as client:
        try:
            response = await client.get(url, timeout=10.0)
            if response.status_code == 200:
                data = response.json()
                if coin_id.lower() in data:
                    return data[coin_id.lower()]
            return None
        except Exception:
            return None

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 Привет! Я бот-аналитик.\n\n"
        "🔸 Чтобы проверить пользователя из базы, пришли его Telegram ID: `/check_user 123456789`\n"
        "🔸 Чтобы узнать цену крипты, напиши ID монеты (bitcoin, ethereum): `/crypto bitcoin`"
    )

# Проверка по твоей базе данных файлом
@dp.message(Command("check_user"))
async def cmd_check_user(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Введи ID пользователя. Пример: `/check_user 123456789`")
        return
    
    user_id = args[1]
    db = load_local_db()
    users = db.get("users", {})

    if user_id in users:
        user_info = users[user_id]
        text = (
            f"👤 **Информация о пользователе {user_id}:**\n\n"
            f"🔹 **Имя:** {user_info.get('name', 'Не указано')}\n"
            f"🔹 **Статус:** {user_info.get('status', 'Обычный')}\n"
            f"🔹 **Заметка:** {user_info.get('notes', '-')}"
        )
    else:
        text = f"❌ Пользователь с ID `{user_id}` в твоей базе данных **не найден**."

    await message.answer(text)

# Получение данных с CoinGecko
@dp.message(Command("crypto"))
async def cmd_crypto(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Введи ID монеты (например: bitcoin, ethereum, solana). Пример: `/crypto solana`")
        return

    coin_id = args[1].lower()
    await message.answer(f"🔍 Запрашиваю данные для **{coin_id.upper()}** с CoinGecko...")

    crypto_data = await get_crypto_price(coin_id)

    if crypto_data:
        price = crypto_data.get("usd", 0)
        cap = crypto_data.get("usd_market_cap", 0)
        text = (
            f"💰 **Рыночные данные {coin_id.upper()}:**\n\n"
            f"💵 **Текущая цена:** ${price:,.2f}\n"
            f"📊 **Капитализация:** ${cap:,.0f}"
        )
    else:
        text = f"❌ Не удалось найти монету `{coin_id}` или API временно недоступен. Проверь правильность названия (именно id, а не тикер)."

    await message.answer(text)

async def main():
    print("Бот запущен и слушает команды...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
