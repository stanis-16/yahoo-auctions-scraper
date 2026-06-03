#!/usr/bin/env python3
"""
Yahoo Auctions Japan — Discord monitor bot

Features:
  - Posts new listings to category-specific channels with a Watch button
  - Watchlist channel tracks watched items and refreshes price/time every 30 min
  - Paste a Yahoo Auctions search URL in #add-criteria to add a new search
  - Baseline run on every new search prevents notification floods
  - Slash commands: /list-searches  /remove-search  /check-now

Environment variables (set in Railway):
  DISCORD_BOT_TOKEN       Discord bot token
  GITHUB_TOKEN            GitHub PAT with repo scope
  GITHUB_REPO             e.g. "yourname/yahoo-monitor"
  OWNER_IDS               Comma-separated Discord user IDs
  WATCHLIST_CHANNEL_ID    Channel ID for the watchlist
  ADD_CRITERIA_CHANNEL_ID Channel ID where you paste URLs to add searches

Discord Developer Portal — required before deploying:
  Bot → Privileged Gateway Intents → Message Content Intent → ON
"""

import asyncio
import base64
import json
import os
import re
from datetime import datetime, timezone
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

OWNER_IDS: set[int] = {
    int(x.strip()) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()
}

CRITERIA_FILE  = "criteria.json"
WATCHLIST_FILE = "watchlist.json"
POLL_MINUTES   = 20
UPDATE_MINUTES = 30

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")


# ── In-memory state ────────────────────────────────────────────────────────────
searches:      list = []
seen_ids:      set  = set()
watchlist:     list = []
listing_cache: dict = {}   # auction_id → listing dict (for Watch button lookups)
first_run:     bool = True


# ── CSS selectors ──────────────────────────────────────────────────────────────
# Search results page
ITEM_SEL  = "li.Product"
LINK_SEL  = "a.Product__titleLink"
PRICE_SEL = ".Product__priceValue"
BIDS_SEL  = ".Product__bid"
TIME_SEL  = ".Product__time"
IMG_SEL   = "img.Product__imageData"
ID_RE     = re.compile(r"/auction/([a-zA-Z]?\d+)")

# Individual item page (for watchlist updates) — adjust if Yahoo changes markup
ITEM_PRICE_SEL = ".Price__value"
ITEM_TIME_SEL  = ".Count__item"

YAHOO_SEARCH_RE = re.compile(r"https://auctions\.yahoo\.co\.jp/search/search\S+")


# ── Discord setup ──────────────────────────────────────────────────────────────
intents = discord.Intents.default()
intents.message_content = True   # required to read pasted URLs in #add-criteria


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
# Yahoo helpers
# ══════════════════════════════════════════════════════════════════════════════

def normalize_url(url: str) -> str:
    p = urlparse(url)
    q = parse_qs(p.query)
    q.update({"s1": ["new"], "o1": ["d"], "n": ["50"]})
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))

def url_to_label(url: str) -> str:
    """Auto-generate a search label from the URL keyword param."""
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
        href   = link["href"]
        m      = ID_RE.search(href)
        aid    = m.group(1) if m else link.get("data-auction-id")
        if not aid:
            continue
        prices = item.select(PRICE_SEL)
        img    = item.select_one(IMG_SEL)
        out.append({
            "id":           aid,
            "title":        _text(link),
            "price":        _price(_text(prices[0])) if prices else None,
            "buynow_price": _price(_text(prices[1])) if len(prices) > 1 else None,
            "bids":         _text(item.select_one(BIDS_SEL)),
            "time_left":    _text(item.select_one(TIME_SEL)),
            "url":          href,
            "image":        (img.get("src") or img.get("data-src")) if img else None,
        })
    return out

async def fetch_item_details(auction_id: str) -> dict:
    """Fetch updated price and time from an individual auction page for watchlist updates."""
    try:
        html       = await fetch_html(f"https://page.auctions.yahoo.co.jp/jp/auction/{auction_id}")
        soup       = BeautifulSoup(html, "html.parser")
        price_node = soup.select_one(ITEM_PRICE_SEL)
        time_node  = soup.select_one(ITEM_TIME_SEL)
        return {"price": _price(_text(price_node)), "time_left": _text(time_node)}
    except Exception:
        return {}

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

