"""Refresh catalog.json and stock.json from Pokémon Center Online.

- Reads every page of the in-stock plush listing.
- Adds products that are not in catalog.json yet (new releases) and updates
  the price/image of existing ones.
- Writes stock.json with the in-stock JAN codes.

The store sometimes puts a Queue-it waiting room in front of the site. It
answers with redirects that set a QueueITAccepted-* cookie, so a cookie jar is
required. If the waiting room or a layout change keeps us from reading the real
listing, the script exits with an error and writes nothing, so the live site
keeps its last good data instead of showing an empty catalog.
"""

import html
import http.cookiejar
import json
import math
import re
import sys
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
CATALOG = ROOT / "catalog.json"
STOCK = ROOT / "stock.json"

BASE = "https://www.pokemoncenter-online.com/plush-toys/plush/?inStock=1&page={}"
SITE = "https://www.pokemoncenter-online.com"
PER_PAGE = 40

TILE_RE = re.compile(
    r'<li class="product" data-pid="(\d{13})">(.*?)</li>', re.S)
IMG_RE = re.compile(r'<img src="([^"]+)" alt="([^"]*)"')
PRICE_RE = re.compile(r'class="price[^"]*">.*?([\d,]+)<small>', re.S)
TOTAL_RE = re.compile(r'class="num">/\s*(\d+)</span>')


def sell_price(official):
    # Same rule the catalog was built with: +1% and a ¥100 fee, rounded up to ¥100.
    return int(math.ceil((official * 1.01 + 100) / 100) * 100)


def new_opener():
    opener = urllib.request.build_opener(
        urllib.request.HTTPCookieProcessor(http.cookiejar.CookieJar()))
    opener.addheaders = [
        ("User-Agent",
         "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
         "(KHTML, like Gecko) Chrome/140.0.0.0 Safari/537.36"),
        ("Accept", "text/html,application/xhtml+xml"),
        ("Accept-Language", "ja,en;q=0.8"),
    ]
    return opener


def fetch_page(opener, page):
    """Return the listing HTML, or None if we landed on the waiting room."""
    with opener.open(BASE.format(page), timeout=45) as resp:
        body = resp.read().decode("utf-8", errors="replace")
        if "wr.pokemoncenter-online.com" in resp.geturl() or not TOTAL_RE.search(body):
            return None
        return body


def fetch_with_retry(state, page, attempts=6):
    last = None
    for attempt in range(attempts):
        try:
            body = fetch_page(state["opener"], page)
            if body is not None:
                return body
            last = "waiting room / unexpected page"
        except Exception as exc:  # network errors, redirect loops
            last = exc
        # Fresh cookies + growing back-off before trying again.
        state["opener"] = new_opener()
        time.sleep(min(120, 10 * (attempt + 1)))
    raise RuntimeError(f"page {page} failed after {attempts} attempts: {last}")


def parse_tiles(body):
    items = {}
    for jan, tile in TILE_RE.findall(body):
        img = IMG_RE.search(tile)
        price = PRICE_RE.search(tile)
        if not img or not price:
            continue
        items[jan] = {
            "n": html.unescape(img.group(2)).strip(),
            "official": int(price.group(1).replace(",", "")),
            "img": img.group(1),
        }
    return items


def main():
    state = {"opener": new_opener()}
    first = fetch_with_retry(state, 1)
    total = int(TOTAL_RE.search(first).group(1))
    pages = max(1, -(-total // PER_PAGE))

    found = parse_tiles(first)
    for p in range(2, pages + 1):
        time.sleep(1.5)
        found.update(parse_tiles(fetch_with_retry(state, p)))

    # Never publish a partial or empty result.
    if total < 50 or len(found) < total * 0.9:
        sys.exit(f"Refusing to write: site reports {total}, parsed {len(found)}.")

    catalog = json.loads(CATALOG.read_text(encoding="utf-8"))
    by_jan = {c.get("jan"): c for c in catalog}
    added = updated = 0
    for jan, it in found.items():
        price = sell_price(it["official"])
        entry = by_jan.get(jan)
        if entry is None:
            catalog.insert(0, {"n": it["n"], "p": price, "img": it["img"],
                               "u": f"{SITE}/{jan}.html", "jan": jan,
                               "added": datetime.now(timezone.utc).strftime("%Y-%m-%d")})
            added += 1
        elif entry["p"] != price or entry.get("img") != it["img"]:
            entry["p"] = price
            entry["img"] = it["img"]
            updated += 1

    CATALOG.write_text(json.dumps(catalog, ensure_ascii=False, separators=(",", ":")),
                       encoding="utf-8")
    STOCK.write_text(json.dumps({
        "inStock": sorted(found),
        "count": len(found),
        "lastUpdated": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
    }, ensure_ascii=False, separators=(",", ":")), encoding="utf-8")

    print(f"Site total {total}, parsed {len(found)} in stock, "
          f"{added} new products added, {updated} prices/images updated, "
          f"catalog now {len(catalog)} items.")


if __name__ == "__main__":
    main()
