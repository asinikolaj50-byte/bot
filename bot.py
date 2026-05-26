import os, re, json, asyncio, io, csv, urllib.parse
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.types import (
    InlineKeyboardMarkup, InlineKeyboardButton,
    ReplyKeyboardMarkup, KeyboardButton, ReplyKeyboardRemove, FSInputFile
)
from aiogram.utils.keyboard import InlineKeyboardBuilder, ReplyKeyboardBuilder
import httpx

TOKEN    = os.getenv("TELEGRAM_TOKEN", "")
_raw     = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(_raw) if _raw.isdigit() else None

if not TOKEN:
    raise RuntimeError("TELEGRAM_TOKEN не задан!")

bot = Bot(token=TOKEN, default_properties=DefaultBotProperties(parse_mode="HTML"))
dp  = Dispatcher(storage=MemoryStorage())

CR_FILE = "cryptorank_results.json"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36",
    "Accept-Language": "ru-RU,ru;q=0.9,en-US;q=0.8",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── FSM States ────────────────────────────────────────────────────────────────
class Search(StatesGroup):
    waiting_name  = State()
    waiting_phone = State()

# ── Хранилище CryptoRank ──────────────────────────────────────────────────────
def load_cr() -> list:
    try:
        if os.path.exists(CR_FILE):
            with open(CR_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
    except Exception:
        pass
    return []

def save_cr(entry: dict):
    data = load_cr()
    if entry["name"].lower() not in {r.get("name","").lower() for r in data}:
        data.append(entry)
        with open(CR_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# ── Клавиатуры ────────────────────────────────────────────────────────────────
def main_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="🔍 Найти человека")
    kb.button(text="📂 Загрузить базу")
    kb.button(text="📋 CryptoRank список")
    kb.button(text="❓ Помощь")
    kb.adjust(2, 2)
    return kb.as_markup(resize_keyboard=True)

def cancel_kb() -> ReplyKeyboardMarkup:
    kb = ReplyKeyboardBuilder()
    kb.button(text="❌ Отмена")
    return kb.as_markup(resize_keyboard=True)

def result_links_kb(name: str) -> InlineKeyboardMarkup:
    q  = urllib.parse.quote(name)
    q2 = name.replace(" ", "+")
    kb = InlineKeyboardBuilder()
    kb.button(text="🟣 CryptoRank",  url=f"https://cryptorank.io/people?search={q}")
    kb.button(text="🔵 LinkedIn",    url=f"https://www.linkedin.com/search/results/people/?keywords={q}")
    kb.button(text="🔴 VKontakte",   url=f"https://vk.com/search?c%5Bq%5D={q}&c%5Bsection%5D=people")
    kb.button(text="⚫ Twitter/X",   url=f"https://twitter.com/search?q=%22{q}%22+crypto")
    kb.button(text="🟡 Google",      url=f"https://www.google.com/search?q=%22{q2}%22+crypto+OR+bitcoin")
    kb.button(text="🟠 Yandex",      url=f"https://yandex.ru/search/?text=%22{q2}%22+крипто+OR+инвест")
    kb.button(text="🏢 HH.ru",       url=f"https://hh.ru/search/resume?text={q}")
    kb.button(text="📊 RusProfile",  url=f"https://www.rusprofile.ru/search?query={q}&type=fiz")
    kb.adjust(2, 2, 2, 2)
    return kb.as_markup()

# ── HTTP клиент ───────────────────────────────────────────────────────────────
async def fetch(url: str, *, method="GET", data=None, timeout=15.0) -> str | None:
    try:
        async with httpx.AsyncClient(
            timeout=timeout, follow_redirects=True, headers=HEADERS
        ) as client:
            if method == "POST":
                r = await client.post(url, data=data)
            else:
                r = await client.get(url)
            if r.status_code == 200:
                return r.text
    except Exception as e:
        print(f"[fetch error] {url}: {e}")
    return None

# ── Парсинг DDG ───────────────────────────────────────────────────────────────
def parse_ddg_html(html: str, max_results: int = 8) -> list[dict]:
    results = []
    seen = set()

    # Попытка 1: блоки result__body
    links = re.findall(
        r'class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>',
        html, re.DOTALL
    )
    snippets = re.findall(
        r'class="result__snippet"[^>]*>(.*?)</(?:span|a|div)>',
        html, re.DOTALL
    )

    for i, (href, title_html) in enumerate(links[:max_results]):
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
        if not href.startswith("http") or not title or href in seen:
            continue
        seen.add(href)
        snippet = re.sub(r"<[^>]+>", "", snippets[i]).strip()[:250] if i < len(snippets) else ""
        results.append({"title": title, "url": href, "snippet": snippet})

    return results

async def ddg(query: str, max_results: int = 8) -> list[dict]:
    html = await fetch(
        "https://html.duckduckgo.com/html/",
        method="POST",
        data={"q": query, "b": "", "kl": "ru-ru"},
        timeout=20.0,
    )
    if not html:
        return []
    return parse_ddg_html(html, max_results)

# ── Источники поиска ──────────────────────────────────────────────────────────

async def search_cryptorank(name: str) -> list[dict]:
    found = []

    # DDG site:cryptorank.io
    results = await ddg(f'"{name}" site:cryptorank.io', max_results=4)
    for r in results:
        if "cryptorank.io" in r["url"] and "/people" in r["url"]:
            found.append({
                "source": "CryptoRank",
                "name": name,
                "url": r["url"],
                "snippet": r.get("snippet", ""),
            })

    # Прямая страница поиска CryptoRank
    html = await fetch(f"https://cryptorank.io/people?search={urllib.parse.quote(name)}")
    if html:
        # Ищем ссылки на профили людей
        profiles = re.findall(r'href="(/people/[^"]+)"', html)
        for slug in list(dict.fromkeys(profiles))[:3]:
            url = f"https://cryptorank.io{slug}"
            if url not in [r["url"] for r in found]:
                # Пытаемся вытащить имя и должность с профиля
                phtml = await fetch(url)
                snippet = ""
                if phtml:
                    name_m = re.search(r'<h1[^>]*>([^<]+)</h1>', phtml)
                    role_m = re.search(r'<div[^>]*class="[^"]*role[^"]*"[^>]*>([^<]+)</div>', phtml, re.I)
                    if name_m:
                        snippet = name_m.group(1).strip()
                    if role_m:
                        snippet += " · " + role_m.group(1).strip()
                found.append({"source": "CryptoRank", "name": name, "url": url, "snippet": snippet})

    return found


async def search_linkedin(name: str) -> list[dict]:
    results = await ddg(f'"{name}" site:linkedin.com/in', max_results=4)
    found = []
    for r in results:
        if "linkedin.com/in/" in r["url"]:
            found.append({
                "source": "LinkedIn",
                "name": r["title"].replace(" | LinkedIn", "").replace(" - LinkedIn", "").strip(),
                "url": r["url"],
                "snippet": r.get("snippet", ""),
            })
    return found


async def search_vk(name: str) -> list[dict]:
    results = await ddg(f'"{name}" site:vk.com', max_results=4)
    found = []
    for r in results:
        if "vk.com/" in r["url"] and "/wall" not in r["url"] and "/photo" not in r["url"]:
            found.append({
                "source": "VKontakte",
                "name": r["title"],
                "url": r["url"],
                "snippet": r.get("snippet", ""),
            })
    return found


async def search_twitter(name: str) -> list[dict]:
    results = await ddg(f'"{name}" site:twitter.com OR site:x.com crypto OR bitcoin OR web3', max_results=3)
    found = []
    for r in results:
        if "twitter.com/" in r["url"] or "x.com/" in r["url"]:
            found.append({
                "source": "Twitter/X",
                "name": r["title"],
                "url": r["url"],
                "snippet": r.get("snippet", ""),
            })
    return found


async def search_hh(name: str) -> list[dict]:
    results = await ddg(f'"{name}" site:hh.ru', max_results=3)
    found = []
    for r in results:
        if "hh.ru/" in r["url"]:
            found.append({
                "source": "HH.ru",
                "name": r["title"],
                "url": r["url"],
                "snippet": r.get("snippet", ""),
            })
    return found


async def search_rusprofile(name: str) -> list[dict]:
    results = await ddg(f'"{name}" site:rusprofile.ru', max_results=3)
    found = []
    for r in results:
        if "rusprofile.ru" in r["url"]:
            found.append({
                "source": "RusProfile",
                "name": r["title"],
                "url": r["url"],
                "snippet": r.get("snippet", ""),
            })
    return found


async def search_crypto_articles(name: str, phone: str = "") -> list[dict]:
    """Статьи / упоминания в крипто-медиа."""
    crypto_kw = (
        'crypto OR bitcoin OR BTC OR blockchain OR web3 OR investor '
        'OR "venture capital" OR VC OR DeFi OR NFT OR fund OR стартап OR инвестор'
    )
    queries = [
        f'"{name}" ({crypto_kw})',
        f'"{name}" инвестиции OR криптовалюта OR блокчейн',
    ]
    if phone:
        queries.append(f'"{name}" "{phone}"')

    seen, found = set(), []
    tasks = [ddg(q, max_results=5) for q in queries]
    batches = await asyncio.gather(*tasks, return_exceptions=True)
    for batch in batches:
        if not isinstance(batch, list):
            continue
        for r in batch:
            if r["url"] not in seen:
                seen.add(r["url"])
                found.append({"source": "Web", **r})
    return found


# ── Полный поиск ─────────────────────────────────────────────────────────────

async def full_search(name: str, phone: str = "") -> dict:
    tasks = {
        "cr":       search_cryptorank(name),
        "linkedin": search_linkedin(name),
        "vk":       search_vk(name),
        "twitter":  search_twitter(name),
        "hh":       search_hh(name),
        "rusprofile": search_rusprofile(name),
        "articles": search_crypto_articles(name, phone),
    }
    results = await asyncio.gather(*tasks.values(), return_exceptions=True)
    out = {}
    for key, res in zip(tasks.keys(), results):
        out[key] = res if isinstance(res, list) else []
    return out


# ── Форматирование результата ─────────────────────────────────────────────────

SOURCE_ICONS = {
    "CryptoRank": "🟣",
    "LinkedIn":   "🔵",
    "VKontakte":  "🔴",
    "Twitter/X":  "⚫",
    "HH.ru":      "🏢",
    "RusProfile": "📊",
    "Web":        "🌐",
}

def format_profile(r: dict) -> str:
    icon = SOURCE_ICONS.get(r["source"], "🔗")
    line = f'{icon} <b>{r["source"]}</b>: <a href="{r["url"]}">{r.get("name","")[:60]}</a>'
    if r.get("snippet"):
        line += f'\n    <i>{r["snippet"][:200]}</i>'
    return line

def build_report(name: str, phone: str, res: dict) -> str:
    cr_hits    = res.get("cr", [])
    li_hits    = res.get("linkedin", [])
    vk_hits    = res.get("vk", [])
    tw_hits    = res.get("twitter", [])
    hh_hits    = res.get("hh", [])
    rp_hits    = res.get("rusprofile", [])
    art_hits   = res.get("articles", [])

    total = sum(len(v) for v in res.values())

    parts = [
        f"<b>👤 {name}</b>" + (f"  |  <code>{phone}</code>" if phone else ""),
        f"<i>Найдено профилей/упоминаний: {total}</i>",
        "─────────────────────",
    ]

    if cr_hits:
        parts.append(f"🟣 <b>CryptoRank</b> — {len(cr_hits)} профил(я):")
        for r in cr_hits[:3]:
            parts.append(f'  • <a href="{r["url"]}">{r.get("snippet") or r["url"]}</a>')
    else:
        parts.append("🟣 <b>CryptoRank</b>: не найден")

    if li_hits:
        parts.append(f"\n🔵 <b>LinkedIn</b> — {len(li_hits)} профил(я):")
        for r in li_hits[:3]:
            parts.append(format_profile(r))

    if vk_hits:
        parts.append(f"\n🔴 <b>ВКонтакте</b> — {len(vk_hits)} профил(я):")
        for r in vk_hits[:3]:
            parts.append(format_profile(r))

    if tw_hits:
        parts.append(f"\n⚫ <b>Twitter/X</b> — {len(tw_hits)}:")
        for r in tw_hits[:2]:
            parts.append(format_profile(r))

    if hh_hits:
        parts.append(f"\n🏢 <b>HH.ru</b> — {len(hh_hits)} резюме:")
        for r in hh_hits[:2]:
            parts.append(format_profile(r))

    if rp_hits:
        parts.append(f"\n📊 <b>RusProfile</b> — {len(rp_hits)} записей:")
        for r in rp_hits[:2]:
            parts.append(format_profile(r))

    if art_hits:
        parts.append(f"\n🌐 <b>Крипто/инвест статьи</b> — {len(art_hits)} упоминаний:")
        for r in art_hits[:5]:
            parts.append(format_profile(r))

    if total == 0:
        parts.append(
            "\n⚠️ <i>Ничего не найдено в открытых источниках.</i>\n"
            "Попробуй добавить город, должность или другой вариант написания имени."
        )

    return "\n".join(parts)

def split_msg(text: str) -> list[str]:
    if len(text) <= 4096:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 4000:
            chunks.append(buf)
            buf = line
        else:
            buf += ("\n" if buf else "") + line
    if buf:
        chunks.append(buf)
    return chunks

# ── Парсинг файла базы ────────────────────────────────────────────────────────

def parse_file(content: bytes, filename: str) -> list[dict]:
    ext  = filename.lower().rsplit(".", 1)[-1]
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
                or f"{item.get('first_name','').strip()} {item.get('last_name','').strip()}".strip()
                or f"{item.get('имя','').strip()} {item.get('фамилия','').strip()}".strip()
            )
            phone = str(item.get("phone", item.get("tel", item.get("телефон", "")))).strip()
            if name and len(name.strip()) > 2:
                people.append({"name": name.strip(), "phone": phone})

    elif ext == "csv":
        reader = csv.DictReader(io.StringIO(text))
        for row in reader:
            row = {k.lower().strip(): str(v).strip() for k, v in row.items()}
            name = (
                row.get("name") or row.get("fullname") or row.get("full_name")
                or f"{row.get('first_name','')} {row.get('last_name','')}".strip()
                or f"{row.get('имя','')} {row.get('фамилия','')}".strip()
            )
            phone = row.get("phone", row.get("tel", row.get("телефон", "")))
            if name and len(name.strip()) > 2:
                people.append({"name": name.strip(), "phone": phone or ""})

    else:  # TXT
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            phone_m = re.search(r"\+?\d[\d\-\(\)\s]{8,14}", line)
            phone   = phone_m.group(0).strip() if phone_m else ""
            name    = re.sub(r"\+?\d[\d\-\(\)\s]{8,14}", "", line).strip(" ,|;:")
            if name and len(name) > 3:
                people.append({"name": name, "phone": phone})

    return people

