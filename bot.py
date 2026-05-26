import os
import re
import json
import asyncio
import io
import csv
import urllib.parse
from datetime import datetime
import httpx
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, InlineKeyboardButton, FSInputFile
from aiogram.utils.keyboard import InlineKeyboardBuilder

TOKEN = os.getenv("TELEGRAM_TOKEN", "")
_raw_admin = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(_raw_admin) if _raw_admin.isdigit() else None

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан!")
if not ADMIN_ID:
    print("⚠️  ADMIN_ID не задан — загрузка файлов будет недоступна.")

bot = Bot(token=TOKEN, default_properties=DefaultBotProperties(parse_mode="HTML"))
dp = Dispatcher()

CRYPTORANK_FILE = "cryptorank_results.json"
HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

CRYPTO_KW = (
    "crypto OR cryptocurrency OR bitcoin OR BTC OR blockchain OR web3 "
    'OR investor OR "venture capital" OR VC OR DeFi OR NFT OR altcoin OR fund'
)


# ── Хранилище CryptoRank ──────────────────────────────────────────────────────

def load_cr() -> list:
    if not os.path.exists(CRYPTORANK_FILE):
        return []
    try:
        with open(CRYPTORANK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_cr(entry: dict):
    data = load_cr()
    existing = {r.get("query", "").lower() for r in data}
    if entry["query"].lower() not in existing:
        data.append(entry)
        with open(CRYPTORANK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ── Клавиатуры ───────────────────────────────────────────────────────────────

def main_menu() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Поиск человека",      callback_data="help_search")
    kb.button(text="📂 Загрузить базу",       callback_data="help_upload")
    kb.button(text="📋 Найдены на CryptoRank", callback_data="show_found")
    kb.button(text="❓ Справка",              callback_data="help_all")
    kb.adjust(2, 2)
    return kb.as_markup()


def search_links_kb(name: str, found_cr: bool) -> InlineKeyboardMarkup:
    q = urllib.parse.quote(name)
    kb = InlineKeyboardBuilder()
    kb.button(text="🔗 CryptoRank",  url=f"https://cryptorank.io/people?search={q}")
    kb.button(text="🔗 LinkedIn",    url=f"https://www.linkedin.com/search/results/people/?keywords={q}")
    kb.button(text="🔗 Google",      url=f"https://www.google.com/search?q=%22{q}%22+crypto+OR+bitcoin")
    kb.button(text="🔗 Twitter/X",   url=f"https://twitter.com/search?q=%22{q}%22+crypto")
    if found_cr:
        kb.button(text="📋 Все в CryptoRank", callback_data="show_found")
    kb.adjust(2, 2, 1)
    return kb.as_markup()


def back_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="◀️ Главное меню", callback_data="main_menu")
    return kb.as_markup()


# ── DuckDuckGo поиск ─────────────────────────────────────────────────────────

async def ddg_search(query: str, max_results: int = 6) -> list[dict]:
    """Реальный поиск через html.duckduckgo.com."""
    url = "https://html.duckduckgo.com/html/"
    data = {"q": query, "b": "", "kl": ""}
    results = []
    try:
        async with httpx.AsyncClient(
            timeout=20.0,
            follow_redirects=True,
            headers={**HEADERS, "Content-Type": "application/x-www-form-urlencoded"},
        ) as client:
            resp = await client.post(url, data=data)

        if resp.status_code != 200:
            return []

        html = resp.text

        # Парсим блоки результатов
        blocks = re.findall(
            r'<div class="links_main[^"]*".*?</div>\s*</div>',
            html, re.DOTALL
        )
        if not blocks:
            # Фолбэк: ищем просто ссылки с заголовками
            links = re.findall(
                r'<a[^>]+class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
                html, re.DOTALL
            )
            snippets = re.findall(
                r'class="result__snippet"[^>]*>(.*?)</(?:a|span|div)>',
                html, re.DOTALL
            )
            for i, (href, title_html) in enumerate(links[:max_results]):
                title = re.sub(r"<[^>]+>", "", title_html).strip()
                if "uddg=" in href:
                    href = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
                if href.startswith("http") and title:
                    snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip() if i < len(snippets) else ""
                    results.append({"title": title, "url": href, "snippet": snippet[:250]})
        else:
            for block in blocks[:max_results]:
                title_m = re.search(r'class="result__a"[^>]*href="([^"]+)"[^>]*>(.*?)</a>', block, re.DOTALL)
                snip_m  = re.search(r'class="result__snippet"[^>]*>(.*?)</(?:a|span|div)>', block, re.DOTALL)
                if not title_m:
                    continue
                href = title_m.group(1)
                title = re.sub(r"<[^>]+>", "", title_m.group(2)).strip()
                if "uddg=" in href:
                    href = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
                if not href.startswith("http") or not title:
                    continue
                snippet = re.sub(r"<[^>]+>", "", snip_m.group(1)).strip()[:250] if snip_m else ""
                results.append({"title": title, "url": href, "snippet": snippet})

    except Exception as e:
        print(f"[DDG error] {e}")

    return results


# ── CryptoRank поиск ─────────────────────────────────────────────────────────

async def search_cryptorank(name: str) -> list[dict]:
    found = []

    # 1. Официальный API CryptoRank (публичный, без ключа)
    try:
        q = urllib.parse.quote(name)
        async with httpx.AsyncClient(timeout=10.0, headers=HEADERS, follow_redirects=True) as client:
            for path in [
                f"https://cryptorank.io/api/v1/people?search={q}&limit=5",
                f"https://cryptorank.io/api/v0/coins?search={q}&limit=5",
            ]:
                r = await client.get(path)
                if r.status_code == 200:
                    data = r.json()
                    items = data if isinstance(data, list) else data.get("data", [])
                    for item in items[:3]:
                        n = item.get("name") or item.get("fullName", "")
                        if name.lower().split()[0] in n.lower():
                            found.append({
                                "source": "api",
                                "name": n,
                                "url": f"https://cryptorank.io/people/{item.get('slug', '')}",
                                "role": item.get("role", ""),
                                "company": item.get("company", ""),
                            })
    except Exception:
        pass

    # 2. DDG site:cryptorank.io
    ddg = await ddg_search(f'"{name}" site:cryptorank.io', max_results=4)
    for r in ddg:
        if "cryptorank.io" in r["url"]:
            found.append({
                "source": "ddg",
                "name": name,
                "url": r["url"],
                "snippet": r.get("snippet", ""),
            })

    return found


# ── Основной поиск ───────────────────────────────────────────────────────────

async def full_search(name: str, phone: str = "") -> dict:
    """
    Возвращает dict с результатами CryptoRank и DDG.
    Сначала CryptoRank, если не найден — DDG крипто/инвест.
    """
    cr = await search_cryptorank(name)

    ddg_results = []
    if not cr:
        queries = [
            f'"{name}" ({CRYPTO_KW})',
            f'"{name}" инвестиции OR криптовалюта OR блокчейн OR стартап',
        ]
        if phone:
            queries.append(f'"{name}" "{phone}"')

        tasks = [ddg_search(q, max_results=5) for q in queries]
        batches = await asyncio.gather(*tasks, return_exceptions=True)

        seen = set()
        for batch in batches:
            if not isinstance(batch, list):
                continue
            for item in batch:
                if item["url"] not in seen:
                    seen.add(item["url"])
                    ddg_results.append(item)

    return {"cr": cr, "ddg": ddg_results}


# ── Парсинг базы данных из файла ─────────────────────────────────────────────

def parse_names_from_file(content: bytes, filename: str) -> list[dict]:
    """Извлекает список {name, phone} из JSON / CSV / TXT."""
    ext = filename.lower().rsplit(".", 1)[-1]
    text = content.decode("utf-8", errors="replace")
    people = []

    if ext == "json":
        data = json.loads(text)
        items = data if isinstance(data, list) else data.get("users", data.get("data", []))
        if isinstance(items, dict):
            items = list(items.values())
        for item in items:
            if not isinstance(item, dict):
                continue
            name = (
                item.get("name") or item.get("fullName") or item.get("full_name")
                or f"{item.get('first_name', '')} {item.get('last_name', '')}".strip()
            )
            phone = str(item.get("phone", item.get("tel", item.get("phone_number", "")))).strip()
            if name and name.strip():
                people.append({"name": name.strip(), "phone": phone})

    elif ext == "csv":
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            row = {k.lower().strip(): v.strip() for k, v in row.items()}
            name = (
                row.get("name") or row.get("fullname") or row.get("full_name")
                or f"{row.get('first_name', '')} {row.get('last_name', '')}".strip()
                or f"{row.get('имя', '')} {row.get('фамилия', '')}".strip()
            )
            phone = row.get("phone", row.get("tel", row.get("телефон", "")))
            if name:
                people.append({"name": name, "phone": phone or ""})

    else:  # txt
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            phone_m = re.search(r"\+?\d[\d\-\(\)\s]{8,14}", line)
            phone = phone_m.group(0).strip() if phone_m else ""
            name = re.sub(r"\+?\d[\d\-\(\)\s]{8,14}", "", line).strip(" ,|;")
            if len(name) > 3:
                people.append({"name": name, "phone": phone})

    return people


# ── Форматирование результата ─────────────────────────────────────────────────

def format_result(name: str, phone: str, res: dict) -> str:
    cr    = res["cr"]
    ddg   = res["ddg"]
    parts = [f"<b>🔍 {name}</b>" + (f" | <code>{phone}</code>" if phone else "")]

    if cr:
        parts.append(f"\n✅ <b>Найден на CryptoRank</b> ({len(cr)} результат(а)):")
        for r in cr[:3]:
            line = f"  🔗 <a href='{r['url']}'>{r['url']}</a>"
            if r.get("role"):
                line += f"\n  👤 {r['role']}"
            if r.get("company"):
                line += f" · {r['company']}"
            if r.get("snippet"):
                line += f"\n  <i>{r['snippet'][:200]}</i>"
            parts.append(line)
    else:
        parts.append("\n❌ <b>CryptoRank:</b> не найден")

        if ddg:
            parts.append(f"\n🌐 <b>Упоминания в интернете</b> ({len(ddg)}):")
            for i, r in enumerate(ddg[:8], 1):
                line = f"  {i}. <a href='{r['url']}'>{r['title'][:80]}</a>"
                if r.get("snippet"):
                    line += f"\n     <i>{r['snippet'][:180]}</i>"
                parts.append(line)
        else:
            parts.append("\n🔍 <i>Крипто/инвест упоминаний не найдено.</i>")

    return "\n".join(parts)


# ── Команды ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "👋 <b>Бот поиска по крипто и инвест источникам</b>\n\n"
        "Ищу людей на <b>CryptoRank</b> и в открытом интернете.\n"
        "Выбери действие:",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: types.CallbackQuery):
    await call.message.edit_text(
        "👋 <b>Главное меню</b>\n\nВыбери действие:",
        reply_markup=main_menu(),
    )


