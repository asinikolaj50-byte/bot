import os
import re
import json
import asyncio
import urllib.parse
from datetime import datetime
import httpx
from aiogram import Bot, Dispatcher, types
from aiogram.filters import Command
from aiogram.client.default import DefaultBotProperties

TOKEN    = os.getenv("TELEGRAM_TOKEN")
_admin_raw = os.getenv("ADMIN_ID", "").strip()
ADMIN_ID = int(_admin_raw) if _admin_raw.isdigit() else None

bot = Bot(token=TOKEN, default_properties=DefaultBotProperties(parse_mode="Markdown"))
dp  = Dispatcher()

CRYPTORANK_FILE = "cryptorank_results.json"

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept-Language": "en-US,en;q=0.9,ru;q=0.8",
}


# ─── Хранилище найденных на CryptoRank ────────────────────────────────────────

def load_cr_results() -> list[dict]:
    if not os.path.exists(CRYPTORANK_FILE):
        return []
    try:
        with open(CRYPTORANK_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return []


def save_cr_result(entry: dict):
    data = load_cr_results()
    # Не дублируем одно и то же имя
    names = [r.get("query", "").lower() for r in data]
    if entry.get("query", "").lower() not in names:
        data.append(entry)
        with open(CRYPTORANK_FILE, "w", encoding="utf-8") as f:
            json.dump(data, f, ensure_ascii=False, indent=2)


# ─── DuckDuckGo HTML-поиск ────────────────────────────────────────────────────

async def ddg_search(query: str, max_results: int = 6) -> list[dict]:
    """
    Возвращает список словарей {title, url, snippet}.
    Используем html.duckduckgo.com — не требует API ключа.
    """
    encoded = urllib.parse.urlencode({"q": query})
    url = f"https://html.duckduckgo.com/html/?{encoded}"

    try:
        async with httpx.AsyncClient(
            timeout=15.0,
            follow_redirects=True,
            headers=HEADERS,
        ) as client:
            resp = await client.get(url)

        if resp.status_code != 200:
            return []

        html = resp.text

        # Заголовки результатов
        title_re   = re.compile(r'class="result__title"[^>]*>.*?<a[^>]+href="([^"]+)"[^>]*>(.*?)</a>', re.DOTALL)
        snippet_re = re.compile(r'class="result__snippet"[^>]*>(.*?)</(?:a|span)>', re.DOTALL)

        titles   = title_re.findall(html)
        snippets = [re.sub(r"<[^>]+>", "", s).strip() for s in snippet_re.findall(html)]

        results = []
        for i, (href, title_html) in enumerate(titles[:max_results]):
            title = re.sub(r"<[^>]+>", "", title_html).strip()
            # DuckDuckGo редиректит через uddg=
            if "uddg=" in href:
                href = urllib.parse.unquote(href.split("uddg=")[-1])
            if not href.startswith("http"):
                continue
            snippet = snippets[i] if i < len(snippets) else ""
            results.append({"title": title, "url": href, "snippet": snippet[:200]})

        return results

    except Exception:
        return []


# ─── CryptoRank: поиск через сайт и DDG ──────────────────────────────────────

async def search_cryptorank_site(full_name: str) -> list[dict]:
    """
    Ищем человека на cryptorank.io двумя способами:
    1. Прямой GET /people/search (если эндпоинт ответит)
    2. DDG site:cryptorank.io "Имя Фамилия"
    """
    results: list[dict] = []

    # Способ 1 — публичный JSON-поиск CryptoRank
    try:
        encoded_name = urllib.parse.quote(full_name)
        cr_url = f"https://cryptorank.io/api/v1/people/search?query={encoded_name}&limit=5"
        async with httpx.AsyncClient(timeout=10.0, headers=HEADERS, follow_redirects=True) as client:
            resp = await client.get(cr_url)
        if resp.status_code == 200:
            data = resp.json()
            # Структура может быть разной, ищем список
            items = data if isinstance(data, list) else data.get("data", [])
            for item in items[:5]:
                name = item.get("name") or item.get("fullName", "")
                if full_name.lower() in name.lower():
                    results.append({
                        "source": "cryptorank_api",
                        "name": name,
                        "url": f"https://cryptorank.io/people/{item.get('slug', '')}",
                        "role": item.get("role") or item.get("position", ""),
                        "company": item.get("company") or item.get("organization", ""),
                    })
    except Exception:
        pass

    # Способ 2 — DDG site:cryptorank.io
    ddg_results = await ddg_search(f'"{full_name}" site:cryptorank.io')
    for r in ddg_results:
        if "cryptorank.io" in r["url"]:
            results.append({
                "source": "cryptorank_ddg",
                "name": full_name,
                "url": r["url"],
                "snippet": r.get("snippet", ""),
            })

    return results


# ─── DuckDuckGo: поиск крипто/инвест упоминаний ──────────────────────────────

CRYPTO_KEYWORDS = (
    "crypto OR cryptocurrency OR bitcoin OR BTC OR blockchain OR web3 "
    "OR investor OR venture OR fund OR DeFi OR NFT OR altcoin OR VC "
    "OR \"venture capital\" OR \"angel investor\""
)

async def search_crypto_mentions(full_name: str, phone: str = "") -> list[dict]:
    """
    Строгий поиск по точному имени + крипто/инвест тематике.
    Дополнительно пробуем с телефоном, если передан.
    """
    queries = [
        f'"{full_name}" ({CRYPTO_KEYWORDS})',
        f'"{full_name}" инвестиции OR криптовалюта OR блокчейн',
    ]
    if phone:
        queries.append(f'"{full_name}" "{phone}"')

    all_results: list[dict] = []
    seen_urls: set[str] = set()

    tasks = [ddg_search(q, max_results=5) for q in queries]
    batches = await asyncio.gather(*tasks, return_exceptions=True)

    for batch in batches:
        if not isinstance(batch, list):
            continue
        for item in batch:
            if item["url"] not in seen_urls:
                seen_urls.add(item["url"])
                all_results.append(item)

    return all_results


# ─── Форматирование отчёта ────────────────────────────────────────────────────

def fmt_cr_result(r: dict) -> str:
    lines = [f"  🔗 {r['url']}"]
    if r.get("role"):
        lines.append(f"  👤 Роль: {r['role']}")
    if r.get("company"):
        lines.append(f"  🏢 Компания: {r['company']}")
    if r.get("snippet"):
        lines.append(f"  _{r['snippet']}_")
    return "\n".join(lines)


def fmt_ddg_result(r: dict, idx: int) -> str:
    lines = [f"*{idx}.* {r['title']}"]
    lines.append(f"  🔗 {r['url']}")
    if r.get("snippet"):
        lines.append(f"  _{r['snippet']}_")
    return "\n".join(lines)


def send_chunks(text: str) -> list[str]:
    """Делит длинный текст на куски ≤ 4096 символов по разделителю ─."""
    if len(text) <= 4096:
        return [text]
    parts = text.split("\n─")
    chunks, current = [], ""
    for part in parts:
        block = ("\n─" if current else "") + part
        if len(current) + len(block) > 4096:
            chunks.append(current)
            current = part
        else:
            current += block
    if current:
        chunks.append(current)
    return chunks


# ─── Команды ──────────────────────────────────────────────────────────────────

@dp.message(Command("start"))
async def cmd_start(message: types.Message):
    await message.answer(
        "🔍 *Бот поиска крипто/инвест упоминаний*\n\n"
        "*Как использовать:*\n"
        "`/search Иван Петров` — поиск по имени\n"
        "`/search Иван Петров +79001234567` — поиск с телефоном (доп. инфа)\n\n"
        "*Другие команды:*\n"
        "`/found` — список всех найденных на CryptoRank\n"
        "`/help` — справка\n\n"
        "_Сначала проверяю CryptoRank, затем DDG по крипто/инвест тематике._"
    )


@dp.message(Command("help"))
async def cmd_help(message: types.Message):
    await message.answer(
        "📖 *Справка*\n\n"
        "*Логика поиска:*\n"
        "1️⃣ Ищу человека на `cryptorank.io`\n"
        "   • Если найден — записываю в `cryptorank_results.json`\n"
        "2️⃣ Если на CryptoRank нет — ищу в DuckDuckGo:\n"
        "   • Строго `\"Имя Фамилия\"` + крипто/инвест ключевые слова\n"
        "   • bitcoin, crypto, web3, investor, venture, fund, DeFi…\n"
        "   • Дополнительно с номером телефона, если передан\n\n"
        "*Форматы ввода:*\n"
        "`/search Имя Фамилия`\n"
        "`/search Имя Фамилия +7xxxxxxxxxx`\n\n"
        "*Файл с результатами CryptoRank:*\n"
        "`cryptorank_results.json` — хранится рядом с ботом\n"
        "Посмотреть через `/found`"
    )


@dp.message(Command("found"))
async def cmd_found(message: types.Message):
    data = load_cr_results()
    if not data:
        await message.answer("📂 Файл `cryptorank_results.json` пуст — никто ещё не найден на CryptoRank.")
        return

    lines = [f"📋 *Найдены на CryptoRank ({len(data)} чел.):*\n"]
    for i, entry in enumerate(data, 1):
        q     = entry.get("query", "—")
        ts    = entry.get("timestamp", "")
        count = entry.get("cr_count", 0)
        lines.append(f"{i}. *{q}* — {count} результат(а) [{ts[:10]}]")
        for r in entry.get("cr_results", [])[:2]:
            lines.append(f"   🔗 {r.get('url', '')}")

    await message.answer("\n".join(lines), disable_web_page_preview=True)


@dp.message(Command("search"))
async def cmd_search(message: types.Message):
    raw = message.text.split(maxsplit=1)
    if len(raw) < 2 or not raw[1].strip():
        await message.answer(
            "⚠️ Укажи имя и фамилию.\n"
            "Пример: `/search Иван Петров` или `/search Иван Петров +79001234567`"
        )
        return

    args    = raw[1].strip().split()
    phone   = ""
    name_parts = []

    for part in args:
        # Телефон: начинается с +, или только цифры длиной 7–15
        if re.match(r"^\+?\d{7,15}$", part):
            phone = part
        else:
            name_parts.append(part)

    if len(name_parts) < 2:
        await message.answer(
            "⚠️ Нужно минимум имя и фамилия.\n"
            "Пример: `/search Иван Петров`"
        )
        return

    full_name = " ".join(name_parts)
    status_text = (
        f"🔎 Ищу: *{full_name}*"
        + (f" | тел: `{phone}`" if phone else "")
        + "\n\n⏳ Шаг 1/2 — проверяю CryptoRank..."
    )
    status = await message.answer(status_text)

    # ── ШАГ 1: CryptoRank ────────────────────────────────────────────────────
    cr_results = await search_cryptorank_site(full_name)

    if cr_results:
        # Нашли на CryptoRank
        entry = {
            "query":      full_name,
            "phone":      phone,
            "timestamp":  datetime.utcnow().isoformat(),
            "cr_count":   len(cr_results),
            "cr_results": cr_results,
        }
        save_cr_result(entry)

        cr_lines = [f"✅ *{full_name}* найден на CryptoRank!\n"]
        cr_lines.append(f"💾 _Сохранено в `{CRYPTORANK_FILE}`_\n")
        for r in cr_results[:5]:
            cr_lines.append(fmt_cr_result(r))
        cr_lines.append(
            f"\n─\n"
            f"🔗 [Поиск на CryptoRank](https://cryptorank.io/people?search={urllib.parse.quote(full_name)})"
        )

        await status.edit_text("\n".join(cr_lines), disable_web_page_preview=False)
        return

    # ── ШАГ 2: DDG крипто/инвест ─────────────────────────────────────────────
    await status.edit_text(
        status_text.replace("Шаг 1/2 — проверяю CryptoRank...", "Шаг 1/2 — CryptoRank: не найден") +
        "\n⏳ Шаг 2/2 — ищу упоминания в интернете..."
    )

    ddg_results = await search_crypto_mentions(full_name, phone)

    # Формируем отчёт
    report = [
        f"📊 *Отчёт по запросу:* `{full_name}`"
        + (f" | `{phone}`" if phone else "") + "\n",
        "❌ *CryptoRank:* не найден\n",
    ]

    if ddg_results:
        report.append(f"🌐 *Упоминания в интернете ({len(ddg_results)}):*\n")
        for i, r in enumerate(ddg_results[:10], 1):
            report.append(fmt_ddg_result(r, i) + "\n")
    else:
        report.append(
            "🔍 *Публичных крипто/инвест упоминаний не найдено.*\n\n"
            "_Попробуй уточнить написание имени или добавить город._"
        )

    # Ручные ссылки для проверки
    q_enc = urllib.parse.quote(full_name)
    report.append(
        "─\n"
        "🔗 *Проверить вручную:*\n"
        f"• [CryptoRank](https://cryptorank.io/people?search={q_enc})\n"
        f"• [Google Crypto](https://www.google.com/search?q=%22{q_enc}%22+crypto+OR+bitcoin)\n"
        f"• [LinkedIn](https://www.linkedin.com/search/results/people/?keywords={q_enc})\n"
        f"• [Twitter/X](https://twitter.com/search?q=%22{full_name.replace(' ', '%20')}%22+crypto)"
    )

    full_text = "\n".join(report)
    await status.delete()

    for chunk in send_chunks(full_text):
        await message.answer(chunk, disable_web_page_preview=True)


# ─── Запуск ───────────────────────────────────────────────────────────────────

async def main():
    print("✅ Бот запущен. Жду команды...")
    await dp.start_polling(bot)


if __name__ == "__main__":
    asyncio.run(main())