# ── Команды / хэндлеры ───────────────────────────────────────────────────────

@dp.message(Command("start"))
@dp.message(F.text == "❌ Отмена")
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>Бот поиска людей</b>\n\n"
        "Ищу по <b>CryptoRank, LinkedIn, ВКонтакте, Twitter, HH.ru, RusProfile</b> и крипто-СМИ.\n\n"
        "Выбери действие:",
        reply_markup=main_kb(),
    )

# ── Поиск через кнопку ────────────────────────────────────────────────────────

@dp.message(F.text == "🔍 Найти человека")
async def btn_search(message: types.Message, state: FSMContext):
    await state.set_state(Search.waiting_name)
    await message.answer(
        "✏️ Введи <b>Имя Фамилию</b> человека:\n\n"
        "<i>Например: Иван Петров</i>",
        reply_markup=cancel_kb(),
    )

@dp.message(StateFilter(Search.waiting_name))
async def got_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name.split()) < 2:
        await message.answer("⚠️ Нужно минимум <b>имя и фамилия</b>. Попробуй ещё раз:")
        return

    await state.update_data(name=name)
    await state.set_state(Search.waiting_phone)
    await message.answer(
        f"📱 Есть номер телефона для <b>{name}</b>?\n\n"
        "Введи номер <i>(например: +79001234567)</i>\n"
        "или нажми <b>Пропустить →</b>",
        reply_markup=ReplyKeyboardMarkup(
            keyboard=[[KeyboardButton(text="➡️ Пропустить"), KeyboardButton(text="❌ Отмена")]],
            resize_keyboard=True,
        ),
    )

