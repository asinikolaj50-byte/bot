import os, re, json, asyncio, io, csv, urllib.parse
from datetime import datetime
from aiogram import Bot, Dispatcher, types, F
from aiogram.filters import Command, StateFilter
from aiogram.fsm.context import FSMContext
from aiogram.fsm.state import State, StatesGroup
from aiogram.fsm.storage.memory import MemoryStorage
from aiogram.client.default import DefaultBotProperties
from aiogram.types import InlineKeyboardMarkup, ReplyKeyboardRemove, FSInputFile
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
    "Accept-Language": "en-US,en;q=0.9",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
}

# ── FSM States ────────────────────────────────────────────────────────────────
class Search(StatesGroup):
    waiting_name  = State()
    waiting_phone = State()

# ── CryptoRank storage ────────────────────────────────────────────────────────
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
    if entry["name"].lower() not in {r.get("name", "").lower() for r in data}:
        data.append(entry)
        with open(CR_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)

# ── Keyboards ─────────────────────────────────────────────────────────────────

def main_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🔍 Найти человека",       callback_data="start_search")
    kb.button(text="📂 Загрузить базу",        callback_data="upload_info")
    kb.button(text="📋 Найдены на CryptoRank", callback_data="show_found")
    kb.button(text="❓ Помощь",               callback_data="show_help")
    kb.adjust(2, 2)
    return kb.as_markup()

def phone_kb(name: str) -> InlineKeyboardMarkup:
    """Кнопки вместо текстового ввода телефона."""
    kb = InlineKeyboardBuilder()
    kb.button(text="➡️ Пропустить (телефона нет)", callback_data="phone_skip")
    kb.button(text="❌ Отмена",                     callback_data="cancel")
    kb.adjust(1)
    return kb.as_markup()

def confirm_search_kb(name: str, phone: str) -> InlineKeyboardMarkup:
    """Подтверждение перед поиском."""
    kb = InlineKeyboardBuilder()
    kb.button(text="✅ Искать",  callback_data="confirm_search")
    kb.button(text="❌ Отмена", callback_data="cancel")
    kb.adjust(2)
    return kb.as_markup()

def result_links_kb(name: str) -> InlineKeyboardMarkup:
    q  = urllib.parse.quote(name)
    q2 = name.replace(" ", "+")
    kb = InlineKeyboardBuilder()
    kb.button(text="🟣 CryptoRank",   url=f"https://cryptorank.io/people?search={q}")
    kb.button(text="🔵 LinkedIn",     url=f"https://www.linkedin.com/search/results/people/?keywords={q}")
    kb.button(text="⚫ Twitter / X",  url=f"https://twitter.com/search?q=%22{q}%22+crypto")
    kb.button(text="🟠 Crunchbase",   url=f"https://www.crunchbase.com/search/people/field/persons/facet_ids/{q2}")
    kb.button(text="🟢 AngelList",    url=f"https://wellfound.com/search?q={q}")
    kb.button(text="🔗 Google",       url=f"https://www.google.com/search?q=%22{q2}%22+crypto+OR+bitcoin+OR+web3")
    kb.button(text="🔍 Новый поиск",  callback_data="start_search")
    kb.button(text="🏠 Меню",         callback_data="main_menu")
    kb.adjust(2, 2, 2, 2)
    return kb.as_markup()

def back_menu_kb() -> InlineKeyboardMarkup:
    kb = InlineKeyboardBuilder()
    kb.button(text="🏠 Главное меню", callback_data="main_menu")
    kb.adjust(1)
    return kb.as_markup()

# ── HTTP ──────────────────────────────────────────────────────────────────────
async def fetch(url: str, *, method="GET", data=None, timeout=15.0) -> str | None:
    try:
        async with httpx.AsyncClient(timeout=timeout, follow_redirects=True, headers=HEADERS) as client:
            r = await client.post(url, data=data) if method == "POST" else await client.get(url)
            if r.status_code == 200:
                return r.text
    except Exception as e:
        print(f"[fetch] {url}: {e}")
    return None

