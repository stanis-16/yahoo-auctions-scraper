#!/usr/bin/env python3
"""
Yahoo Auctions Japan -- listing monitor (framework)

What it does right now:
  - Runs each of your saved searches (sorted newest-first automatically)
  - Parses the listings off the results page
  - Applies your client-side filters (price, include/exclude keywords)
  - Remembers what it has already shown you (state file), so you only ever
    hear about genuinely new listings
  - "Notifies" matches -- for now that just means printing them and logging
    them to a file. The notify() function is a stub you'll later swap for
    Telegram / Discord / email.

Run modes:
  - "once"  -> one pass, then exit. Use this with GitHub Actions / cron.
  - "loop"  -> keep running forever, polling on an interval. Use on a VPS / Pi.

Everything you'd normally edit lives in the CONFIG block below.
"""

import json
import re
import time
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import requests
from bs4 import BeautifulSoup


# ============================================================================
# CONFIG  --  this is the part you edit
# ============================================================================

# Each entry is one saved search. Add as many as you want.
#
# Easiest way to fill in "url": set the search up in your browser with all the
# filters you want (keyword, chassis code, category, price range, condition...),
# then copy the address-bar URL and paste it here. You do NOT need to worry
# about the sort order in that URL -- the script forces newest-first for you.
#
# The optional fields below "url" are extra filters applied AFTER fetching,
# so you can refine beyond what the Yahoo search page lets you do.
SEARCHES = [
    {
        "label": "Example -- S13 front bumper",
        "url": "https://auctions.yahoo.co.jp/search/search?p=S13%20%E3%83%95%E3%83%AD%E3%83%B3%E3%83%88%E3%83%90%E3%83%B3%E3%83%91%E3%83%BC",

        # ---- optional client-side filters (leave as-is to disable a rule) ----
        "min_price": None,                       # yen; None = no minimum
        "max_price": 30000,                      # yen; None = no maximum
        "exclude": ["ジャンク", "破損", "現状渡し"],  # skip if title contains ANY of these
        "include_any": [],                       # keep only if title has at least one (empty = off)
        "include_all": [],                       # keep only if title has all of these (empty = off)
    },

    # ---- add more searches below, same shape ----
    # {
    #     "label": "...",
    #     "url": "...",
    #     "min_price": None,
    #     "max_price": None,
    #     "exclude": [],
    #     "include_any": [],
    #     "include_all": [],
    # },
]

SETTINGS = {
    "run_mode": "once",             # "once" (cron / GitHub Actions) or "loop" (VPS / Pi)
    "poll_interval_seconds": 900,   # loop mode only: gap between passes (900 = 15 min)
    "request_delay_seconds": 3,     # politeness pause between searches in one pass
    "results_per_page": 50,         # 20, 50, or 100
    "notify_on_first_run": False,   # False = first run just builds a baseline quietly
    "state_file": "seen.json",      # remembers listing IDs already shown to you
    "match_log": "matches.jsonl",   # every new match appended here (one JSON per line)
    "debug_save_html": False,       # True = dump first page's HTML to debug_page.html
    "user_agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                   "AppleWebKit/537.36 (KHTML, like Gecko) "
                   "Chrome/124.0.0.0 Safari/537.36"),
}

# CSS selectors for reading the results page. Only touch these if Yahoo changes
# their markup and the script starts parsing 0 listings (see the README notes).
SELECTORS = {
    "item": "li.Product",
    "title_link": "a.Product__titleLink",
    "price": ".Product__priceValue",
    "bids": ".Product__bid",
    "time_left": ".Product__time",
    "image": "img.Product__imageData",
}

# ============================================================================
# ENGINE  --  you shouldn't need to edit below here for normal use
# ============================================================================

AUCTION_ID_RE = re.compile(r"/auction/([a-zA-Z]?\d+)")


def normalize_url(url, results_per_page):
    """Force newest-first sorting + page size onto a pasted search URL."""
    parts = urlparse(url)
    q = parse_qs(parts.query)
    q["s1"] = ["new"]                       # sort key: new = newest listed
    q["o1"] = ["d"]                         # order: descending
    q["n"] = [str(results_per_page)]        # results per page
    return urlunparse(parts._replace(query=urlencode(q, doseq=True)))


