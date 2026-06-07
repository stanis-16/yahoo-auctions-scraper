#!/usr/bin/env python3
"""
Yahoo Auctions Japan — Discord monitor bot

Channels:
  - Category channels    : new matching listings (set per-search)
  - Buy-It-Now channel   : new matching listings that have a BIN price
  - Watchlist channel    : items you're tracking, sorted by time left, auto-refreshed
  - Add-criteria channel : paste a search URL to add a search; /manage to edit them

Environment variables (Railway):
  DISCORD_BOT_TOKEN, GITHUB_TOKEN, GITHUB_REPO, OWNER_IDS
  WATCHLIST_CHANNEL_ID, ADD_CRITERIA_CHANNEL_ID
  BIN_CHANNEL_ID            (optional: buy-it-now alerts)

Discord Developer Portal: Bot → Privileged Gateway Intents → Message Content Intent → ON
"""

import asyncio
import base64
import json
import os
import re
import time
from datetime import datetime, timezone, timedelta
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from bs4 import BeautifulSoup


# ── Environment variables ──────────────────────────────────────────────────────
BOT_TOKEN               = os.environ["DISCORD_BOT_TOKEN"]
GITHUB_TOKEN            = os.environ["GITHUB_TOKEN"]
GITHUB_REPO             = os.environ["GITHUB_REPO"]
WATCHLIST_CHANNEL_ID    = int(os.environ.get("WATCHLIST_CHANNEL_ID",    0))
ADD_CRITERIA_CHANNEL_ID = int(os.environ.get("ADD_CRITERIA_CHANNEL_ID", 0))
BIN_CHANNEL_ID          = int(os.environ.get("BIN_CHANNEL_ID",          0))
ENDING_SOON_CHANNEL_ID  = int(os.environ.get("ENDING_SOON_CHANNEL_ID",  0))

OWNER_IDS: set[int] = {
    int(x.strip()) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()
}

CRITERIA_FILE  = "criteria.json"
WATCHLIST_FILE = "watchlist.json"
STANDARD_MINUTES = 30
BIN_MINUTES      = 10
UPDATE_MINUTES   = 15

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")


# ── In-memory state ────────────────────────────────────────────────────────────
searches:       list = []
seen_standard:  set  = set()
seen_bin:       set  = set()
watchlist:      list = []
listing_cache:  dict = {}
first_standard: bool = True
first_bin:      bool = True
_posted_order:  list = []


# ── CSS selectors ──────────────────────────────────────────────────────────────
ITEM_SEL  = "li.Product"
LINK_SEL  = "a.Product__titleLink"
PRICE_SEL = ".Product__priceValue"
BIDS_SEL  = ".Product__bid"
TIME_SEL  = ".Product__time"
IMG_SEL   = "img.Product__imageData"
ID_RE     = re.compile(r"/auction/([a-zA-Z]?\d+)")

# Individual item page (watchlist refresh) — adjust if Yahoo changes markup
ITEM_PRICE_SEL = ".Price__value"
ITEM_TIME_SEL  = ".Count__item"

YAHOO_SEARCH_RE = re.compile(r"https://auctions\.yahoo\.co\.jp/search/search\S+")
YAHOO_ITEM_RE   = re.compile(r"https?://(?:page\.)?auctions\.yahoo\.co\.jp/\S*?/auction/([a-zA-Z]?\d+)")


# ── Discord setup ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True


class OwnerOnlyTree(app_commands.CommandTree):
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if OWNER_IDS and interaction.user.id not in OWNER_IDS:
            await interaction.response.send_message("❌ This bot is private.", ephemeral=True)
            return False
        return True


client = discord.Client(intents=intents)
tree   = OwnerOnlyTree(client)


# ══════════════════════════════════════════════════════════════════════════════
# GitHub helpers
# ══════════════════════════════════════════════════════════════════════════════

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

async def gh_read(path: str):
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=GH_HEADERS) as r:
            if r.status == 404:
                return None, None
            data = await r.json()
    return json.loads(base64.b64decode(data["content"]).decode()), data["sha"]

async def gh_write(path: str, content, sha: Optional[str], message: str) -> bool:
    url  = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    body = {
        "message": message,
        "content": base64.b64encode(
            json.dumps(content, ensure_ascii=False, indent=2).encode()
        ).decode(),
    }
    if sha:
        body["sha"] = sha
    async with aiohttp.ClientSession() as s:
        async with s.put(url, headers=GH_HEADERS, json=body) as r:
            return r.status in (200, 201)

async def load_criteria():
    global searches
    data, _ = await gh_read(CRITERIA_FILE)
    searches = data if isinstance(data, list) else []

async def save_criteria() -> bool:
    _, sha = await gh_read(CRITERIA_FILE)
    return await gh_write(CRITERIA_FILE, searches, sha, "chore: update criteria")

