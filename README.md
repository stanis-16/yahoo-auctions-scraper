# Yahoo Auctions Japan Monitor

A rapid prototype of a private Discord bot that scrapes Yahoo Auctions for new listings and sends alerts to a Discord channel. I built this as copy pasting searches in Japanese was tedious.

I reverse-engineered the yahoo auction URL's so search criteria could be easily manipulated. This also allowed the use of hidden search options not available on the UI.

Stack: Python, discord.py, aiohttp, BeautifulSoup, GitHub REST API, Railway



## What it does

* **Category alerts.** Each saved search posts to its own Discord channel with image, title, current and buy-it-now price, bids, and time left in English.
* **Buy-it-now channel.** Searches can carry a BIN price cap and are checked every 10 minutes (standard searches every 30), since BIN deals go quickly.
* **Watchlist.** A button on each alert adds the item to a watchlist sorted by time remaining, with an ending-soon channel for items under 3 hours left.
* **Managed from Discord.** Paste a search URL into a channel to add it, then use `/manage` to enable, disable, or delete searches. No code edits needed.
* **Owner-only access** via a Discord user ID allowlist.

## Notes

* **No official API.** No official Yahoo Auctions API available, so the bot parses public search pages and carries every filter in the URL. Pasted URLs are normalised (noise parameters stripped, newest-first sort and page size forced, shipping defaults added) while the user's own filters are preserved.
* **Server-side BIN filtering.** I diffed filtered and unfiltered URLs to find Yahoo's buy-it-now parameters, so the BIN feed is filtered by Yahoo rather than guessed from page layout.
* **Independent dedupe pools.** The 10-minute BIN loop and 30-minute standard loop each keep their own seen-ID set. A shared set would let whichever loop ran first swallow listings meant for the other.
* **Smart pagination.** Normally one page per poll. If a whole page is new, it walks further pages until it reaches an already-seen item, capped at 5 pages, so a burst of listings is not missed.
* **No alert floods.** Adding a search silently baselines everything currently listed.
* **Stateless hosting.** Searches, "seen" listing ID's and the watchlist persist as JSON, so the bot survives redeploys without a database.



## Run it

Needs a Discord bot token (with Message Content Intent on), a GitHub token with `repo` scope, and these environment variables: `DISCORD\_BOT\_TOKEN`, `GITHUB\_TOKEN`, `GITHUB\_REPO`, `OWNER\_IDS`, `ADD\_CRITERIA\_CHANNEL\_ID`, `WATCHLIST\_CHANNEL\_ID`, and optionally `BIN\_CHANNEL\_ID` and `ENDING\_SOON\_CHANNEL\_ID`. Deploy as a worker with `python bot.py` (see `Procfile`).

`monitor.py` and `monitor.yml` are the original GitHub Actions prototype and are no longer used.

