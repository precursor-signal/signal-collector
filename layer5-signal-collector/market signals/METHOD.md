# Layer 5 - Market Signals: daily feed routine (method)

Status: method agreed 2026-10-10. Built: 26 sector folders, each with `<sector>-rss-feeds.csv` (3 always-on + 35 rotating feeds) and, after the first run, `<sector>-market-signals.xlsx` (one sheet per day).
Folder layout: `news/` holds the headline script, the 26 sector folders and `run-log.csv`; `movers/` holds the market-movers script, its workbook, ticker-sector map and log.
Script: `news/layer5_market_signals.py` (all sectors). Run log: `news/run-log.csv`. Scheduled by GitHub Actions at 4:00 PM ET (see `.github/workflows`).

## Purpose
Catch the news headlines that move stocks, ETFs and private-company valuations in each of the 26 sectors, every day of the week,
without sending the hundreds of queries per sector that the weekly collector profiles need (which tripped Google's 503 block).

## The routine
1. Each sector has **35 rotating feeds**: 7 weekday groups (Monday to Sunday) of 5 feeds. Each day only that weekday's 5 run.
2. Each sector also has a small set of **always-on feeds** that run every day, so news about major players is never missed
   (for example pure-play company names, tickers/ETFs, big private names). Number of always-on feeds: to be decided.
3. A feed is a Google News RSS search URL:
   `https://news.google.com/rss/search?q=<query>&hl=en-US&gl=US&ceid=US:en`
   with `when:1d` so it returns only the last 24 hours.
4. The daily run reads each feed, keeps headlines from the last 24 hours, drops junk, de-duplicates, tags the sector and feed,
   and appends each headline with its publish date to that sector's daily record.
5. Seven days a week, because news happens at weekends too.
6. A later step checks the headlines against sector words, opens the links and writes notes (as originally described by the owner).

## Feed design rules (learned from the space-sector test, 2026-10-10)
- One feed = one rich combination of sector-specific words (names, tickers, events, funding, contracts, regulators), not a copy of another feed.
- Keep every query to 180 characters or less (about 10 OR terms at most). Measured 2026-10-10: above roughly 200 characters / 11 OR terms Google silently ignores `when:1d` and returns old stories (all 38 space feeds passed after the cap was applied; 24 of 38 failed before).
- Google returns at most 100 items per search feed. If a feed returns 100 in 24 hours it is truncated: split it into smaller feeds.
- Do NOT use exclusion operators such as `-"Stock Price, News, Quote"`: they silently disabled `when:1d`. Filter junk after fetching.
- ALWAYS validate the window: if the oldest item in a feed is older than 24 hours, flag the feed (the window was ignored) and discard the stale items.
- Junk to drop after fetching: Yahoo quote pages ("Stock Price, News, Quote & History"), option contract pages, price-prediction pages.
- Duplicates are common (same story from several outlets): de-duplicate on normalised headline.
- Feed items have headline, outlet, date and a Google redirect link only; there is no summary.

## Article links and catalyst hints (added 2026-10-12)
- After the feeds are fetched, `news/layer5_market_signals.py` resolves every Google News redirect link to the real publisher URL
  (shared module `gnews_resolve.py`, four workers, 45-minute budget, stops early on repeated 429/503). The URL is stored in the
  `article_url` column (column I) of the day's sheet; `source_link` keeps the Google link. A blank `article_url` means it could not be
  resolved. Counts per sector go to `news/resolve-log.csv`. `--no-resolve` skips the step; `--resolve-only` re-runs it on today's sheets.
- `movers/layer5_market_movers.py` adds `catalyst_hint_1..3` to every mover row: the newest Google News headlines of the last
  two days that name the company or its ticker, as `published_et | outlet | headline | url` (real article URL when resolved).
  A hint is a lead only. Many hints just report the price move; none is verified as the cause. `--no-hints` skips the step.

## Rate-limit approach (proposal, not tested on GitHub IPs)
- Volume: 5 rotating + N always-on feeds per sector per day, about 130 to 200 requests per day across 26 sectors, versus 250 to 700 per sector for the weekly profiles.
- Space requests out with random gaps (seconds), one sector per job, staggered start times.
- Wait out a block (cooldown) and retry instead of falling back to Bing, which returned nothing.
- Observed on 2026-10-09: blocks are not a fixed count or rate; a job was blocked after about 555 queries while another passed 740 at the same pace.

## Record kept per headline
sector, feed id, query, headline, outlet, publish date/time (ET), Google News link, resolved article URL (when opened), duplicate group, junk flag, window-valid flag.

## Known limits
- A rotating feed sees only its own weekday's last 24 hours; a story that matches its words on another weekday is only caught by the always-on feeds or by another feed.
- Google's terms describe RSS feeds as for personal, non-commercial feed-reader use; automated use is a grey area.
- Mover screeners (stockanalysis, Yahoo, MarketBeat) show only the current day; they are separate from this routine.

## Open items for the owner
- Folder structure under this folder.
- Number and content of always-on feeds per sector.
- Which sector's 35 feeds to write first (space was the test sector; 7 test feeds exist in `../_feed_test/space_feeds_urls.txt`).
- Where daily headline records are stored and in what format.
- Day boundary: 24 hours back from the run time, or the calendar day in Eastern time.

## Feed vocabulary (rewritten 2026-10-10)
Feeds are built from the words that appear most in each sector's Layer 5 research history (hf-signal-framework wiki, 1,827 files, 11 weekly runs):
- sector-specific words only (3x+ more frequent than in other sectors, generic report words removed), grouped into themes by which words share the same research files;
- each theme is crossed with one intent group (market moves, deals/contracts, analyst actions, earnings/guidance, capital/financing, growth/expansion, policy);
- company, private-company, ETF and ticker feeds use the config names ordered by research frequency; two feeds per sector use Layer 5 phrase groups.
Tested on 5 sectors (space, power-energy, defense, ai-chips plus a sample of the rest): no feed ignored the 24-hour window; layer-5 phrase feeds return few items.
