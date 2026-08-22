"""
Неофициальный клиент поиска для Rakuma (楽天ラクマ / fril.jp).

В отличие от Mercari, у Rakuma нет поддерживаемой Python-библиотеки вроде
mercapi. Страница поиска fril.jp отдаётся обычным серверным HTML (не SPA),
поэтому здесь используется лёгкий regex-парсер вместо полноценного HTML-парсера —
без лишних зависимостей.

⚠️ Это самодельное решение поверх неофициальной, не документированной вёрстки
сайта. Если Rakuma поменяет разметку страницы поиска — парсинг может сломаться
(вернёт пустой список вместо ошибки, чтобы не ронять бота). Если находки по
Rakuma вдруг пропали, а раньше работали — это первое, что стоит проверить.
"""

import html as html_lib
import re

import httpx

SEARCH_URL = "https://fril.jp/s"
ITEM_ID_RE = re.compile(r"https://item\.fril\.jp/([0-9a-f]{16,40})")
PRICE_RE = re.compile(r"¥([\d,]+)")
TITLE_RE = re.compile(r'title="([^"]{2,150})"')

HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) "
        "AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1"
    )
}


async def search_rakuma(keyword: str, max_results: int = 40) -> list[dict]:
    """
    Ищет товары на Rakuma по ключевому слову, отсортированные по дате (новые первые).
    Возвращает список словарей: {"id", "name", "price", "url"}.
    Проданные товары (SOLD OUT) пропускаются.
    """
    params = {"query": keyword, "sort": "created_at", "order": "desc"}
    async with httpx.AsyncClient(timeout=15, headers=HEADERS, follow_redirects=True) as client:
        resp = await client.get(SEARCH_URL, params=params)
        resp.raise_for_status()
        page = resp.text

    items: list[dict] = []
    seen_ids: set[str] = set()
    matches = list(ITEM_ID_RE.finditer(page))

    i = 0
    n = len(matches)
    while i < n:
        item_id = matches[i].group(1)
        if item_id in seen_ids:
            i += 1
            continue
        seen_ids.add(item_id)

        start = matches[i].start()
        # На странице каждый товар обычно ссылается на себя дважды подряд
        # (картинка + текст), поэтому границу "чанка" ищем до СЛЕДУЮЩЕГО
        # ДРУГОГО товара, а не до следующего совпадения вообще — иначе
        # чанк обрывается до того, как в нём появится цена.
        j = i + 1
        while j < n and matches[j].group(1) == item_id:
            j += 1
        end = matches[j].start() if j < n else min(len(page), start + 4000)
        chunk = page[start:end]
        i = j

        if "SOLD OUT" in chunk:
            continue  # пропускаем проданное

        price_match = PRICE_RE.search(chunk)
        if not price_match:
            continue
        price = int(price_match.group(1).replace(",", ""))

        title_match = TITLE_RE.search(chunk)
        if title_match:
            raw_title = html_lib.unescape(title_match.group(1))
            name = raw_title.split("(")[0].strip() or keyword
        else:
            name = keyword

        items.append(
            {
                "id": item_id,
                "name": name,
                "price": price,
                "url": f"https://item.fril.jp/{item_id}",
            }
        )
        if len(items) >= max_results:
            break

    return items