@dp.message(StateFilter(Search.waiting_phone))
async def got_phone(message: types.Message, state: FSMContext):
    data  = await state.get_data()
    name  = data["name"]
    phone = "" if message.text.strip() in ("➡️ Пропустить", "❌ Отмена") else message.text.strip()

    if message.text.strip() == "❌ Отмена":
        await state.clear()
        await message.answer("Отменено.", reply_markup=main_kb())
        return

    await state.clear()
    await _do_search(message, name, phone)

async def _do_search(message: types.Message, name: str, phone: str = ""):
    status = await message.answer(
        f"⏳ <b>Ищу: {name}</b>\n\n"
        "🟣 CryptoRank...\n"
        "🔵 LinkedIn...\n"
        "🔴 VK, Twitter...\n"
        "🌐 Крипто-СМИ, HH, RusProfile...",
        reply_markup=ReplyKeyboardRemove(),
    )

    try:
        res = await asyncio.wait_for(full_search(name, phone), timeout=50.0)
    except asyncio.TimeoutError:
        await status.edit_text(
            f"⚠️ <b>Таймаут поиска для {name}.</b>\nПопробуй ещё раз.",
        )
        await message.answer("Выбери действие:", reply_markup=main_kb())
        return

    # Сохраняем в CryptoRank файл
    if res.get("cr"):
        save_cr({
            "name":       name,
            "phone":      phone,
            "timestamp":  datetime.utcnow().isoformat(),
            "cr_count":   len(res["cr"]),
            "cr_results": res["cr"],
        })

    report = build_report(name, phone, res)
    await status.delete()

    chunks = split_msg(report)
    for i, chunk in enumerate(chunks):
        if i == len(chunks) - 1:
            await message.answer(chunk, reply_markup=result_links_kb(name), disable_web_page_preview=True)
        else:
            await message.answer(chunk, disable_web_page_preview=True)

    await message.answer("Выбери действие:", reply_markup=main_kb())