def make_listing_embed(listing: dict, search: dict) -> discord.Embed:
    embed = discord.Embed(
        title=listing.get("title") or "No title",
        url=listing.get("url"),
        color=0x2ecc71,
    )
    price  = listing.get("price")
    buynow = listing.get("buynow_price")
    if price  is not None: embed.add_field(name="💴 Current Price", value=f"¥{price:,}",  inline=True)
    if buynow is not None: embed.add_field(name="🏷️ Buy It Now",    value=f"¥{buynow:,}", inline=True)
    if listing.get("bids"):      embed.add_field(name="📊 Bids",      value=listing["bids"],      inline=True)
    if listing.get("time_left"): embed.add_field(name="⏱️ Time Left", value=listing["time_left"], inline=True)
    if listing.get("image"):     embed.set_thumbnail(url=listing["image"])
    embed.set_footer(text=f"🔍 {search.get('label', '')}")
    return embed

def make_watchlist_embed(entry: dict) -> discord.Embed:
    embed = discord.Embed(
        title=entry.get("title") or "No title",
        url=entry.get("url"),
        color=0xe67e22,
    )
    price  = entry.get("price")
    buynow = entry.get("buynow_price")
    if price  is not None: embed.add_field(name="💴 Current Price", value=f"¥{price:,}",  inline=True)
    if buynow is not None: embed.add_field(name="🏷️ Buy It Now",    value=f"¥{buynow:,}", inline=True)
    if entry.get("time_left"): embed.add_field(name="⏱️ Time Left", value=entry["time_left"], inline=True)
    if entry.get("image"):     embed.set_thumbnail(url=entry["image"])
    embed.set_footer(
        text=f"👁 Watching · Updated {entry.get('last_updated', '—')} · {entry.get('search_label', '')}"
    )
    return embed


# ══════════════════════════════════════════════════════════════════════════════
# Discord UI components
# ══════════════════════════════════════════════════════════════════════════════

