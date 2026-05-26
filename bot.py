import os
import json
import asyncio
import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

# Токен берется из секретов хостинга/гитхаба
TOKEN = os.getenv("TELEGRAM_TOKEN")

# ВАЖНО: Замени это число на свой реальный Telegram ID, 
# чтобы только ТЫ мог загружать базу данных в бота, а не любой прохожий.
ADMIN_ID = 123456789  # <--- Вставь сюда свой ID (без кавычек)

bot = Bot(token=TOKEN, default_properties=DefaultBotProperties(parse_mode="Markdown"))
dp = Dispatcher()

DB_FILE = "database.json"

# Безопасная загрузка базы
def load_local_db():
    if not os.path.exists(DB_FILE):
        # Если файла еще нет, создаем пустую структуру
        with open(DB_FILE, "w", encoding="utf-8") as f:
            json.dump({"users": {}}, f)
        return {"users": {}}
    
    try:
        with open(DB_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {"users": {}}

# Функция запроса к CoinGecko API
async def get_crypto_price(coin_id: str):
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
        "👋 **Бот-аналитик готов к работе.**\n\n"
        "🔹 Проверить ID из базы: `/check_user 123456789`\n"
        "🔹 Узнать цену крипты: `/crypto bitcoin`\n\n"
        "⚙️ _Для админа: чтобы обновить базу, просто пришли мне файл database.json_"
    )

# Обработчик загрузки файла (работает только для ADMIN_ID)
@dp.message(lambda message: message.document is not None)
async def handle_docs(message: types.Message):
    if message.from_user.id != ADMIN_ID:
        await message.answer("❌ У вас нет прав для обновления базы данных.")
        return

    if message.document.file_name != "database.json":
        await message.answer("⚠️ Ошибка! Файл должен называться строго `database.json`")
        return

    await message.answer("📥 Скачиваю и обновляю базу данных...")
    
    # Получаем информацию о файле и качаем его
    file_id = message.document.file_id
    file = await bot.get_file(file_id)
    file_path = file.file_path
    
    # Скачиваем поверх старого файла
    await bot.download_file(file_path, DB_FILE)
    
    await message.answer("✅ **База данных успешно обновлена!** Теперь проверки будут идти по новому файлу.")

# Проверка юзера по загруженному файлу
@dp.message(Command("check_user"))
async def cmd_check_user(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Введи ID. Пример: `/check_user 123456789`")
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
        text = f"❌ Пользователь с ID `{user_id}` в текущей базе **not found**."

    await message.answer(text)

# Получение данных крипты
@dp.message(Command("crypto"))
async def cmd_crypto(message: types.Message):
    args = message.text.split()
    if len(args) < 2:
        await message.answer("⚠️ Введи ID монеты. Пример: `/crypto ethereum`")
        return

    coin_id = args[1].lower()
    crypto_data = await get_crypto_price(coin_id)

    if crypto_data:
        price = crypto_data.get("usd", 0)
        cap = crypto_data.get("usd_market_cap", 0)
        text = (
            f"💰 **Данные {coin_id.upper()}:**\n\n"
            f"💵 **Цена:** ${price:,.2f}\n"
            f"📊 **Капитализация:** ${cap:,.0f}"
        )
    else:
        text = f"❌ Монета `{coin_id}` не найдена на CoinGecko."

    await message.answer(text)

async def main():
    print("Бот запущен...")
    await dp.start_polling(bot)

if __name__ == "__main__":
    asyncio.run(main())