async def load_watchlist():
    global watchlist
    data, _ = await gh_read(WATCHLIST_FILE)
    watchlist = data if isinstance(data, list) else []

async def save_watchlist() -> bool:
    _, sha = await gh_read(WATCHLIST_FILE)
    return await gh_write(WATCHLIST_FILE, watchlist, sha, "chore: update watchlist")


# ══════════════════════════════════════════════════════════════════════════════
# Time helpers
# ══════════════════════════════════════════════════════════════════════════════

def parse_jp_time(text: Optional[str]):
    """'1日2時間' → ('1d 2h', total_seconds). Returns (text, None) if unparseable."""
    if not text:
        return None, None
    days = hours = mins = secs = 0
    found = False
    if (m := re.search(r"(\d+)\s*日",   text)): days  = int(m.group(1)); found = True
    if (m := re.search(r"(\d+)\s*時間", text)): hours = int(m.group(1)); found = True
    if (m := re.search(r"(\d+)\s*分",   text)): mins  = int(m.group(1)); found = True
    if (m := re.search(r"(\d+)\s*秒",   text)): secs  = int(m.group(1)); found = True
    if not found:
        return text, None
    total = days*86400 + hours*3600 + mins*60 + secs
    parts = []
    if days:  parts.append(f"{days}d")
    if hours: parts.append(f"{hours}h")
    if mins:  parts.append(f"{mins}m")
    if secs and not (days or hours): parts.append(f"{secs}s")
    return (" ".join(parts) or "<1m"), total

def fmt_remaining(end_iso: Optional[str]) -> str:
    """ISO end timestamp → human English remaining time."""
    if not end_iso:
        return "—"
    try:
        end = datetime.fromisoformat(end_iso)
    except Exception:
        return "—"
    rem = (end - datetime.now(timezone.utc)).total_seconds()
    if rem <= 0:
        return "Ended"
    d = int(rem // 86400); h = int((rem % 86400)//3600); m = int((rem % 3600)//60)
    parts = []
    if d: parts.append(f"{d}d")
    if h: parts.append(f"{h}h")
    if m and not d: parts.append(f"{m}m")
    return " ".join(parts) or "<1m"

def parse_english_time(text: Optional[str]) -> Optional[int]:
    """'2d 3h' -> total seconds. Reverse of the display format."""
    if not text:
        return None
    secs = 0; found = False
    if (m := re.search(r"(\d+)\s*d", text)): secs += int(m.group(1)) * 86400; found = True
    if (m := re.search(r"(\d+)\s*h", text)): secs += int(m.group(1)) * 3600;  found = True
    if (m := re.search(r"(\d+)\s*m", text)): secs += int(m.group(1)) * 60;    found = True
    return secs if found else None

def end_ts_from_secs(secs: Optional[int]) -> Optional[str]:
    if not secs:
        return None
    return (datetime.now(timezone.utc) + timedelta(seconds=secs)).isoformat()

def sort_key(entry: dict):
    try:
        return datetime.fromisoformat(entry.get("end_timestamp"))
    except Exception:
        return datetime.max.replace(tzinfo=timezone.utc)


# ══════════════════════════════════════════════════════════════════════════════
# Yahoo helpers
# ══════════════════════════════════════════════════════════════════════════════

JUNK_PARAMS = ("tab_ex", "ei", "aq", "oq", "sc_i", "fr", "x", "y")
FORCED_PARAMS = {"s1": "new", "o1": "d", "n": "100", "b": "1"}
DEFAULT_PARAMS = {"is_postage_mode": "1", "dest_pref_code": "16"}  # shipping est. to Toyama

def normalize_url(url: str, extra: dict = None) -> str:
    p = urlparse(url)
    q = parse_qs(p.query)
    for junk in JUNK_PARAMS:
        q.pop(junk, None)
    for k, v in DEFAULT_PARAMS.items():
        q.setdefault(k, [v])
    for k, v in FORCED_PARAMS.items():
        q[k] = [v]
    if extra:
        for k, v in extra.items():
            q[k] = [v]
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))

def bin_url(url: str, bin_max: int) -> str:
    return normalize_url(url, extra={"price_type": "bidorbuyprice", "min": "", "max": str(bin_max)})

def url_to_label(url: str) -> str:
    q = parse_qs(urlparse(url).query)
    for key in ("p", "va"):
        if q.get(key):
            return q[key][0][:50]
    return f"Search {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M')}"

async def fetch_html(url: str) -> str:
    hdrs = {"User-Agent": USER_AGENT, "Accept-Language": "ja,en;q=0.8"}
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=hdrs,
                         timeout=aiohttp.ClientTimeout(total=20)) as r:
            r.raise_for_status()
            return await r.text()

def _text(node) -> Optional[str]:
    return node.get_text(strip=True) if node else None

