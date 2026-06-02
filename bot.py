#!/usr/bin/env python3
"""
Yahoo Auctions Japan — Discord monitor bot

What it does:
  - Polls Yahoo Auctions every 20 minutes for new listings matching your searches
  - Posts a rich embed (image, title, prices, bids, time left, link) to the right channel
  - Remembers what it has shown you so you never see a duplicate
  - Manages criteria via Discord slash commands — no need to edit files

Slash commands:
  /add-search    — add a new search with filters, routed to a specific channel
  /remove-search — remove a search by label
  /list-searches — see all active searches and their settings
  /check-now     — trigger an immediate poll without waiting 20 minutes

Setup: set these environment variables (in Railway or a .env file):
  DISCORD_BOT_TOKEN  — from Discord Developer Portal → Bot → Token
  GITHUB_TOKEN       — GitHub Personal Access Token with `repo` scope
  GITHUB_REPO        — your repo, e.g. "yourname/yahoo-monitor"
"""

import asyncio
import base64
import json
import os
import re
from typing import Optional
from urllib.parse import urlparse, parse_qs, urlencode, urlunparse

import aiohttp
import discord
from discord import app_commands
from discord.ext import tasks
from bs4 import BeautifulSoup


# ── Environment variables ──────────────────────────────────────────────────────
BOT_TOKEN    = os.environ["DISCORD_BOT_TOKEN"]
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]
GITHUB_REPO  = os.environ["GITHUB_REPO"]    # e.g. "yourname/yahoo-monitor"

CRITERIA_FILE = "criteria.json"
POLL_MINUTES  = 20

# Comma-separated Discord user IDs allowed to use slash commands.
# Leave empty to allow everyone (not recommended for a personal bot).
# Get your ID: Discord Settings → Advanced → enable Developer Mode,
# then right-click your username anywhere → Copy User ID.
OWNER_IDS: set[int] = {
    int(x.strip()) for x in os.environ.get("OWNER_IDS", "").split(",") if x.strip()
}

USER_AGENT = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
              "AppleWebKit/537.36 (KHTML, like Gecko) "
              "Chrome/124.0.0.0 Safari/537.36")

# ── In-memory state ────────────────────────────────────────────────────────────
searches:  list = []
seen_ids:  set  = set()
first_run: bool = True

# ── CSS selectors (update here if Yahoo changes their markup) ──────────────────
ITEM_SEL  = "li.Product"
LINK_SEL  = "a.Product__titleLink"
PRICE_SEL = ".Product__priceValue"
BIDS_SEL  = ".Product__bid"
TIME_SEL  = ".Product__time"
IMG_SEL   = "img.Product__imageData"
ID_RE     = re.compile(r"/auction/([a-zA-Z]?\d+)")

# ── Discord client ─────────────────────────────────────────────────────────────
class OwnerOnlyTree(app_commands.CommandTree):
    """Gates all slash commands to users listed in OWNER_IDS."""
    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if OWNER_IDS and interaction.user.id not in OWNER_IDS:
            await interaction.response.send_message(
                "❌ This bot is private.", ephemeral=True)
            return False
        return True

intents = discord.Intents.default()
client  = discord.Client(intents=intents)
tree    = OwnerOnlyTree(client)


# ══════════════════════════════════════════════════════════════════════════════
# GitHub helpers — criteria.json lives in your repo as the source of truth
# ══════════════════════════════════════════════════════════════════════════════

GH_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

async def gh_read(path: str):
    """Return (parsed_content, sha) for a file in the repo, or (None, None)."""
    url = f"https://api.github.com/repos/{GITHUB_REPO}/contents/{path}"
    async with aiohttp.ClientSession() as s:
        async with s.get(url, headers=GH_HEADERS) as r:
            if r.status == 404:
                return None, None
            data = await r.json()
    return json.loads(base64.b64decode(data["content"]).decode()), data["sha"]

async def gh_write(path: str, content, sha: Optional[str], message: str) -> bool:
    """Create or update a file in the repo. Pass sha=None to create a new file."""
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


# ══════════════════════════════════════════════════════════════════════════════
# Yahoo Auctions scraping
# ══════════════════════════════════════════════════════════════════════════════

def normalize_url(url: str) -> str:
    """Force newest-first sort and page size onto a pasted search URL."""
    p = urlparse(url)
    q = parse_qs(p.query)
    q.update({"s1": ["new"], "o1": ["d"], "n": ["50"]})
    return urlunparse(p._replace(query=urlencode(q, doseq=True)))

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
# Discord embed builder
# ══════════════════════════════════════════════════════════════════════════════

def make_embed(listing: dict, search: dict) -> discord.Embed:
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


# ══════════════════════════════════════════════════════════════════════════════
# Core poll — called by the task loop and by /check-now
# ══════════════════════════════════════════════════════════════════════════════

async def do_poll():
    global first_run

    if not searches:
        print("No searches configured yet.")
        return

    if first_run:
        print("First run — building baseline, no alerts this pass...")

    for i, search in enumerate(searches):
        url = normalize_url(search.get("url", ""))
        if not url:
            continue
        label = search.get("label", "?")

        try:
            html = await fetch_html(url)
        except Exception as e:
            print(f"[!] Fetch failed for '{label}': {e}")
            continue

        listings = parse_listings(html)
        print(f"[{label}] {len(listings)} listings parsed")

        if first_run:
            # Silently mark everything seen — establishes a baseline so the
            # first real poll only alerts on genuinely new listings.
            for listing in listings:
                seen_ids.add(listing["id"])
        else:
            channel_id = search.get("channel_id")
            channel    = client.get_channel(int(channel_id)) if channel_id else None
            if channel_id and not channel:
                print(f"[!] Channel {channel_id} not found — is the bot in that server/channel?")

            for listing in listings:
                if listing["id"] in seen_ids:
                    continue
                seen_ids.add(listing["id"])
                if not passes_filters(listing, search):
                    continue
                if channel:
                    try:
                        await channel.send(embed=make_embed(listing, search))
                    except discord.Forbidden:
                        print(f"[!] Missing permission to post in channel {channel_id}")
                    except discord.HTTPException as e:
                        print(f"[!] Discord error posting listing: {e}")

        if i < len(searches) - 1:
            await asyncio.sleep(3)  # polite pause between searches

    first_run = False
    print("Poll complete.")