# ── Команда /search (как альтернатива) ───────────────────────────────────────

@dp.message(Command("search"))
async def cmd_search(message: types.Message, state: FSMContext):
    raw = message.text.split(maxsplit=1)
    if len(raw) < 2 or len(raw[1].strip().split()) < 2:
        await state.set_state(Search.waiting_name)
        await message.answer("✏️ Введи <b>Имя Фамилию</b>:", reply_markup=cancel_kb())
        return
    parts = raw[1].strip().split()
    phone, name_parts = "", []
    for p in parts:
        if re.match(r"^\+?\d{7,15}$", p):
            phone = p
        else:
            name_parts.append(p)
    if len(name_parts) < 2:
        await message.answer("⚠️ Нужно минимум имя и фамилия.")
        return
    await _do_search(message, " ".join(name_parts), phone)

# ── CryptoRank список ─────────────────────────────────────────────────────────

@dp.message(F.text == "📋 CryptoRank список")
@dp.message(Command("found"))
async def btn_found(message: types.Message, state: FSMContext):
    await state.clear()
    data = load_cr()
    if not data:
        await message.answer(
            "📂 <b>CryptoRank: пусто</b>\n\nНикого ещё не нашли. Попробуй поиск.",
            reply_markup=main_kb(),
        )
        return
    lines = [f"📋 <b>Найдены на CryptoRank ({len(data)}):</b>\n"]
    for i, e in enumerate(data, 1):
        n  = e.get("name", "—")
        ts = e.get("timestamp", "")[:10]
        c  = e.get("cr_count", 0)
        lines.append(f"{i}. <b>{n}</b> · {c} рез. [{ts}]")
        for r in e.get("cr_results", [])[:1]:
            lines.append(f"   🔗 <a href='{r.get('url','')}'>{r.get('url','')[:60]}</a>")
    await message.answer("\n".join(lines), reply_markup=main_kb(), disable_web_page_preview=True)