# ── DDG parser ────────────────────────────────────────────────────────────────
def parse_ddg(html: str, max_results: int = 8) -> list[dict]:
    seen, out = set(), []
    links    = re.findall(r'class="result__a"[^>]+href="([^"]+)"[^>]*>(.*?)</a>', html, re.DOTALL)
    snippets = re.findall(r'class="result__snippet"[^>]*>(.*?)</(?:span|a|div)>', html, re.DOTALL)
    for i, (href, title_html) in enumerate(links[:max_results]):
        title = re.sub(r"<[^>]+>", "", title_html).strip()
        if "uddg=" in href:
            href = urllib.parse.unquote(href.split("uddg=")[-1].split("&")[0])
        if not href.startswith("http") or not title or href in seen:
            continue
        seen.add(href)
        snip = re.sub(r"<[^>]+>", "", snippets[i]).strip()[:250] if i < len(snippets) else ""
        out.append({"title": title, "url": href, "snippet": snip})
    return out

async def ddg(query: str, max_results: int = 8) -> list[dict]:
    html = await fetch(
        "https://html.duckduckgo.com/html/",
        method="POST",
        data={"q": query, "b": "", "kl": "en-us"},
        timeout=20.0,
    )
    return parse_ddg(html, max_results) if html else []

# ── Crypto relevance filter ───────────────────────────────────────────────────

CRYPTO_TERMS = {
    "crypto", "cryptocurrency", "bitcoin", "btc", "ethereum", "eth", "blockchain",
    "web3", "defi", "nft", "token", "altcoin", "ico", "ido", "dao", "dex", "cefi",
    "investor", "venture", "vc", "fund", "fintech", "startup", "angel",
    "coinbase", "binance", "kraken", "solana", "polygon", "avalanche",
    "инвестор", "криптовалюта", "блокчейн", "инвестиции", "стартап",
}

def is_crypto_relevant(r: dict) -> bool:
    """Возвращает True только если результат реально связан с крипто/инвест тематикой."""
    haystack = (r.get("title", "") + " " + r.get("snippet", "")).lower()
    return any(term in haystack for term in CRYPTO_TERMS)

def clean_title(title: str, *remove_suffixes: str) -> str:
    """Убирает шаблонные суффиксы из заголовков (- LinkedIn, | Crunchbase и т.д.)"""
    for suffix in remove_suffixes:
        title = re.sub(rf"\s*[\|—\-]\s*{re.escape(suffix)}.*", "", title, flags=re.I)
    return title.strip()

# ── Search sources ────────────────────────────────────────────────────────────

async def search_cryptorank(name: str) -> list[dict]:
    found = []
    # DDG site:cryptorank.io
    for r in await ddg(f'"{name}" site:cryptorank.io/people', max_results=4):
        if "cryptorank.io" in r["url"]:
            found.append({"source": "CryptoRank", "name": name, "url": r["url"], "snippet": r.get("snippet", "")})
    # Direct page scrape
    html = await fetch(f"https://cryptorank.io/people?search={urllib.parse.quote(name)}")
    if html:
        for slug in list(dict.fromkeys(re.findall(r'href="(/people/[^"?#]+)"', html)))[:3]:
            url = f"https://cryptorank.io{slug}"
            if url in [r["url"] for r in found]:
                continue
            snippet = ""
            phtml = await fetch(url)
            if phtml:
                m1 = re.search(r'<h1[^>]*>([^<]+)</h1>', phtml)
                m2 = re.search(r'"jobTitle"\s*:\s*"([^"]+)"', phtml)
                m3 = re.search(r'"worksFor"\s*:\s*\{[^}]*"name"\s*:\s*"([^"]+)"', phtml)
                parts = [x.group(1).strip() for x in [m1, m2, m3] if x]
                snippet = " · ".join(parts)
            found.append({"source": "CryptoRank", "name": name, "url": url, "snippet": snippet})
    return found

async def search_linkedin(name: str) -> list[dict]:
    found = []
    # Ищем с крипто-контекстом — только реальные крипто-профили
    for r in await ddg(
        f'"{name}" site:linkedin.com/in crypto OR blockchain OR web3 OR investor OR fintech OR DeFi OR venture',
        max_results=5,
    ):
        if "linkedin.com/in/" not in r["url"]:
            continue
        if not is_crypto_relevant(r):
            continue
        clean = clean_title(r["title"], "LinkedIn", "| LinkedIn")
        found.append({"source": "LinkedIn", "name": clean, "url": r["url"], "snippet": r.get("snippet", "")})
    return found

