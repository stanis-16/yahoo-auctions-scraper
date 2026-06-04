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
POLL_MINUTES   = 20
UPDATE_MINUTES = 15

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")


# ── In-memory state ────────────────────────────────────────────────────────────
searches:      list = []
seen_ids:      set  = set()
watchlist:     list = []
listing_cache: dict = {}
first_run:     bool = True


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

def normalize_url(url: str) -> str:
    p = urlparse(url)
    q = parse_qs(p.query)
    q.update({"s1": ["new"], "o1": ["d"], "n": ["50"]})
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))

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
    """Delete the bot's watchlist embed messages and repost all items sorted by time left."""
    channel = client.get_channel(WATCHLIST_CHANNEL_ID)
    if not channel:
        return
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
        self.add_item(self.name_input)

    async def on_submit(self, interaction: discord.Interaction):
        name = self.name_input.value.strip() or url_to_label(self.url)
        entry = {
            "label": name, "url": self.url, "channel_id": self.channel_id,
            "enabled": True, "min_price": None, "max_price": None,
            "exclude": ["ジャンク", "破損", "現状渡し"], "include_any": [], "include_all": [],
        }
        searches.append(entry)
        await interaction.response.defer(ephemeral=True)
        if await save_criteria():
            await baseline_search(entry)
            await interaction.followup.send(
                f"✅ Added **{name}** → <#{self.channel_id}>", ephemeral=True)
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
            e.add_field(name=f"{'🟢' if on else '🔴'} {s['label']}",
                        value=f"→ <#{s.get('channel_id')}>", inline=False)
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

async def baseline_search(search: dict):
    url = normalize_url(search.get("url", ""))
    if not url:
        return
    try:
        for listing in parse_listings(await fetch_html(url)):
            seen_ids.add(listing["id"])
        print(f"Baselined '{search.get('label')}'")
    except Exception as e:
        print(f"[!] Baseline failed for '{search.get('label')}': {e}")

async def do_poll():
    global first_run
    if not searches:
        print("No searches configured yet."); return
    if first_run:
        print("First run — building baseline...")

    bin_channel = client.get_channel(BIN_CHANNEL_ID) if BIN_CHANNEL_ID else None

    for i, search in enumerate(searches):
        if not search.get("enabled", True):
            continue
        url   = normalize_url(search.get("url", ""))
        label = search.get("label", "?")
        if not url:
            continue
        try:
            html = await fetch_html(url)
        except Exception as e:
            print(f"[!] Fetch failed for '{label}': {e}"); continue

        listings = parse_listings(html)
        print(f"[{label}] {len(listings)} listings parsed")

        # Refresh any watched items that still appear in these results
        if watchlist:
            wl_by_id = {w["id"]: w for w in watchlist}
            for listing in listings:
                w = wl_by_id.get(listing["id"])
                if w:
                    if listing.get("price") is not None:
                        w["price"] = listing["price"]
                    if listing.get("time_secs") is not None:
                        w["end_timestamp"] = end_ts_from_secs(listing["time_secs"])

        if first_run:
            for listing in listings:
                seen_ids.add(listing["id"])
        else:
            channel = client.get_channel(int(search["channel_id"])) if search.get("channel_id") else None
            for listing in listings:
                if listing["id"] in seen_ids:
                    continue
                seen_ids.add(listing["id"])
                listing_cache[listing["id"]] = listing
                if not passes_filters(listing, search):
                    continue
                embed = make_listing_embed(listing, label)
                if channel:
                    try:
                        await channel.send(embed=embed, view=WatchView())
                    except discord.HTTPException as e:
                        print(f"[!] post error: {e}")
                if bin_channel and listing.get("buynow_price"):
                    try:
                        await bin_channel.send(embed=make_listing_embed(listing, label), view=WatchView())
                    except discord.HTTPException as e:
                        print(f"[!] BIN post error: {e}")

        if i < len(searches) - 1:
            await asyncio.sleep(3)

    first_run = False
    print("Poll complete.")

async def do_watchlist_update():
    if not watchlist:
        return
    # Time remaining is computed live from each item's end_timestamp, so no
    # per-item re-fetch is needed. Prices refresh in do_poll() whenever a watched
    # item still shows up in a search's results.
    await check_ending_soon()
    await rebuild_watchlist()
    print("Watchlist refreshed.")


# ══════════════════════════════════════════════════════════════════════════════
# Task loops
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(minutes=POLL_MINUTES)
async def poll_loop():
    await do_poll()

@tasks.loop(minutes=UPDATE_MINUTES)
async def watchlist_loop():
    await do_watchlist_update()

@poll_loop.before_loop
async def before_poll():
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

@tree.command(name="debug-selectors", description="Test which selectors work on an auction page")
@app_commands.describe(url="An individual Yahoo auction URL")
async def debug_selectors(interaction: discord.Interaction, url: str):
    await interaction.response.defer(ephemeral=True)
    m = YAHOO_ITEM_RE.search(url)
    if not m:
        await interaction.followup.send("That doesn't look like an auction URL.", ephemeral=True); return
    aid = m.group(1)
    try:
        html = await fetch_html(f"https://page.auctions.yahoo.co.jp/jp/auction/{aid}")
    except Exception as e:
        await interaction.followup.send(f"Fetch failed: {e}", ephemeral=True); return
    soup  = BeautifulSoup(html, "html.parser")
    lines = []
    for prop in ("og:title", "og:image"):
        tag = soup.find("meta", attrs={"property": prop})
        lines.append(f"{prop}: {(tag.get('content')[:40] + '...') if tag and tag.get('content') else 'MISSING'}")
    lines.append(f"ITEM_PRICE_SEL ({ITEM_PRICE_SEL}) -> {_text(soup.select_one(ITEM_PRICE_SEL)) or 'NONE'}")
    lines.append(f"ITEM_TIME_SEL  ({ITEM_TIME_SEL}) -> {_text(soup.select_one(ITEM_TIME_SEL)) or 'NONE'}")
    lines.append("\n-- price-like classes --")
    seen = set()
    for el in soup.find_all(class_=re.compile("Price|price")):
        cls = ".".join(el.get("class", []))
        if cls in seen: continue
        seen.add(cls)
        txt = el.get_text(strip=True)[:25]
        if txt: lines.append(f".{cls} = {txt}")
        if len(seen) > 10: break
    lines.append("\n-- time/count-like classes --")
    seen = set()
    for el in soup.find_all(class_=re.compile("Count|Time|count|time|End|end")):
        cls = ".".join(el.get("class", []))
        if cls in seen: continue
        seen.add(cls)
        txt = el.get_text(strip=True)[:25]
        if txt: lines.append(f".{cls} = {txt}")
        if len(seen) > 10: break
    out = "\n".join(lines)[:1900]
    await interaction.followup.send(f"```\n{out}\n```", ephemeral=True)


@tree.command(name="check-now", description="Trigger an immediate poll right now")
async def check_now(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🔍 Checking {len(searches)} search(es) now...", ephemeral=True)
    asyncio.create_task(do_poll())


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
    client.add_view(WatchView())
    client.add_view(WatchlistItemView())
    await tree.sync()
    print("Slash commands synced")
    if not poll_loop.is_running():
        poll_loop.start()
    if not watchlist_loop.is_running():
        watchlist_loop.start()
    print(f"Running — poll every {POLL_MINUTES} min, watchlist every {UPDATE_MINUTES} min")


if __name__ == "__main__":
    client.run(BOT_TOKEN)