def fetch(url, settings):
    headers = {
        "User-Agent": settings["user_agent"],
        "Accept-Language": "ja,en;q=0.8",
    }
    resp = requests.get(url, headers=headers, timeout=20)
    resp.raise_for_status()
    return resp.text


def _text(node):
    return node.get_text(strip=True) if node else None


def _price(text):
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None


def parse_listings(html, debug_save=False):
    if debug_save:
        Path("debug_page.html").write_text(html, encoding="utf-8")

    soup = BeautifulSoup(html, "html.parser")
    listings = []

    for item in soup.select(SELECTORS["item"]):
        link = item.select_one(SELECTORS["title_link"])
        if not link or not link.get("href"):
            continue

        href = link["href"]
        m = AUCTION_ID_RE.search(href)
        auction_id = m.group(1) if m else link.get("data-auction-id")
        if not auction_id:
            continue

        img = item.select_one(SELECTORS["image"])
        listings.append({
            "id": auction_id,
            "title": _text(link),
            "price": _price(_text(item.select_one(SELECTORS["price"]))),
            "bids": _text(item.select_one(SELECTORS["bids"])),
            "time_left": _text(item.select_one(SELECTORS["time_left"])),
            "url": href,
            "image": (img.get("src") or img.get("data-src")) if img else None,
        })

    return listings


def passes_filters(listing, search):
    title = listing.get("title") or ""
    price = listing.get("price")

    if search.get("max_price") is not None and price is not None and price > search["max_price"]:
        return False
    if search.get("min_price") is not None and price is not None and price < search["min_price"]:
        return False
    if any(w in title for w in search.get("exclude", [])):
        return False

    inc_any = search.get("include_any", [])
    if inc_any and not any(w in title for w in inc_any):
        return False

    inc_all = search.get("include_all", [])
    if inc_all and not all(w in title for w in inc_all):
        return False

    return True


def load_seen(path):
    p = Path(path)
    if p.exists():
        try:
            return set(json.loads(p.read_text()))
        except Exception:
            return set()
    return set()


def save_seen(path, seen):
    Path(path).write_text(json.dumps(sorted(seen)), encoding="utf-8")


def notify(listing, search):
    """
    Placeholder notifier. For now it prints to the console and appends the match
    to a log file. Later you'll replace the body of this function with a Telegram
    or Discord push -- nothing else in the script needs to change.
    """
    price = f"\u00a5{listing['price']:,}" if listing.get("price") else "\u2014"
    print(f"  NEW  [{search['label']}]  {price}  {listing.get('title')}")
    print(f"       {listing.get('url')}")
    with open(SETTINGS["match_log"], "a", encoding="utf-8") as f:
        f.write(json.dumps({"search": search["label"], **listing}, ensure_ascii=False) + "\n")


def run_once():
    seen = load_seen(SETTINGS["state_file"])
    first_run = len(seen) == 0
    if first_run:
        print("First run: building a baseline of current listings "
              "(no alerts unless notify_on_first_run is True).")

    new_count = 0
    for i, search in enumerate(SEARCHES):
        url = normalize_url(search["url"], SETTINGS["results_per_page"])
        label = search.get("label", url)

        try:
            html = fetch(url, SETTINGS)
        except Exception as e:
            print(f"[!] Fetch failed for '{label}': {e}")
            continue

        listings = parse_listings(html, SETTINGS["debug_save_html"] and i == 0)
        print(f"[{label}] parsed {len(listings)} listings")

        for listing in listings:
            if listing["id"] in seen:
                continue
            seen.add(listing["id"])   # mark seen even if filtered out, so we don't recheck it
            if passes_filters(listing, search):
                if first_run and not SETTINGS["notify_on_first_run"]:
                    continue
                notify(listing, search)
                new_count += 1

        if i < len(SEARCHES) - 1:
            time.sleep(SETTINGS["request_delay_seconds"])

    save_seen(SETTINGS["state_file"], seen)
    print(f"Done. {new_count} new match(es).")


def main():
    if SETTINGS["run_mode"] == "loop":
        while True:
            run_once()
            print(f"Sleeping {SETTINGS['poll_interval_seconds']}s ...")
            time.sleep(SETTINGS["poll_interval_seconds"])
    else:
        run_once()


if __name__ == "__main__":
    main()