def _price(text: Optional[str]) -> Optional[int]:
    if not text:
        return None
    digits = re.sub(r"[^\d]", "", text)
    return int(digits) if digits else None

def parse_listings(html: str) -> list:
    soup = BeautifulSoup(html, "html.parser")
    out  = []
    for item in soup.select(ITEM_SEL):
        link = item.select_one(LINK_SEL)
        if not link or not link.get("href"):
            continue
        href = link["href"]
        m    = ID_RE.search(href)
        aid  = m.group(1) if m else link.get("data-auction-id")
        if not aid:
            continue
        prices       = item.select(PRICE_SEL)
        img          = item.select_one(IMG_SEL)
        eng, secs    = parse_jp_time(_text(item.select_one(TIME_SEL)))
        out.append({
            "id":           aid,
            "title":        _text(link),
            "price":        _price(_text(prices[0])) if prices else None,
            "buynow_price": _price(_text(prices[1])) if len(prices) > 1 else None,
            "bids":         _text(item.select_one(BIDS_SEL)),
            "time_left":    eng,
            "time_secs":    secs,
            "url":          href,
            "image":        (img.get("src") or img.get("data-src")) if img else None,
        })
    return out

async def fetch_item_details(auction_id: str) -> dict:
    """Pull title/image (via OG tags) + price/time from an individual auction page."""
    try:
        html = await fetch_html(f"https://page.auctions.yahoo.co.jp/jp/auction/{auction_id}")
    except Exception:
        return {}
    soup = BeautifulSoup(html, "html.parser")

    def og(prop):
        tag = soup.find("meta", attrs={"property": prop})
        return tag.get("content") if tag and tag.get("content") else None

    eng, secs = parse_jp_time(_text(soup.select_one(ITEM_TIME_SEL)))
    return {
        "id":         auction_id,
        "title":      og("og:title"),
        "image":      og("og:image"),
        "url":        f"https://page.auctions.yahoo.co.jp/jp/auction/{auction_id}",
        "price":      _price(_text(soup.select_one(ITEM_PRICE_SEL))),
        "time_left":  eng,
        "time_secs":  secs,
    }

def passes_filters(listing: dict, search: dict) -> bool:
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


def passes_keyword_filters(listing: dict, search: dict) -> bool:
    title = listing.get("title") or ""
    if any(w in title for w in search.get("exclude", [])):
        return False
    inc_any = search.get("include_any", [])
    if inc_any and not any(w in title for w in inc_any):
        return False
    inc_all = search.get("include_all", [])
    if inc_all and not all(w in title for w in inc_all):
        return False
    return True


# ══════════════════════════════════════════════════════════════════════════════
# Discord embeds
# ══════════════════════════════════════════════════════════════════════════════

def make_listing_embed(listing: dict, category: str) -> discord.Embed:
    embed = discord.Embed(title=listing.get("title") or "No title",
                          url=listing.get("url"), color=0x2ecc71)
    if category:
        embed.set_author(name=category)          # category name shown first
    price  = listing.get("price")
    buynow = listing.get("buynow_price")
    if price  is not None: embed.add_field(name="💴 Current Price", value=f"¥{price:,}",  inline=True)
    if buynow is not None: embed.add_field(name="🏷️ Buy It Now",    value=f"¥{buynow:,}", inline=True)
    if listing.get("bids"):      embed.add_field(name="📊 Bids",      value=listing["bids"],      inline=True)
    if listing.get("time_left"): embed.add_field(name="⏱️ Time Left", value=listing["time_left"], inline=True)
    if listing.get("image"):     embed.set_image(url=listing["image"])   # large image
    return embed

def make_watchlist_embed(entry: dict) -> discord.Embed:
    embed = discord.Embed(title=entry.get("title") or "No title",
                          url=entry.get("url"), color=0xe67e22)
    if entry.get("search_label"):
        embed.set_author(name=entry["search_label"])
    price  = entry.get("price")
    buynow = entry.get("buynow_price")
    if price  is not None: embed.add_field(name="💴 Current Price", value=f"¥{price:,}",  inline=True)
    if buynow is not None: embed.add_field(name="🏷️ Buy It Now",    value=f"¥{buynow:,}", inline=True)
    embed.add_field(name="⏱️ Time Left", value=fmt_remaining(entry.get("end_timestamp")), inline=True)
    if entry.get("image"): embed.set_image(url=entry["image"])
    embed.set_footer(text=f"Updated {entry.get('last_updated', '—')}")
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# Watchlist management
# ══════════════════════════════════════════════════════════════════════════════