async def search_twitter(name: str) -> list[dict]:
    found = []
    for r in await ddg(
        f'"{name}" (site:twitter.com OR site:x.com) crypto OR bitcoin OR web3 OR investor OR blockchain',
        max_results=4,
    ):
        if "twitter.com/" not in r["url"] and "x.com/" not in r["url"]:
            continue
        if not is_crypto_relevant(r):
            continue
        found.append({"source": "Twitter/X", "name": clean_title(r["title"], "on X", "on Twitter"), "url": r["url"], "snippet": r.get("snippet", "")})
    return found

async def search_crunchbase(name: str) -> list[dict]:
    found = []
    for r in await ddg(f'"{name}" site:crunchbase.com/person', max_results=4):
        if "crunchbase.com/person/" not in r["url"]:
            continue
        # Crunchbase уже подразумевает стартап/инвест контекст, но проверим
        if not is_crypto_relevant(r):
            continue
        found.append({"source": "Crunchbase", "name": clean_title(r["title"], "Crunchbase"), "url": r["url"], "snippet": r.get("snippet", "")})
    return found

async def search_angellist(name: str) -> list[dict]:
    found = []
    for r in await ddg(f'"{name}" site:wellfound.com crypto OR blockchain OR web3 OR DeFi', max_results=3):
        if "wellfound.com/" not in r["url"] and "angel.co/" not in r["url"]:
            continue
        if not is_crypto_relevant(r):
            continue
        found.append({"source": "AngelList", "name": clean_title(r["title"], "Wellfound", "AngelList"), "url": r["url"], "snippet": r.get("snippet", "")})
    return found

async def search_github(name: str) -> list[dict]:
    found = []
    for r in await ddg(f'"{name}" site:github.com blockchain OR web3 OR crypto OR solidity OR ethereum', max_results=3):
        if "github.com/" not in r["url"]:
            continue
        # Только профили пользователей, не репозитории и не issue
        if any(x in r["url"] for x in ["/commit/", "/issues/", "/pull/", "/blob/"]):
            continue
        if not is_crypto_relevant(r):
            continue
        found.append({"source": "GitHub", "name": clean_title(r["title"], "GitHub"), "url": r["url"], "snippet": r.get("snippet", "")})
    return found

async def search_articles(name: str, phone: str = "") -> list[dict]:
    kw = (
        'crypto OR bitcoin OR BTC OR blockchain OR web3 OR DeFi OR NFT '
        'OR "venture capital" OR VC OR fund OR fintech OR "crypto investor" OR "blockchain founder"'
    )
    queries = [f'"{name}" ({kw})']
    if phone:
        queries.append(f'"{name}" "{phone}" crypto OR blockchain')
    seen, found = set(), []
    for batch in await asyncio.gather(*[ddg(q, max_results=6) for q in queries], return_exceptions=True):
        if not isinstance(batch, list):
            continue
        for r in batch:
            if r["url"] in seen:
                continue
            # Строгий фильтр: крипто-слова должны быть и в заголовке/сниппете
            if not is_crypto_relevant(r):
                continue
            seen.add(r["url"])
            found.append({"source": "Article", **r})
    return found

# ── Full parallel search ──────────────────────────────────────────────────────
async def full_search(name: str, phone: str = "") -> dict:
    keys = ["cr", "linkedin", "twitter", "crunchbase", "angellist", "github", "articles"]
    coros = [
        search_cryptorank(name),
        search_linkedin(name),
        search_twitter(name),
        search_crunchbase(name),
        search_angellist(name),
        search_github(name),
        search_articles(name, phone),
    ]
    results = await asyncio.gather(*coros, return_exceptions=True)
    return {k: (v if isinstance(v, list) else []) for k, v in zip(keys, results)}

# ── Report formatting ─────────────────────────────────────────────────────────

def _label(r: dict) -> str:
    """Возвращает читаемое имя/заголовок для отображения."""
    return (r.get("name") or r.get("title") or "—")[:80]

def _snip(r: dict, limit: int = 160) -> str:
    s = r.get("snippet", "").strip()
    if not s:
        return ""
    # Убираем лишние пробелы и переносы
    s = re.sub(r"\s+", " ", s)
    return s[:limit]