class WatchView(discord.ui.View):
    """Persistent Watch button attached to every listing embed."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="👁 Watch", style=discord.ButtonStyle.secondary,
                       custom_id="watch_listing")
    async def watch(self, interaction: discord.Interaction, button: discord.ui.Button):
        if OWNER_IDS and interaction.user.id not in OWNER_IDS:
            await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
            return
        if not WATCHLIST_CHANNEL_ID:
            await interaction.response.send_message(
                "❌ WATCHLIST_CHANNEL_ID not set in Railway.", ephemeral=True)
            return

        embed = interaction.message.embeds[0] if interaction.message.embeds else None
        if not embed or not embed.url:
            await interaction.response.send_message("❌ Couldn't read listing.", ephemeral=True)
            return

        m = ID_RE.search(embed.url)
        if not m:
            await interaction.response.send_message("❌ Couldn't parse auction ID.", ephemeral=True)
            return
        auction_id = m.group(1)

        if any(w["id"] == auction_id for w in watchlist):
            await interaction.response.send_message("Already on your watchlist.", ephemeral=True)
            return

        # Get listing details from cache, or reconstruct from the embed
        listing = dict(listing_cache.get(auction_id, {}))
        if not listing:
            listing = {"id": auction_id, "url": embed.url, "title": embed.title}
            for field in embed.fields:
                if "Current Price" in field.name:
                    listing["price"]        = _price(field.value)
                elif "Buy It Now"   in field.name:
                    listing["buynow_price"] = _price(field.value)
                elif "Time Left"    in field.name:
                    listing["time_left"]    = field.value
            if embed.thumbnail:
                listing["image"] = embed.thumbnail.url

        wl_channel = client.get_channel(WATCHLIST_CHANNEL_ID)
        if not wl_channel:
            await interaction.response.send_message("❌ Watchlist channel not found.", ephemeral=True)
            return

        entry = {
            **listing,
            "search_label": (embed.footer.text or "").replace("🔍 ", ""),
            "last_updated":  datetime.now(timezone.utc).strftime("%H:%M UTC"),
            "message_id":    None,
        }
        wl_msg         = await wl_channel.send(embed=make_watchlist_embed(entry), view=WatchlistItemView())
        entry["message_id"] = wl_msg.id
        watchlist.append(entry)
        await save_watchlist()

        # Replace the Watch button with a disabled "Watching" state on this specific message
        done_view = discord.ui.View()
        done_btn  = discord.ui.Button(label="✅ Watching", style=discord.ButtonStyle.secondary, disabled=True)
        done_view.add_item(done_btn)
        await interaction.message.edit(view=done_view)
        await interaction.response.send_message("Added to watchlist!", ephemeral=True)


class WatchlistItemView(discord.ui.View):
    """Persistent Remove button on each watchlist embed."""

    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(label="🗑️ Remove", style=discord.ButtonStyle.danger,
                       custom_id="watchlist_remove")
    async def remove(self, interaction: discord.Interaction, button: discord.ui.Button):
        if OWNER_IDS and interaction.user.id not in OWNER_IDS:
            await interaction.response.send_message("❌ Not authorised.", ephemeral=True)
            return
        global watchlist
        watchlist = [w for w in watchlist if w.get("message_id") != interaction.message.id]
        await save_watchlist()
        await interaction.message.delete()
        await interaction.response.send_message("Removed from watchlist.", ephemeral=True)


class AddSearchView(discord.ui.View):
    """Shown when a Yahoo Auctions URL is pasted in #add-criteria."""

    def __init__(self, url: str, label: str):
        super().__init__(timeout=300)
        self.url              = url
        self.label            = label
        self.selected_channel = None

    @discord.ui.select(
        cls=discord.ui.ChannelSelect,
        placeholder="Select a channel to post listings in...",
        channel_types=[discord.ChannelType.text],
    )
    async def channel_select(self, interaction: discord.Interaction, select: discord.ui.ChannelSelect):
        self.selected_channel = select.values[0]
        await interaction.response.defer()

    @discord.ui.button(label="✅ Add Search", style=discord.ButtonStyle.green)
    async def confirm(self, interaction: discord.Interaction, button: discord.ui.Button):
        if not self.selected_channel:
            await interaction.response.send_message("Pick a channel first.", ephemeral=True)
            return

        entry = {
            "label":       self.label,
            "url":         self.url,
            "channel_id":  self.selected_channel.id,
            "min_price":   None,
            "max_price":   None,
            "exclude":     ["ジャンク", "破損", "現状渡し"],
            "include_any": [],
            "include_all": [],
        }
        searches.append(entry)

        if await save_criteria():
            await baseline_search(entry)
            self.stop()
            for child in self.children:
                child.disabled = True
            await interaction.message.edit(
                content=f"✅ Added **{self.label}** → {self.selected_channel.mention}",
                view=self,
            )
            await interaction.response.defer()
        else:
            searches.pop()
            await interaction.response.send_message("❌ Failed to save to GitHub.", ephemeral=True)

    @discord.ui.button(label="❌ Cancel", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        self.stop()
        await interaction.message.delete()
        await interaction.response.defer()


# ══════════════════════════════════════════════════════════════════════════════
# Core logic
# ══════════════════════════════════════════════════════════════════════════════

async def baseline_search(search: dict):
    """Silently mark all current listings as seen so the first real poll is clean."""
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
        print("No searches configured yet.")
        return

    if first_run:
        print("First run — building baseline, no alerts this pass...")

    for i, search in enumerate(searches):
        url   = normalize_url(search.get("url", ""))
        label = search.get("label", "?")
        if not url:
            continue

        try:
            html = await fetch_html(url)
        except Exception as e:
            print(f"[!] Fetch failed for '{label}': {e}")
            continue

        listings = parse_listings(html)
        print(f"[{label}] {len(listings)} listings parsed")

        if first_run:
            for listing in listings:
                seen_ids.add(listing["id"])
        else:
            channel_id = search.get("channel_id")
            channel    = client.get_channel(int(channel_id)) if channel_id else None
            if channel_id and not channel:
                print(f"[!] Channel {channel_id} not found")

            for listing in listings:
                if listing["id"] in seen_ids:
                    continue
                seen_ids.add(listing["id"])
                listing_cache[listing["id"]] = listing
                if not passes_filters(listing, search):
                    continue
                if channel:
                    try:
                        await channel.send(
                            embed=make_listing_embed(listing, search),
                            view=WatchView(),
                        )
                    except discord.Forbidden:
                        print(f"[!] No permission in channel {channel_id}")
                    except discord.HTTPException as e:
                        print(f"[!] Discord error: {e}")

        if i < len(searches) - 1:
            await asyncio.sleep(3)

    first_run = False
    print("Poll complete.")


async def do_watchlist_update():
    """Re-fetch each watchlist item and edit its Discord embed with fresh data."""
    if not watchlist:
        return
    channel = client.get_channel(WATCHLIST_CHANNEL_ID)
    if not channel:
        return

    for entry in watchlist:
        if not entry.get("id") or not entry.get("message_id"):
            continue
        details = await fetch_item_details(entry["id"])
        if details.get("price")     is not None: entry["price"]     = details["price"]
        if details.get("time_left") is not None: entry["time_left"] = details["time_left"]
        entry["last_updated"] = datetime.now(timezone.utc).strftime("%H:%M UTC")
        try:
            msg = await channel.fetch_message(entry["message_id"])
            await msg.edit(embed=make_watchlist_embed(entry), view=WatchlistItemView())
        except discord.NotFound:
            print(f"[!] Watchlist message {entry['message_id']} not found (deleted?)")
        except discord.HTTPException as e:
            print(f"[!] Failed to update watchlist message: {e}")
        await asyncio.sleep(2)

    await save_watchlist()
    print("Watchlist updated.")


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

@tree.command(name="list-searches", description="Show all active searches")
async def list_searches(interaction: discord.Interaction):
    if not searches:
        await interaction.response.send_message("No searches yet. Paste a URL in #add-criteria.", ephemeral=True)
        return
    embed = discord.Embed(title="🔍 Active Searches", color=0x3498db)
    for s in searches:
        cid   = s.get("channel_id")
        lines = [f"Channel: <#{cid}>" if cid else "⚠️ No channel set"]
        if s.get("max_price"):   lines.append(f"Max: ¥{s['max_price']:,}")
        if s.get("min_price"):   lines.append(f"Min: ¥{s['min_price']:,}")
        if s.get("exclude"):     lines.append(f"Exclude: {', '.join(s['exclude'])}")
        if s.get("include_any"): lines.append(f"Include any: {', '.join(s['include_any'])}")
        embed.add_field(name=s["label"], value="\n".join(lines), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="remove-search", description="Remove a search by its label")
@app_commands.describe(label="Label of the search to remove")
async def remove_search(interaction: discord.Interaction, label: str):
    await interaction.response.defer(ephemeral=True)
    before      = len(searches)
    searches[:] = [s for s in searches if s["label"].lower() != label.lower()]
    if len(searches) == before:
        await interaction.followup.send(f'❌ No search called **{label}**. Check `/list-searches`.', ephemeral=True)
        return
    await save_criteria()
    await interaction.followup.send(f'✅ Removed **{label}**.', ephemeral=True)


@tree.command(name="check-now", description="Trigger an immediate poll right now")
async def check_now(interaction: discord.Interaction):
    await interaction.response.send_message(
        f"🔍 Checking {len(searches)} search(es) now...", ephemeral=True)
    asyncio.create_task(do_poll())


# ══════════════════════════════════════════════════════════════════════════════
# Message listener — paste a URL in #add-criteria to add a search
# ══════════════════════════════════════════════════════════════════════════════

@client.event
async def on_message(message: discord.Message):
    if message.author.bot or not message.guild:
        return
    if not ADD_CRITERIA_CHANNEL_ID or message.channel.id != ADD_CRITERIA_CHANNEL_ID:
        return
    if OWNER_IDS and message.author.id not in OWNER_IDS:
        return

    match = YAHOO_SEARCH_RE.search(message.content)
    if not match:
        return

    url   = match.group(0)
    label = url_to_label(url)
    embed = discord.Embed(
        title="New search detected",
        description=f"**Auto-label:** `{label}`\n\nPick a channel, then click Add Search.",
        color=0x3498db,
    )
    embed.add_field(name="URL", value=url, inline=False)
    await message.reply(embed=embed, view=AddSearchView(url, label))


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

    # Re-register persistent views so buttons still work after restarts
    client.add_view(WatchView())
    client.add_view(WatchlistItemView())

    await tree.sync()
    print("Slash commands synced")

    if not poll_loop.is_running():
        poll_loop.start()
    if not watchlist_loop.is_running():
        watchlist_loop.start()

    print(f"Running — polling every {POLL_MINUTES} min, watchlist updates every {UPDATE_MINUTES} min")


if __name__ == "__main__":
    client.run(BOT_TOKEN)