@dp.callback_query(F.data == "help_search")
async def cb_help_search(call: types.CallbackQuery):
    await call.message.edit_text(
        "🔍 <b>Поиск человека</b>\n\n"
        "Введи команду:\n"
        "<code>/search Иван Петров</code>\n"
        "<code>/search Иван Петров +79001234567</code>\n\n"
        "Телефон — дополнительно, не обязателен.",
        reply_markup=back_kb(),
    )


@dp.callback_query(F.data == "help_upload")
async def cb_help_upload(call: types.CallbackQuery):
    await call.message.edit_text(
        "📂 <b>Загрузка базы</b>\n\n"
        "Пришли файл в чат. Поддерживаемые форматы:\n"
        "• <b>JSON</b> — список объектов с полями name/phone\n"
        "• <b>CSV</b> — таблица с заголовками\n"
        "• <b>TXT</b> — по одному человеку на строку\n\n"
        "Бот автоматически пройдётся по каждому и начнёт поиск.",
        reply_markup=back_kb(),
    )


@dp.callback_query(F.data == "help_all")
async def cb_help_all(call: types.CallbackQuery):
    await call.message.edit_text(
        "❓ <b>Справка</b>\n\n"
        "<b>Как работает поиск:</b>\n"
        "1️⃣ Сначала ищу на <b>cryptorank.io</b>\n"
        "   → Если найден — сохраняю в файл\n"
        "2️⃣ Если нет — ищу в DuckDuckGo:\n"
        "   • Точное имя в кавычках\n"
        "   • + ключевые слова: crypto, bitcoin, web3,\n"
        "     investor, VC, DeFi, blockchain, fund…\n\n"
        "<b>Команды:</b>\n"
        "/search Имя Фамилия [телефон]\n"
        "/found — кто найден на CryptoRank\n"
        "/start — главное меню",
        reply_markup=back_kb(),
    )