def fmt_block(r: dict, icon: str) -> str:
    label = _label(r)
    snip  = _snip(r)
    line  = f'{icon} <a href="{r["url"]}">{label}</a>'
    if snip:
        line += f"\n     <i>{snip}</i>"
    return line

def build_report(name: str, phone: str, res: dict) -> str:
    cr  = res.get("cr", [])
    li  = res.get("linkedin", [])
    tw  = res.get("twitter", [])
    cb  = res.get("crunchbase", [])
    al  = res.get("angellist", [])
    gh  = res.get("github", [])
    art = res.get("articles", [])

    crypto_found = bool(cr or li or cb or al or tw or gh or art)

    # ── Шапка ──
    header = f"<b>👤 {name}</b>"
    if phone:
        header += f"   <code>{phone}</code>"

    # Статус по CryptoRank
    if cr:
        header += "\n🟣 <b>Есть на CryptoRank</b> ✅"
    else:
        header += "\n🟣 CryptoRank — не найден"

    lines = [header, ""]

    # ── CryptoRank профили ──
    if cr:
        lines.append("<b>Профили на CryptoRank:</b>")
        for r in cr[:3]:
            lines.append(fmt_block(r, "  ▸"))
        lines.append("")

    # ── LinkedIn ──
    if li:
        lines.append("<b>LinkedIn:</b>")
        for r in li[:3]:
            lines.append(fmt_block(r, "  🔵"))
        lines.append("")

    # ── Crunchbase ──
    if cb:
        lines.append("<b>Crunchbase:</b>")
        for r in cb[:3]:
            lines.append(fmt_block(r, "  🟠"))
        lines.append("")

    # ── AngelList ──
    if al:
        lines.append("<b>AngelList / Wellfound:</b>")
        for r in al[:2]:
            lines.append(fmt_block(r, "  🟢"))
        lines.append("")

    # ── Twitter ──
    if tw:
        lines.append("<b>Twitter / X:</b>")
        for r in tw[:2]:
            lines.append(fmt_block(r, "  ⚫"))
        lines.append("")

    # ── GitHub ──
    if gh:
        lines.append("<b>GitHub:</b>")
        for r in gh[:2]:
            lines.append(fmt_block(r, "  ⬛"))
        lines.append("")

    # ── Статьи ──
    if art:
        lines.append("<b>Упоминания в крипто-СМИ:</b>")
        for r in art[:5]:
            lines.append(fmt_block(r, "  🌐"))
        lines.append("")

    # ── Ничего не найдено ──
    if not crypto_found:
        lines.append(
            "⚠️ <i>Открытых крипто-профилей не найдено.</i>\n"
            "Возможно, человек не публичен или используй другое написание имени."
        )

    return "\n".join(lines).strip()

def split_msg(text: str) -> list[str]:
    if len(text) <= 4096:
        return [text]
    chunks, buf = [], ""
    for line in text.split("\n"):
        if len(buf) + len(line) + 1 > 4000:
            chunks.append(buf); buf = line
        else:
            buf += ("\n" if buf else "") + line
    if buf: chunks.append(buf)
    return chunks

# ── File parsing ──────────────────────────────────────────────────────────────
def parse_file(content: bytes, filename: str) -> list[dict]:
    ext  = filename.lower().rsplit(".", 1)[-1]
    text = content.decode("utf-8", errors="replace")
    people = []

    if ext == "json":
        data  = json.loads(text)
        items = data if isinstance(data, list) else data.get("users", data.get("data", []))
        if isinstance(items, dict): items = list(items.values())
        for item in items:
            if not isinstance(item, dict): continue
            name = (
                item.get("name") or item.get("fullName") or item.get("full_name")
                or f"{item.get('first_name','').strip()} {item.get('last_name','').strip()}".strip()
            )
            phone = str(item.get("phone", item.get("tel", ""))).strip()
            if name and len(name.strip()) > 2:
                people.append({"name": name.strip(), "phone": phone})

    elif ext == "csv":
        for row in csv.DictReader(io.StringIO(text)):
            row = {k.lower().strip(): str(v).strip() for k, v in row.items()}
            name = (
                row.get("name") or row.get("fullname") or row.get("full_name")
                or f"{row.get('first_name','')} {row.get('last_name','')}".strip()
            )
            phone = row.get("phone", row.get("tel", ""))
            if name and len(name.strip()) > 2:
                people.append({"name": name.strip(), "phone": phone or ""})

    else:  # TXT
        for line in text.splitlines():
            line = line.strip()
            if not line or line.startswith("#"): continue
            pm    = re.search(r"\+?\d[\d\-\(\)\s]{8,14}", line)
            phone = pm.group(0).strip() if pm else ""
            name  = re.sub(r"\+?\d[\d\-\(\)\s]{8,14}", "", line).strip(" ,|;:")
            if name and len(name) > 3:
                people.append({"name": name, "phone": phone})

    return people