async def add_watch_entry(data: dict, search_label: str = "") -> bool:
    if any(w["id"] == data["id"] for w in watchlist):
        return False
    entry = {
        "id":            data["id"],
        "title":         data.get("title"),
        "url":           data.get("url"),
        "image":         data.get("image"),
        "price":         data.get("price"),
        "buynow_price":  data.get("buynow_price"),
        "end_timestamp": end_ts_from_secs(data.get("time_secs")),
        "search_label":  search_label,
        "alerted_3h":    False,
        "last_updated":  datetime.now(timezone.utc).strftime("%H:%M UTC"),
        "message_id":    None,
    }
    watchlist.append(entry)
    await save_watchlist()
    await rebuild_watchlist()
    return True

async def rebuild_watchlist():
    """Update the watchlist channel. Edits cards in place (silent) when the sort order
    is unchanged; only deletes & reposts (which pings) when the order actually changes."""
    global _posted_order
    channel = client.get_channel(WATCHLIST_CHANNEL_ID)
    if not channel:
        return

    watchlist.sort(key=sort_key)
    new_order = [w["id"] for w in watchlist]
    have_all_ids = all(w.get("message_id") for w in watchlist)

    if new_order == _posted_order and have_all_ids:
        # Same order: edit each card in place — no new notifications
        for entry in watchlist:
            try:
                msg = await channel.fetch_message(entry["message_id"])
                await msg.edit(embed=make_watchlist_embed(entry), view=WatchlistItemView())
            except discord.NotFound:
                await _full_rebuild(channel)   # a card was deleted; rebuild
                return
            except discord.HTTPException:
                pass
            await asyncio.sleep(0.3)
        await save_watchlist()
    else:
        await _full_rebuild(channel)


async def _full_rebuild(channel):
    """Delete the bot's watchlist cards and repost them sorted by time left."""
    global _posted_order
    try:
        async for msg in channel.history(limit=200):
            if msg.author == client.user and msg.embeds:
                await msg.delete()
                await asyncio.sleep(0.4)
    except discord.HTTPException as e:
        print(f"[!] Error clearing watchlist: {e}")

    watchlist.sort(key=sort_key)
    for entry in watchlist:
        try:
            msg = await channel.send(embed=make_watchlist_embed(entry), view=WatchlistItemView())
            entry["message_id"] = msg.id
            await asyncio.sleep(0.4)
        except discord.HTTPException as e:
            print(f"[!] Error posting watchlist item: {e}")
    _posted_order = [w["id"] for w in watchlist]
    await save_watchlist()

async def check_ending_soon():
    """Post watchlist items with under 3 hours left to the ending-soon channel, once each."""
    if not ENDING_SOON_CHANNEL_ID:
        return
    channel = client.get_channel(ENDING_SOON_CHANNEL_ID)
    if not channel:
        return
    now     = datetime.now(timezone.utc)
    mention = " ".join(f"<@{uid}>" for uid in OWNER_IDS) if OWNER_IDS else ""
    changed = False
    for entry in watchlist:
        try:
            end = datetime.fromisoformat(entry.get("end_timestamp"))
        except Exception:
            continue
        rem = (end - now).total_seconds()
        if 0 < rem <= 3 * 3600 and not entry.get("alerted_3h"):
            entry["alerted_3h"] = True
            changed = True
            await channel.send(content=(mention or None), embed=make_watchlist_embed(entry))
    if changed:
        await save_watchlist()


# ══════════════════════════════════════════════════════════════════════════════
# Discord UI components
# ══════════════════════════════════════════════════════════════════════════════

class WatchView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="👁 Watch", style=discord.ButtonStyle.secondary,
                       custom_id="watch_listing")
    async def watch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if OWNER_IDS and interaction.user.id not in OWNER_IDS:
            await interaction.response.send_message("❌ Not authorised.", ephemeral=True); return
        if not WATCHLIST_CHANNEL_ID:
            await interaction.response.send_message("❌ WATCHLIST_CHANNEL_ID not set.", ephemeral=True); return

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if not embed or not embed.url:
            await interaction.response.send_message("❌ Couldn't read listing.", ephemeral=True); return
        m = ID_RE.search(embed.url)
        if not m:
            await interaction.response.send_message("❌ Couldn't parse auction ID.", ephemeral=True); return
        auction_id = m.group(1)

        listing = dict(listing_cache.get(auction_id, {}))
        if not listing:
            listing = {"id": auction_id, "url": embed.url, "title": embed.title}
            for f in embed.fields:
                if   "Current Price" in f.name: listing["price"]        = _price(f.value)
                elif "Buy It Now"    in f.name: listing["buynow_price"] = _price(f.value)
                elif "Time Left"     in f.name: listing["time_secs"]    = parse_english_time(f.value)
            if embed.image: listing["image"] = embed.image.url

        label = embed.author.name if embed.author else ""
        await interaction.response.defer(ephemeral=True)
        added = await add_watch_entry(listing, label)
        msg = "Added to watchlist!" if added else "Already on your watchlist."

        if added:
            done = discord.ui.View()
            b = discord.ui.Button(label="✅ Watching", style=discord.ButtonStyle.secondary, disabled=True)
            done.add_item(b)
            try:
                await interaction.message.edit(view=done)
            except discord.HTTPException:
                pass
        await interaction.followup.send(msg, ephemeral=True)