@dp.callback_query(F.data == "show_found")
async def cb_show_found(call: types.CallbackQuery):
    data = load_cr()
    if not data:
        await call.message.edit_text(
            "📂 <b>CryptoRank: пусто</b>\n\n"
            "Никто ещё не найден на CryptoRank.\n"
            "Используй /search или загрузи файл с базой.",
            reply_markup=back_kb(),
        )
        return

    lines = [f"📋 <b>Найдены на CryptoRank ({len(data)} чел.):</b>\n"]
    for i, entry in enumerate(data, 1):
        q  = entry.get("query", "—")
        ts = entry.get("timestamp", "")[:10]
        cnt = entry.get("cr_count", 0)
        lines.append(f"{i}. <b>{q}</b> — {cnt} рез. [{ts}]")
        for r in entry.get("cr_results", [])[:1]:
            lines.append(f"   🔗 <a href='{r.get('url', '')}'>{r.get('url', '')}</a>")

    await call.message.edit_text(
        "\n".join(lines),
        reply_markup=back_kb(),
        disable_web_page_preview=True,
    )


@dp.message(Command("found"))
async def cmd_found(message: types.Message):
    data = load_cr()
    if not data:
        await message.answer(
            "📂 <b>CryptoRank: пусто</b>\n\nНикто ещё не найден.",
            reply_markup=back_kb(),
        )
        return
    lines = [f"📋 <b>Найдены на CryptoRank ({len(data)} чел.):</b>\n"]
    for i, entry in enumerate(data, 1):
        q  = entry.get("query", "—")
        ts = entry.get("timestamp", "")[:10]
        lines.append(f"{i}. <b>{q}</b> [{ts}]")
        for r in entry.get("cr_results", [])[:1]:
            lines.append(f"   🔗 <a href='{r.get('url','')}'>ссылка</a>")
    await message.answer("\n".join(lines), disable_web_page_preview=True, reply_markup=back_kb())