# ── Core search runner ────────────────────────────────────────────────────────
async def _do_search(chat_id: int, name: str, phone: str = ""):
    status = await bot.send_message(
        chat_id,
        f"⏳ <b>Ищу: {name}</b>\n\n"
        "🟣 CryptoRank\n🔵 LinkedIn\n🟠 Crunchbase\n"
        "🟢 AngelList\n⚫ Twitter/X\n⬛ GitHub\n🌐 Статьи...",
    )
    try:
        res = await asyncio.wait_for(full_search(name, phone), timeout=55.0)
    except asyncio.TimeoutError:
        await status.edit_text(f"⚠️ <b>Таймаут для {name}.</b> Попробуй ещё раз.", reply_markup=back_menu_kb())
        return

    if res.get("cr"):
        save_cr({
            "name": name, "phone": phone,
            "timestamp": datetime.utcnow().isoformat(),
            "cr_count": len(res["cr"]),
            "cr_results": res["cr"],
        })

    report = build_report(name, phone, res)
    await status.delete()

    chunks = split_msg(report)
    for i, chunk in enumerate(chunks):
        kb = result_links_kb(name) if i == len(chunks) - 1 else None
        await bot.send_message(chat_id, chunk, reply_markup=kb, disable_web_page_preview=True)

# ── /start & menu ─────────────────────────────────────────────────────────────
@dp.message(Command("start"))
async def cmd_start(message: types.Message, state: FSMContext):
    await state.clear()
    await message.answer(
        "👋 <b>Бот поиска людей</b>\n\n"
        "Ищу по: CryptoRank · LinkedIn · Crunchbase · AngelList · Twitter/X · GitHub · крипто-СМИ\n\n"
        "Выбери действие:",
        reply_markup=main_kb(),
    )