# ══════════════════════════════════════════════════════════════════════════════
# Task loop
# ══════════════════════════════════════════════════════════════════════════════

@tasks.loop(minutes=POLL_MINUTES)
async def poll_loop():
    await do_poll()

@poll_loop.before_loop
async def before_poll():
    await client.wait_until_ready()


# ══════════════════════════════════════════════════════════════════════════════
# Slash commands
# ══════════════════════════════════════════════════════════════════════════════

@tree.command(name="add-search", description="Add a new Yahoo Auctions search")
@app_commands.describe(
    label       = "Friendly name for this search (e.g. 'S13 front bumper')",
    url         = "Yahoo Auctions search URL — set filters in browser, then paste the URL here",
    channel     = "Which channel should new listings be posted to?",
    max_price   = "Maximum price in ¥ (optional)",
    min_price   = "Minimum price in ¥ (optional)",
    exclude     = "Skip listings whose title contains any of these — comma separated (e.g. ジャンク,破損)",
    include_any = "Only keep listings whose title contains at least one of these — comma separated",
)
async def add_search(
    interaction: discord.Interaction,
    label:       str,
    url:         str,
    channel:     discord.TextChannel,
    max_price:   Optional[int] = None,
    min_price:   Optional[int] = None,
    exclude:     str = "",
    include_any: str = "",
):
    await interaction.response.defer(ephemeral=True)

    if any(s["label"].lower() == label.lower() for s in searches):
        await interaction.followup.send(
            f'❌ A search called **{label}** already exists. Use `/remove-search` first.', ephemeral=True)
        return

    entry = {
        "label":       label,
        "url":         url,
        "channel_id":  channel.id,
        "min_price":   min_price,
        "max_price":   max_price,
        "exclude":     [w.strip() for w in exclude.split(",") if w.strip()],
        "include_any": [w.strip() for w in include_any.split(",") if w.strip()],
        "include_all": [],
    }
    searches.append(entry)

    if await save_criteria():
        lines = [f"📺 Posts to: {channel.mention}"]
        if max_price: lines.append(f"💰 Max price: ¥{max_price:,}")
        if min_price: lines.append(f"💰 Min price: ¥{min_price:,}")
        if exclude:   lines.append(f"🚫 Exclude: `{exclude}`")
        if include_any: lines.append(f"✅ Include any: `{include_any}`")
        await interaction.followup.send(
            f'✅ Added **{label}**\n' + "\n".join(lines), ephemeral=True)
    else:
        searches.pop()
        await interaction.followup.send(
            '❌ Failed to save to GitHub. Check that GITHUB_TOKEN has `repo` write access.', ephemeral=True)


@tree.command(name="remove-search", description="Remove a search by its label")
@app_commands.describe(label="Label of the search to remove")
async def remove_search(interaction: discord.Interaction, label: str):
    await interaction.response.defer(ephemeral=True)
    before = len(searches)
    searches[:] = [s for s in searches if s["label"].lower() != label.lower()]
    if len(searches) == before:
        await interaction.followup.send(f'❌ No search called **{label}** found. Check `/list-searches`.', ephemeral=True)
        return
    await save_criteria()
    await interaction.followup.send(f'✅ Removed **{label}**.', ephemeral=True)


@tree.command(name="list-searches", description="Show all active searches and their filters")
async def list_searches(interaction: discord.Interaction):
    if not searches:
        await interaction.response.send_message(
            "No searches configured yet. Use `/add-search` to add one.", ephemeral=True)
        return

    embed = discord.Embed(title="🔍 Active Searches", color=0x3498db)
    for s in searches:
        cid   = s.get("channel_id")
        lines = [f"Channel: <#{cid}>" if cid else "⚠️ No channel set"]
        if s.get("max_price"): lines.append(f"Max: ¥{s['max_price']:,}")
        if s.get("min_price"): lines.append(f"Min: ¥{s['min_price']:,}")
        if s.get("exclude"):   lines.append(f"Exclude: {', '.join(s['exclude'])}")
        if s.get("include_any"): lines.append(f"Include any: {', '.join(s['include_any'])}")
        embed.add_field(name=s["label"], value="\n".join(lines), inline=False)

    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="check-now", description="Trigger an immediate poll right now")
async def check_now(interaction: discord.Interaction):
    count = len(searches)
    await interaction.response.send_message(
        f"🔍 Checking {count} search{'es' if count != 1 else ''} now — "
        "new listings will appear in their channels shortly.", ephemeral=True)
    asyncio.create_task(do_poll())


# ══════════════════════════════════════════════════════════════════════════════
# Startup
# ══════════════════════════════════════════════════════════════════════════════

@client.event
async def on_ready():
    print(f"Logged in as {client.user} ({client.user.id})")
    await load_criteria()
    print(f"Loaded {len(searches)} search(es) from GitHub")
    await tree.sync()
    print("Slash commands synced globally (may take up to 1 hour to appear in Discord)")
    if not poll_loop.is_running():
        poll_loop.start()
        print(f"Poll loop started — checking every {POLL_MINUTES} minutes")


if __name__ == "__main__":
    client.run(BOT_TOKEN)