class WatchlistItemView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🗑️ Remove", style=discord.ButtonStyle.danger,
                       custom_id="watchlist_remove")
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        if OWNER_IDS and interaction.user.id not in OWNER_IDS:
            await interaction.response.send_message("❌ Not authorised.", ephemeral=True); return
        global watchlist
        watchlist = [w for w in watchlist if w.get("message_id") != interaction.message.id]
        await save_watchlist()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        await interaction.response.send_message("Removed from watchlist.", ephemeral=True)


class CategoryModal(discord.ui.Modal, title="Name this search"):
    def __init__(self, url: str, channel_id: int, default_name: str):
        super().__init__()
        self.url        = url
        self.channel_id = channel_id
        self.name_input = discord.ui.TextInput(
            label="Category name (shown at top of alerts)",
            default=default_name, max_length=80)
        self.bin_input = discord.ui.TextInput(
            label="Buy-It-Now max price in \u00a5 (optional)",
            placeholder="Leave blank for no BIN alerts", required=False, max_length=12)
        self.add_item(self.name_input)
        self.add_item(self.bin_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip() or url_to_label(self.url)
        digits = re.sub(r"[^\d]", "", self.bin_input.value or "")
        bin_max = int(digits) if digits else None
        entry = {
            "label": name, "url": self.url, "channel_id": self.channel_id,
            "enabled": True, "min_price": None, "max_price": None,
            "bin_max_price": bin_max,
            "exclude": ["ジャンク", "破損", "現状渡し"], "include_any": [], "include_all": [],
        }
        searches.append(entry)
        await interaction.response.defer(ephemeral=True)
        if await save_criteria():
            await baseline_search(entry)
            extra = f"  \u00b7  BIN alerts \u2264 \u00a5{bin_max:,}" if bin_max else ""
            await interaction.followup.send(
                f"✅ Added **{name}** → <#{self.channel_id}>{extra}", ephemeral=True)
        else:
            searches.pop()
            await interaction.followup.send("❌ Failed to save to GitHub.", ephemeral=True)


class AddSearchView(discord.ui.View):
    def __init__(self, url: str, default_name: str):
        super().__init__(timeout=300)
        self.url                = url
        self.default_name       = default_name
        self.selected_channel_id = None

    @discord.ui.select(cls=discord.ui.ChannelSelect,
                       placeholder="Select a channel for this category…",
                       channel_types=[discord.ChannelType.text])
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.selected_channel_id = select.values[0].id
        await interaction.response.defer()

    @discord.ui.button(label="Continue →", style=discord.ButtonStyle.green)
    async def cont(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_channel_id:
            await interaction.response.send_message("Pick a channel first.", ephemeral=True); return
        await interaction.response.send_modal(
            CategoryModal(self.url, self.selected_channel_id, self.default_name))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        try:
            await interaction.message.delete()
        except discord.HTTPException:
            pass
        await interaction.response.defer()


class ManageView(discord.ui.View):
    """Ephemeral UI to list / toggle / delete searches."""
    def __init__(self):
        super().__init__(timeout=300)
        self.selected: Optional[int] = None
        self._refresh()

    def _refresh(self):
        self.clear_items()
        opts = []
        for i, s in enumerate(searches):
            on = s.get("enabled", True)
            opts.append(discord.SelectOption(
                label=(s["label"] or f"search {i}")[:90], value=str(i),
                description="🟢 alerts on" if on else "🔴 alerts off",
                default=(self.selected == i)))
        sel = discord.ui.Select(placeholder="Pick a search to manage…",
                                options=opts or [discord.SelectOption(label="(no searches)", value="none")])
        sel.callback = self._on_select
        self.add_item(sel)
        if self.selected is not None and 0 <= self.selected < len(searches):
            on = searches[self.selected].get("enabled", True)
            toggle = discord.ui.Button(label="Disable alerts" if on else "Enable alerts",
                                       style=discord.ButtonStyle.secondary)
            toggle.callback = self._on_toggle
            delete = discord.ui.Button(label="Delete", style=discord.ButtonStyle.danger)
            delete.callback = self._on_delete
            self.add_item(toggle)
            self.add_item(delete)

    def _embed(self):
        e = discord.Embed(title="⚙️ Manage Searches", color=0x95a5a6)
        if not searches:
            e.description = "No searches yet — paste a Yahoo Auctions URL in this channel to add one."
            return e
        for s in searches:
            on = s.get("enabled", True)
            bin_txt = f" · 💰 BIN ≤ ¥{s['bin_max_price']:,}" if s.get("bin_max_price") else ""
            e.add_field(name=f"{'🟢' if on else '🔴'} {s['label']}",
                        value=f"→ <#{s.get('channel_id')}>{bin_txt}", inline=False)
        return e

    async def _on_select(self, interaction: discord.Interaction):
        val = interaction.data["values"][0]
        self.selected = int(val) if val.isdigit() else None
        self._refresh()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_toggle(self, interaction: discord.Interaction):
        if self.selected is None:
            await interaction.response.defer(); return
        s = searches[self.selected]
        s["enabled"] = not s.get("enabled", True)
        await save_criteria()
        self._refresh()
        await interaction.response.edit_message(embed=self._embed(), view=self)

    async def _on_delete(self, interaction: discord.Interaction):
        if self.selected is None:
            await interaction.response.defer(); return
        searches.pop(self.selected)
        self.selected = None
        await save_criteria()
        self._refresh()
        await interaction.response.edit_message(embed=self._embed(), view=self)


# ══════════════════════════════════════════════════════════════════════════════
# Core logic
# ══════════════════════════════════════════════════════════════════════════════

async def fetch_pages(url: str, seen_set: set, extra: dict = None, max_pages: int = 5) -> list:
    """Fetch newest-first pages, stopping once a page contains an already-seen listing
    (everything after it is older), results run out, or max_pages is reached. Normally
    returns just page 1; only goes deeper during a burst of new listings."""
    collected = []
    for page in range(max_pages):
        ex = dict(extra or {})
        ex["b"] = str(page * 100 + 1)
        try:
            listings = parse_listings(await fetch_html(normalize_url(url, extra=ex)))
        except Exception as e:
            print(f"[!] page {page + 1} fetch failed: {e}")
            break
        if not listings:
            break
        collected.extend(listings)
        if any(l["id"] in seen_set for l in listings):
            break                  # reached already-seen territory
        if len(listings) < 100:
            break                  # last page
        await asyncio.sleep(2)     # politeness between pages
    return collected


async def baseline_search(search: dict):
    """Mark current listings as seen in both pools so a new criteria doesn't flood."""
    label = search.get("label")
    try:
        for listing in parse_listings(await fetch_html(normalize_url(search["url"]))):
            seen_standard.add(listing["id"])
    except Exception as e:
        print(f"[!] Standard baseline failed for '{label}': {e}")
    if search.get("bin_max_price"):
        try:
            for listing in parse_listings(await fetch_html(bin_url(search["url"], search["bin_max_price"]))):
                seen_bin.add(listing["id"])
        except Exception as e:
            print(f"[!] BIN baseline failed for '{label}': {e}")
    print(f"Baselined '{label}'")

MAX_PAGES = 5  # safety cap for smart pagination (5 × 100 = up to 500 newest items)

async def collect_new(base_url, seen_set, bin_max=None):
    """Walk result pages newest-first, returning listings not yet in seen_set.
    Stops at the first already-seen item (everything older is already seen too),
    a short final page, or MAX_PAGES — so normally it only fetches page one and
    only pages deeper during a genuine burst of new listings."""
    new_items, b = [], 1
    for _ in range(MAX_PAGES):
        extra = {"b": str(b)}
        if bin_max is not None:
            extra.update({"price_type": "bidorbuyprice", "min": "", "max": str(bin_max)})
        try:
            listings = parse_listings(await fetch_html(normalize_url(base_url, extra=extra)))
        except Exception as e:
            print(f"[!] page fetch failed (b={b}): {e}")
            break
        if not listings:
            break
        hit_seen = False
        for l in listings:
            if l["id"] in seen_set:
                hit_seen = True
                break
            new_items.append(l)
        if hit_seen or len(listings) < 100:
            break
        b += 100
        await asyncio.sleep(2)
    return new_items


async def do_standard_poll():
    global first_standard
    if not searches:
        return
    if first_standard:
        print("Standard pool — first run, building baseline (no alerts).")
    for i, search in enumerate(searches):
        if not search.get("enabled", True):
            continue
        label = search.get("label", "?")

        if first_standard:
            # baseline: page one only, mark seen, no alerts, no pagination
            try:
                listings = parse_listings(await fetch_html(normalize_url(search["url"])))
            except Exception as e:
                print(f"[!] std baseline failed for '{label}': {e}"); continue
            for l in listings:
                seen_standard.add(l["id"])
            print(f"[std/{label}] baseline {len(listings)}")
        else:
            new_items = await collect_new(search["url"], seen_standard)
            print(f"[std/{label}] {len(new_items)} new")
            if watchlist:
                wl = {w["id"]: w for w in watchlist}
                for l in new_items:
                    w = wl.get(l["id"])
                    if w:
                        if l.get("price") is not None:     w["price"] = l["price"]
                        if l.get("time_secs") is not None: w["end_timestamp"] = end_ts_from_secs(l["time_secs"])
            channel = client.get_channel(int(search["channel_id"])) if search.get("channel_id") else None
            for l in new_items:
                seen_standard.add(l["id"])
                listing_cache[l["id"]] = l
                if not passes_filters(l, search):
                    continue
                if channel:
                    try:
                        await channel.send(embed=make_listing_embed(l, label), view=WatchView())
                    except discord.HTTPException as e:
                        print(f"[!] std post error: {e}")
        if i < len(searches) - 1:
            await asyncio.sleep(3)

    if len(listing_cache) > 4000:
        for k in list(listing_cache.keys())[:len(listing_cache) - 3000]:
            del listing_cache[k]
    first_standard = False
    print("Standard poll complete.")


async def do_bin_poll():
    global first_bin
    if not BIN_CHANNEL_ID:
        return
    bin_channel = client.get_channel(BIN_CHANNEL_ID)
    if not bin_channel:
        return
    active = [s for s in searches if s.get("enabled", True) and s.get("bin_max_price")]
    if not active:
        return
    if first_bin:
        print("BIN pool — first run, building baseline (no alerts).")
    for i, search in enumerate(active):
        label   = search.get("label", "?")
        bin_max = search["bin_max_price"]
        if first_bin:
            try:
                listings = parse_listings(await fetch_html(bin_url(search["url"], bin_max)))
            except Exception as e:
                print(f"[!] bin baseline failed for '{label}': {e}"); continue
            for l in listings:
                seen_bin.add(l["id"])
            print(f"[bin/{label}] baseline {len(listings)}")
        else:
            new_items = await collect_new(search["url"], seen_bin, bin_max=bin_max)
            print(f"[bin/{label}] {len(new_items)} new")
            for l in new_items:
                seen_bin.add(l["id"])
                listing_cache[l["id"]] = l
                if not passes_keyword_filters(l, search):
                    continue
                try:
                    await bin_channel.send(
                        embed=make_listing_embed(l, f"{label} · BIN ≤ ¥{bin_max:,}"),
                        view=WatchView())
                except discord.HTTPException as e:
                    print(f"[!] bin post error: {e}")
        if i < len(active) - 1:
            await asyncio.sleep(3)
    first_bin = False
    print("BIN poll complete.")


# ══════════════════════════════════════════════════════════════════════════════
# Task loops
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(minutes=STANDARD_MINUTES)
async def standard_loop():
    await do_standard_poll()

@tasks.loop(minutes=BIN_MINUTES)
async def bin_loop():
    await do_bin_poll()

@tasks.loop(minutes=UPDATE_MINUTES)
async def watchlist_loop():
    await do_watchlist_update()

@standard_loop.before_loop
async def before_standard():
    await client.wait_until_ready()

@bin_loop.before_loop
async def before_bin():
    await client.wait_until_ready()

@watchlist_loop.before_loop
async def before_watchlist():
    await client.wait_until_ready()


# ══════════════════════════════════════════════════════════════════════════════
# Slash commands
# ══════════════════════════════════════════════════════════════════════════════

@tree.command(name="manage", description="List, enable/disable, or delete searches")
async def manage(interaction: discord.Interaction):
    v = ManageView()
    await interaction.response.send_message(embed=v._embed(), view=v, ephemeral=True)

@tree.command(name="debug-selectors", description="Inspect a search or auction page")
@app_commands.describe(url="A search URL (dumps parsed listings) or an auction URL (dumps embedded data)")
async def debug_selectors(interaction: discord.Interaction, url: str):
    await interaction.response.defer(ephemeral=True)

    # --- Search URL: dump what the bot parses, to verify current vs BIN price ---
    if "/search/search" in url:
        try:
            html = await fetch_html(normalize_url(url))
        except Exception as e:
            await interaction.followup.send(f"Fetch failed: {e}", ephemeral=True); return
        listings = parse_listings(html)
        lines = [f"parsed {len(listings)} listings (first 6):", ""]
        for l in listings[:6]:
            lines.append(f"current={l['price']}  BIN={l['buynow_price']}  time={l['time_left']}")
            lines.append(f"  {(l['title'] or '')[:48]}")
        out = "\n".join(lines)[:1900]
        await interaction.followup.send(f"```\n{out}\n```", ephemeral=True); return

    # --- Auction URL: inspect embedded JSON ---
    m = YAHOO_ITEM_RE.search(url)
    if not m:
        await interaction.followup.send("That doesn't look like a Yahoo search or auction URL.", ephemeral=True); return
    aid = m.group(1)
    try:
        html = await fetch_html(f"https://page.auctions.yahoo.co.jp/jp/auction/{aid}")
    except Exception as e:
        await interaction.followup.send(f"Fetch failed: {e}", ephemeral=True); return

    lines = [f"HTML length: {len(html):,}", f"'円' count: {html.count('円')}"]
    for marker in ("__NEXT_DATA__", "__PRELOADED_STATE__", "__INITIAL_STATE__", "application/ld+json"):
        lines.append(f"{marker}: {'YES' if marker in html else 'no'}")
    soup = BeautifulSoup(html, "html.parser")
    for tag in soup.find_all("script", attrs={"type": "application/ld+json"}):
        try:
            data = json.loads(tag.string or "{}")
            blob = json.dumps(data, ensure_ascii=False).lower()
            if "price" in blob or "offer" in blob:
                keys = list(data.keys()) if isinstance(data, dict) else ["(list)"]
                lines.append("ld+json keys: " + ", ".join(keys[:12]))
                break
        except Exception:
            pass
    lines.append("-- candidate JSON fields --")
    patt = re.compile(r'"([A-Za-z_]*(?:price|Price|bid|Bid|[Ee]nd|[Tt]ime)[A-Za-z_]*)"\s*:\s*("?[^",}\]]{1,40})')
    seen = []
    for mm in patt.finditer(html):
        pair = f"{mm.group(1)}: {mm.group(2)}"
        if pair not in seen:
            seen.append(pair)
        if len(seen) >= 22:
            break
    lines.extend(seen or ["(none found)"])
    out = "\n".join(lines)[:1900]
    await interaction.followup.send(f"```\n{out}\n```", ephemeral=True)


@tree.command(name="check-now", description="Trigger an immediate poll right now")
async def check_now(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🔍 Checking {len(searches)} search(es) now...", ephemeral=True)
    asyncio.create_task(do_standard_poll())
    asyncio.create_task(do_bin_poll())


# ══════════════════════════════════════════════════════════════════════════════
# Message listener
# ══════════════════════════════════════════════════════════════════════════════

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if OWNER_IDS and message.author.id not in OWNER_IDS:
        return

    # Add-criteria channel: paste a search URL
    if ADD_CRITERIA_CHANNEL_ID and message.channel.id == ADD_CRITERIA_CHANNEL_ID:
        match = YAHOO_SEARCH_RE.search(message.content)
        if not match:
            return
        url   = match.group(0)
        label = url_to_label(url)
        embed = discord.Embed(title="New search detected",
                              description="Pick a channel, then click Continue to name it.",
                              color=0x3498db)
        embed.add_field(name="URL", value=url, inline=False)
        await message.reply(embed=embed, view=AddSearchView(url, label))
        return

    # Watchlist channel: paste an individual auction URL
    if WATCHLIST_CHANNEL_ID and message.channel.id == WATCHLIST_CHANNEL_ID:
        m = YAHOO_ITEM_RE.search(message.content)
        if not m:
            return
        auction_id = m.group(1)
        notice = await message.reply(f"Adding `{auction_id}` to your watchlist…")
        details = await fetch_item_details(auction_id)
        if not details.get("id"):
            await notice.edit(content="❌ Couldn't fetch that listing.")
            return
        added = await add_watch_entry(details, "Added manually")
        await notice.edit(content="✅ Added to watchlist." if added else "Already on your watchlist.")
        try:
            await message.delete()
        except discord.HTTPException:
            pass
        return


# ══════════════════════════════════════════════════════════════════════════════
# Startup
# ══════════════════════════════════════════════════════════════════════════════

@client.event
async def on_ready():
    print(f"Logged in as {client.user} ({client.user.id})")
    await load_criteria()
    print(f"Loaded {len(searches)} search(es)")
    await load_watchlist()
    print(f"Loaded {len(watchlist)} watchlist item(s)")
    global _posted_order
    watchlist.sort(key=sort_key)
    _posted_order = [w["id"] for w in watchlist]
    client.add_view(WatchView())
    client.add_view(WatchlistItemView())
    await tree.sync()
    print("Slash commands synced")
    if not standard_loop.is_running():
        standard_loop.start()
    if not bin_loop.is_running():
        bin_loop.start()
    if not watchlist_loop.is_running():
        watchlist_loop.start()
    print(f"Running — standard {STANDARD_MINUTES}m, BIN {BIN_MINUTES}m, watchlist {UPDATE_MINUTES}m")


def main():
    try:
        client.run(BOT_TOKEN)
    except discord.errors.HTTPException as e:
        if getattr(e, "status", None) == 429:
            # A rate-limited login means recent restarts were too frequent. Wait a long
            # time before exiting so Railway's restart doesn't keep hammering the endpoint.
            print("[startup] Discord login rate-limited (429). Sleeping 10 minutes before "
                  "exit so restarts don't sustain the block.", flush=True)
            time.sleep(600)
        raise


if __name__ == "__main__":
    main()