@dp.callback_query(F.data == "main_menu")
async def cb_main_menu(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text(
        "👋 <b>Главное меню</b>\n\nВыбери действие:",
        reply_markup=main_kb(),
    )

@dp.callback_query(F.data == "cancel")
async def cb_cancel(call: types.CallbackQuery, state: FSMContext):
    await state.clear()
    await call.message.edit_text("❌ Отменено.", reply_markup=main_kb())

# ── Search flow (FSM via inline buttons) ──────────────────────────────────────
@dp.callback_query(F.data == "start_search")
async def cb_start_search(call: types.CallbackQuery, state: FSMContext):
    await state.set_state(Search.waiting_name)
    await call.message.edit_text(
        "✏️ <b>Введи имя и фамилию</b> человека:\n\n"
        "<i>Пример: John Smith</i>",
        reply_markup=InlineKeyboardBuilder().button(text="❌ Отмена", callback_data="cancel").as_markup(),
    )

@dp.message(StateFilter(Search.waiting_name))
async def got_name(message: types.Message, state: FSMContext):
    name = message.text.strip()
    if len(name.split()) < 2:
        await message.answer(
            "⚠️ Нужно минимум <b>имя и фамилия</b>.\n<i>Пример: John Smith</i>",
            reply_markup=InlineKeyboardBuilder().button(text="❌ Отмена", callback_data="cancel").as_markup(),
        )
        return

    await state.update_data(name=name)
    await state.set_state(Search.waiting_phone)
    await message.answer(
        f"📱 Есть номер телефона для <b>{name}</b>?\n\n"
        "Если да — введи его.\n"
        "Если нет — нажми кнопку ниже 👇",
        reply_markup=phone_kb(name),
    )

@dp.callback_query(F.data == "phone_skip", StateFilter(Search.waiting_phone))
async def cb_phone_skip(call: types.CallbackQuery, state: FSMContext):
    data = await state.get_data()
    name = data["name"]
    await state.clear()
    await call.message.edit_text(f"🔍 Запускаю поиск: <b>{name}</b>")
    await _do_search(call.message.chat.id, name, "")

@dp.message(StateFilter(Search.waiting_phone))
async def got_phone(message: types.Message, state: FSMContext):
    data  = await state.get_data()
    name  = data["name"]
    phone = message.text.strip()
    await state.clear()
    await message.answer(f"🔍 Запускаю поиск: <b>{name}</b>  <code>{phone}</code>")
    await _do_search(message.chat.id, name, phone)

# ── /search command ───────────────────────────────────────────────────────────
@dp.message(Command("search"))
async def cmd_search(message: types.Message, state: FSMContext):
    raw = message.text.split(maxsplit=1)
    if len(raw) < 2 or len(raw[1].strip().split()) < 2:
        await state.set_state(Search.waiting_name)
        await message.answer(
            "✏️ <b>Введи имя и фамилию:</b>",
            reply_markup=InlineKeyboardBuilder().button(text="❌ Отмена", callback_data="cancel").as_markup(),
        )
        return
    parts = raw[1].strip().split()
    phone, name_parts = "", []
    for p in parts:
        if re.match(r"^\+?\d{7,15}$", p): phone = p
        else: name_parts.append(p)
    if len(name_parts) < 2:
        await message.answer("⚠️ Нужно минимум имя и фамилия."); return
    await _do_search(message.chat.id, " ".join(name_parts), phone)

# ── CryptoRank list ───────────────────────────────────────────────────────────
@dp.callback_query(F.data == "show_found")
@dp.message(Command("found"))
async def show_found(event, state: FSMContext = None):
    if state: await state.clear()
    is_cb = isinstance(event, types.CallbackQuery)
    data  = load_cr()
    if not data:
        text = "📂 <b>CryptoRank: пусто</b>\n\nНикого ещё не нашли."
        if is_cb: await event.message.edit_text(text, reply_markup=back_menu_kb())
        else: await event.answer(text, reply_markup=back_menu_kb())
        return
    lines = [f"📋 <b>Найдены на CryptoRank ({len(data)}):</b>\n"]
    for i, e in enumerate(data, 1):
        n  = e.get("name", "—"); ts = e.get("timestamp", "")[:10]; c = e.get("cr_count", 0)
        lines.append(f"{i}. <b>{n}</b> · {c} рез. [{ts}]")
        for r in e.get("cr_results", [])[:1]:
            lines.append(f"   🔗 <a href='{r.get('url','')}'>{r.get('url','')[:55]}</a>")
    text = "\n".join(lines)
    if is_cb: await event.message.edit_text(text, reply_markup=back_menu_kb(), disable_web_page_preview=True)
    else: await event.answer(text, reply_markup=back_menu_kb(), disable_web_page_preview=True)

# ── Help ──────────────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "show_help")
async def cb_help(call: types.CallbackQuery):
    await call.message.edit_text(
        "❓ <b>Справка</b>\n\n"
        "<b>Источники поиска:</b>\n"
        "🟣 CryptoRank — крипто-профили\n"
        "🔵 LinkedIn — профессиональные профили\n"
        "🟠 Crunchbase — стартапы и инвесторы\n"
        "🟢 AngelList / Wellfound — стартап-сообщество\n"
        "⚫ Twitter / X — крипто-упоминания\n"
        "⬛ GitHub — блокчейн-разработчики\n"
        "🌐 Крипто-СМИ — статьи и публикации\n\n"
        "<b>Как использовать:</b>\n"
        "1. Нажми <b>🔍 Найти человека</b>\n"
        "2. Введи имя и фамилию\n"
        "3. Укажи телефон или пропусти\n"
        "4. Бот выдаст результаты по всем источникам\n\n"
        "<b>Загрузка базы:</b>\n"
        "Нажми <b>📂 Загрузить базу</b> и пришли файл.\n"
        "Поддерживается: JSON · CSV · TXT",
        reply_markup=back_menu_kb(),
    )

# ── Upload info ───────────────────────────────────────────────────────────────
@dp.callback_query(F.data == "upload_info")
async def cb_upload_info(call: types.CallbackQuery):
    if ADMIN_ID and call.from_user.id != ADMIN_ID:
        await call.answer("❌ Только администратор может загружать файлы.", show_alert=True)
        return
    await call.message.edit_text(
        "📂 <b>Загрузка базы</b>\n\n"
        "Пришли файл прямо в этот чат.\n\n"
        "<b>Форматы и структура:</b>\n"
        "• <b>JSON</b>: поля <code>name</code> / <code>fullName</code> / <code>first_name + last_name</code>\n"
        "• <b>CSV</b>: колонки <code>name</code>, <code>phone</code>\n"
        "• <b>TXT</b>: одно имя на строку\n\n"
        "После загрузки бот автоматически начнёт поиск по каждому.",
        reply_markup=back_menu_kb(),
    )

# ── File upload handler ───────────────────────────────────────────────────────
@dp.message(F.document)
async def handle_file(message: types.Message, state: FSMContext):
    await state.clear()
    if ADMIN_ID and message.from_user.id != ADMIN_ID:
        await message.answer("❌ Только администратор может загружать файлы.", reply_markup=back_menu_kb())
        return

    doc = message.document
    filename = doc.file_name or "file.txt"
    ext = filename.lower().rsplit(".", 1)[-1]
    if ext not in ("json", "csv", "txt"):
        await message.answer(f"⚠️ Формат <b>.{ext}</b> не поддерживается. Используй JSON, CSV или TXT.")
        return

    status = await message.answer(f"📥 Загружаю <b>{filename}</b>...")
    try:
        file = await bot.get_file(doc.file_id)
        buf  = io.BytesIO()
        await bot.download_file(file.file_path, buf)
        people = parse_file(buf.getvalue(), filename)
    except Exception as e:
        await status.edit_text(f"❌ Ошибка чтения: {e}", reply_markup=back_menu_kb())
        return

    if not people:
        await status.edit_text(
            "⚠️ Не удалось извлечь имена из файла.\n"
            "Проверь структуру: нужны поля <code>name</code> или <code>first_name + last_name</code>.",
            reply_markup=back_menu_kb(),
        )
        return

    await status.edit_text(
        f"✅ <b>Файл принят!</b>\n"
        f"👥 Найдено: <b>{len(people)}</b> человек\n\n"
        "🚀 Начинаю поиск по каждому...\n"
        "<i>Результаты появляются по мере нахождения</i>"
    )

    cr_found = 0
    for i, person in enumerate(people, 1):
        name  = person["name"]
        phone = person.get("phone", "")

        prog = await message.answer(
            f"⏳ <b>[{i}/{len(people)}]</b>  {name}"
            + (f"  <code>{phone}</code>" if phone else "")
        )
        try:
            res = await asyncio.wait_for(full_search(name, phone), timeout=55.0)
        except asyncio.TimeoutError:
            await prog.edit_text(f"⚠️ [{i}/{len(people)}] <b>{name}</b> — таймаут, пропускаю.")
            await asyncio.sleep(2)
            continue

        if res.get("cr"):
            cr_found += 1
            save_cr({
                "name": name, "phone": phone,
                "timestamp": datetime.utcnow().isoformat(),
                "cr_count": len(res["cr"]),
                "cr_results": res["cr"],
            })

        await prog.delete()
        for chunk in split_msg(build_report(name, phone, res)):
            await message.answer(chunk, disable_web_page_preview=True)
        await message.answer(f"🔗 Ссылки для <b>{name}</b>:", reply_markup=result_links_kb(name))
        await asyncio.sleep(4)

    kb = InlineKeyboardBuilder()
    if cr_found: kb.button(text="📋 CryptoRank список", callback_data="show_found")
    kb.button(text="🔍 Новый поиск", callback_data="start_search")
    kb.button(text="🏠 Меню", callback_data="main_menu")
    kb.adjust(1)

    await message.answer(
        f"✅ <b>Готово!</b>\n"
        f"👥 Обработано: <b>{len(people)}</b>\n"
        f"🟣 Найдено на CryptoRank: <b>{cr_found}</b>",
        reply_markup=kb.as_markup(),
    )

    if cr_found and os.path.exists(CR_FILE):
        await message.answer_document(FSInputFile(CR_FILE), caption=f"📁 cryptorank_results.json · {cr_found} записей")

# ── Run ───────────────────────────────────────────────────────────────────────
async def main():
    print("✅ Бот запущен.")
    await dp.start_polling(bot, allowed_updates=["message", "callback_query"])

if __name__ == "__main__":
    asyncio.run(main())