# ── Загрузка базы ─────────────────────────────────────────────────────────────

@dp.message(F.text == "📂 Загрузить базу")
async def btn_upload(message: types.Message):
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        await message.answer("❌ Только администратор может загружать файлы.")
        return
    await message.answer(
        "📂 <b>Загрузка базы</b>\n\n"
        "Пришли файл (JSON / CSV / TXT).\n\n"
        "<b>Ожидаемые поля:</b>\n"
        "• JSON: <code>name</code> / <code>fullName</code> / <code>first_name + last_name</code>\n"
        "• CSV: колонки <code>name</code>, <code>phone</code>\n"
        "• TXT: по одному имени на строку\n\n"
        "Бот автоматически начнёт поиск по каждому человеку.",
        reply_markup=cancel_kb(),
    )

@dp.message(F.document)
async def handle_file(message: types.Message, state: FSMContext):
    await state.clear()

    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        await message.answer("❌ Только администратор может загружать файлы.")
        return

    doc      = message.document
    filename = doc.file_name or "file.txt"
    ext      = filename.lower().rsplit(".", 1)[-1]

    if ext not in ("json", "csv", "txt"):
        await message.answer(f"⚠️ Формат <b>.{ext}</b> не поддерживается. Используй JSON, CSV или TXT.")
        return

    status = await message.answer(f"📥 Загружаю <b>{filename}</b>...", reply_markup=ReplyKeyboardRemove())
    try:
        file = await bot.get_file(doc.file_id)
        buf  = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        people = parse_file(buf.getvalue(), filename)
    except Exception as e:
        await status.edit_text(f"❌ Ошибка: {e}")
        await message.answer("Выбери действие:", reply_markup=main_kb())
        return

    if not people:
        await status.edit_text(
            "⚠️ Не удалось найти имена в файле.\n"
            "Проверь структуру: нужны поля <code>name</code>, <code>fullName</code> или <code>first_name/last_name</code>."
        )
        await message.answer("Выбери действие:", reply_markup=main_kb())
        return

    await status.edit_text(
        f"✅ <b>Файл принят!</b>\n"
        f"👥 Найдено: <b>{len(people)}</b> человек\n\n"
        "🚀 Начинаю поиск по каждому..."
    )

    cr_found = 0
    for i, person in enumerate(people, 1):
        name  = person["name"]
        phone = person.get("phone", "")

        progress = await message.answer(
            f"⏳ <b>[{i}/{len(people)}]</b> Ищу: <b>{name}</b>"
            + (f" · <code>{phone}</code>" if phone else "")
        )

        try:
            res = await asyncio.wait_for(full_search(name, phone), timeout=50.0)
        except asyncio.TimeoutError:
            await progress.edit_text(
                f"⚠️ <b>[{i}/{len(people)}] {name}</b> — таймаут, пропускаю."
            )
            await asyncio.sleep(2)
            continue

        if res.get("cr"):
            cr_found += 1
            save_cr({
                "name":       name,
                "phone":      phone,
                "timestamp":  datetime.utcnow().isoformat(),
                "cr_count":   len(res["cr"]),
                "cr_results": res["cr"],
            })

        await progress.delete()
        report = build_report(name, phone, res)
        for chunk in split_msg(report):
            await message.answer(chunk, disable_web_page_preview=True)
        await message.answer(
            f"🔗 <b>Ссылки для {name}:</b>",
            reply_markup=result_links_kb(name),
        )

        # Пауза между запросами (защита от блокировки DDG)
        await asyncio.sleep(4)

    # Итог
    summary_lines = [
        f"✅ <b>Готово!</b>",
        f"👥 Обработано: <b>{len(people)}</b> человек",
        f"🟣 Найдено на CryptoRank: <b>{cr_found}</b>",
    ]
    await message.answer("\n".join(summary_lines), reply_markup=main_kb())

    # Отправляем итоговый файл если есть находки
    if cr_found > 0 and os.path.exists(CR_FILE):
        await message.answer_document(
            FSInputFile(CR_FILE),
            caption=f"📁 cryptorank_results.json · {cr_found} новых записей"
        )

# ── Помощь ────────────────────────────────────────────────────────────────────

@dp.message(F.text == "❓ Помощь")
@dp.message(Command("help"))
async def btn_help(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "❓ <b>Справка</b>\n\n"
        "<b>Источники поиска:</b>\n"
        "🟣 CryptoRank — профили крипто-деятелей\n"
        "🔵 LinkedIn — профессиональные профили\n"
        "🔴 ВКонтакте — соцсеть\n"
        "⚫ Twitter/X — крипто-упоминания\n"
        "🏢 HH.ru — резюме\n"
        "📊 RusProfile — реестр юрлиц/ИП\n"
        "🌐 Крипто-СМИ — статьи и публикации\n\n"
        "<b>Как искать:</b>\n"
        "Нажми <b>🔍 Найти человека</b> → введи имя → (опционально) телефон\n\n"
        "<b>Загрузка базы:</b>\n"
        "Нажми <b>📂 Загрузить базу</b> и пришли файл.\n"
        "Бот обойдёт всех и выдаст результаты.\n\n"
        "<b>Форматы файла:</b> JSON · CSV · TXT",
        reply_markup=main_kb(),
    )

# ── Запуск ────────────────────────────────────────────────────────────────────

async def main():
    print("✅ Бот запущен.")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