@dp.message(Command("search"))
async def cmd_search(message: types.Message):
    raw = message.text.split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        await message.answer(
            "⚠️ Укажи имя.\n"
            "Пример: <code>/search Иван Петров</code>",
            reply_markup=back_kb(),
        )
        return

    parts = raw[1].strip().split()
    phone = ""
    name_parts = []
    for p in parts:
        if re.match(r"^\+?\d{7,15}$", p):
            phone = p
        else:
            name_parts.append(p)

    if len(name_parts) < 2:
        await message.answer(
            "⚠️ Нужно минимум <b>имя и фамилия</b>.\n"
            "Пример: <code>/search Иван Петров</code>",
        )
        return

    name = " ".join(name_parts)
    await _run_search(message, name, phone)


async def _run_search(message: types.Message, name: str, phone: str = ""):
    status = await message.answer(
        f"⏳ Ищу <b>{name}</b>...\n"
        "Шаг 1/2 — проверяю CryptoRank"
    )
    try:
        res = await asyncio.wait_for(full_search(name, phone), timeout=40.0)
    except asyncio.TimeoutError:
        await status.edit_text(
            f"⚠️ <b>Таймаут поиска для {name}.</b>\n"
            "Попробуй ещё раз или уточни имя.",
            reply_markup=search_links_kb(name, False),
        )
        return

    # Сохраняем если найдено на CryptoRank
    if res["cr"]:
        save_cr({
            "query":      name,
            "phone":      phone,
            "timestamp":  datetime.utcnow().isoformat(),
            "cr_count":   len(res["cr"]),
            "cr_results": res["cr"],
        })

    text = format_result(name, phone, res)
    found_cr = bool(res["cr"])

    await status.edit_text(
        text,
        reply_markup=search_links_kb(name, found_cr),
        disable_web_page_preview=True,
    )


# ── Приём файла с базой ───────────────────────────────────────────────────────

@dp.message(F.document)
async def handle_file(message: types.Message):
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        await message.answer("❌ Только администратор может загружать файлы.")
        return

    doc = message.document
    filename = doc.file_name or "file.txt"
    ext = filename.lower().rsplit(".", 1)[-1]

    if ext not in ("json", "csv", "txt"):
        await message.answer(
            f"⚠️ Формат <b>{ext}</b> не поддерживается.\n"
            "Используй: <b>JSON, CSV или TXT</b>",
        )
        return

    status = await message.answer(f"📥 Загружаю <b>{filename}</b>...")

    try:
        file = await bot.get_file(doc.file_id)
        buf = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        content = buf.getvalue()

        people = parse_names_from_file(content, filename)
    except Exception as e:
        await status.edit_text(f"❌ Ошибка чтения файла: {e}")
        return

    if not people:
        await status.edit_text(
            "⚠️ Не удалось найти имена в файле.\n\n"
            "Убедись что в JSON есть поля <code>name</code> / <code>fullName</code>,\n"
            "в CSV — колонки <code>name</code> или <code>first_name + last_name</code>,\n"
            "в TXT — по одному имени на строку."
        )
        return

    await status.edit_text(
        f"✅ Файл принят. Найдено <b>{len(people)}</b> человек.\n"
        f"🚀 Начинаю поиск по каждому...\n\n"
        f"<i>Результаты буду присылать по мере нахождения.</i>"
    )

    # Ищем каждого последовательно с небольшой паузой
    for i, person in enumerate(people, 1):
        name  = person["name"]
        phone = person.get("phone", "")

        await message.answer(
            f"🔎 <b>[{i}/{len(people)}]</b> Ищу: <b>{name}</b>"
            + (f" | <code>{phone}</code>" if phone else "")
        )

        try:
            res = await asyncio.wait_for(full_search(name, phone), timeout=40.0)
        except asyncio.TimeoutError:
            await message.answer(
                f"⚠️ [{i}/{len(people)}] <b>{name}</b> — таймаут, пропускаю.",
                reply_markup=search_links_kb(name, False),
            )
            await asyncio.sleep(2)
            continue

        if res["cr"]:
            save_cr({
                "query":      name,
                "phone":      phone,
                "timestamp":  datetime.utcnow().isoformat(),
                "cr_count":   len(res["cr"]),
                "cr_results": res["cr"],
            })

        text = format_result(name, phone, res)
        await message.answer(
            text,
            reply_markup=search_links_kb(name, bool(res["cr"])),
            disable_web_page_preview=True,
        )

        # Пауза между запросами чтобы не получить бан от DDG
        await asyncio.sleep(3)

    # Итоговый файл CryptoRank
    cr_data = load_cr()
    found_in_batch = [
        r for r in cr_data
        if r.get("query", "") in [p["name"] for p in people]
    ]

    summary = (
        f"✅ <b>Готово!</b> Обработано: {len(people)} человек.\n"
        f"📊 Найдено на CryptoRank: <b>{len(found_in_batch)}</b>"
    )

    kb = InlineKeyboardBuilder()
    if found_in_batch:
        kb.button(text="📋 Посмотреть найденных", callback_data="show_found")
    kb.button(text="◀️ Главное меню", callback_data="main_menu")
    kb.adjust(1)

    await message.answer(summary, reply_markup=kb.as_markup())

    # Отправляем обновлённый файл cryptorank_results.json
    if found_in_batch and os.path.exists(CRYPTORANK_FILE):
        await message.answer_document(
            FSInputFile(CRYPTORANK_FILE),
            caption=f"📁 Файл с результатами CryptoRank ({len(cr_data)} записей)"
        )


# ── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    print("✅ Бот запущен.")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])


if __name__ == "__main__":
    asyncio.run(main())
