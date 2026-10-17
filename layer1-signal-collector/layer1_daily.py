#!/usr/bin/env python3
"""Layer 1 DAILY collector - physics & engineering reality signals. Capture only, no judgement.

Two layers of capture, same mechanism as Layer 2/3/4:
  WHOLESALE  every arXiv entry (by tracked category), HackerNews hit, GitHub/Hugging Face result,
             SEC full-text hit and feed item that is reachable is stored with its date, source and
             link before any filter;
  KEYWORD    every stored item's title+summary/description text is then analysed with the Layer 1
             vocabulary and combination rules (config/keywords.json): a research/academic marker +
             a tracked bottleneck term, an engineer-community marker + a bottleneck term, a
             conference term, a patent marker, a government R&D marker (excluding Layer 3's
             procurement wording), a manufacturing-partnership term, a lab/university spinout
             carrying a research signal, customer-usage growth wording.

Modules
  arxiv    export.arxiv.org Atom API, one wholesale query per tracked category
           (config/arxiv-categories.csv), newest-submitted first - then tagged by the engine.
  hn       hn.algolia.com search_by_date on the Layer 1 topic queries (config/keywords.json
           hn_queries), newest first.
  github   api.github.com/search/repositories on the adoption-velocity queries
           (config/keywords.json adoption_queries). Unauthenticated: ~60 req/hr - kept small.
  hf       huggingface.co/api/models, same query list, sorted by downloads.
  patents  USPTO Open Data Portal (api.uspto.gov), authenticated via env var USPTO_ODP_API_KEY
           (registered and wired in 2026-10-17) - dependable, no rate-limit/bot-block seen in
           testing. Falls back to patents.google.com/xhr/query (undocumented) only when no key
           is set. The Google path is CONFIRMED BOT-DETECTION-GATED after a handful of requests
           in short succession (tested 2026-10-17: HTTP 503 "your computer or network may be
           sending automated queries") and is kept as a best-effort, tightly budgeted module
           (default 3 queries/run, aborts the rest of the module on the first 503) rather than a
           dependable source.
  feeds    trade-press RSS + rotated per-sector Google News queries, stored whole, tagged,
           de-duplicated. Short items with a lead are opened and read (same lead-reading rule as
           Layer 2's feeds module).
  fts      SEC full-text search on the Layer 1 high-yield phrases (gov R&D grants, manufacturing
           partnerships, patent portfolios, university spinouts) - secondary/corroboration only,
           not the primary source the way it is for Layer 2/3/4.

Output (relative to data/daily/):
  arxiv/<YYYY-MM-DD>.jsonl, hn/<YYYY-MM-DD>.jsonl, github/<YYYY-MM-DD>.jsonl, hf/<YYYY-MM-DD>.jsonl,
  patents/<YYYY-MM-DD>.jsonl, feeds/<YYYY-MM-DD>.jsonl, fts/<YYYY-MM-DD>.jsonl
  health/<YYYY-MM-DD>.json      counts and failures (what did NOT work today)
Run log: data/run-log.csv

Usage
  python layer1_daily.py                          # all modules
  python layer1_daily.py --only arxiv,hn
  python layer1_daily.py --only feeds --fresh
  python layer1_daily.py --only patents --patents-budget 2
"""
import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import time

import l1lib as L

DAILY = os.path.join(L.DATA, "daily")
BODY_MAX = 1_200_000
BODY_CATEGORIES = ("trade", "gnews")


def row_from_analysis(base, an, th):
    gh = an["group_hits"]
    base.update({
        "families": "; ".join(an["families"]),
        "combos": "; ".join(an["combos"]),
        "score": an["score"],
        "bottleneck_terms": "; ".join(gh.get("bottleneck_terms", [])[:8]),
        "evidence": " || ".join(an["evidence"]),
        "group_hits": "; ".join(sorted(gh)),
        "kept": an["score"] >= th.get("keep_min_score", 3),
    })
    return base


def gate_relevance(row):
    """A keyword match only counts when the item also ties to a tracked sector, ticker or
    private-watchlist company. Used for feeds/fts (news/filings are almost always ABOUT a named
    company, so ungated text is mostly noise). Deliberately NOT applied to arxiv/hn/github/hf:
    those sources are topic-based, pre-company signals by design - Layer 1's whole premise is
    catching a bottleneck before it is tied to any tracked ticker, so gating them on today's
    tracked universe would filter out exactly the earliest, not-yet-mapped-to-a-company signals
    routine 01's agents exist to map. (Found during the 2026-10-17 test run: arxiv_kept went
    0 -> real candidates once this gate was removed from that module - see README known limits.)"""
    if row.get("kept") and not (row.get("sector_hits") or row.get("tickers") or row.get("private_matches")):
        row["kept"] = False
        row["skipped"] = "no sector, ticker or private-watchlist relevance"
    return row


def tag_bottleneck_topic(engine, text, an, base_weight=3):
    """For topic-based sources (arxiv/hn/github/hf): a bottleneck term match is itself the
    signal, independent of the generic sentence-combo engine's research_markers requirement
    (which is tuned for news prose saying 'a new paper shows...', not for an abstract that IS
    the paper). If bottleneck_terms hit and no combo already fired, add research_cluster at a
    baseline weight so the item is not silently dropped just because it lacks that phrasing."""
    gh = engine.groups_hit(text)
    bn = gh.get("bottleneck_terms", [])
    if bn and not an["families"]:
        an["families"] = ["research_cluster"]
        an["score"] = max(an["score"], base_weight + min(2, len(bn) - 1))
        an["group_hits"] = gh
    return an


# =============================================================================== arXiv


def run_arxiv(engine, universe, private, kw, health, date, max_results=25):
    cats = L.load_arxiv_categories()
    if not cats:
        health["arxiv_error"] = "config/arxiv-categories.csv missing"
        return
    seen_path = os.path.join(DAILY, "arxiv", "_seen.json")
    seen = set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "arxiv", date.isoformat() + ".jsonl")
    rows = L.read_jsonl(day_path)
    stats = {}
    for row in cats:
        cat = row["category"]
        st, entries, err = L.arxiv_search(None, categories=[cat], max_results=max_results)
        if entries is None:
            stats[cat] = {"status": st, "error": err}
            continue
        new = 0
        for e in entries:
            k = L.item_key(e["title"], e["id"])
            if k in seen:
                continue
            seen.add(k)
            text = e["title"] + ". " + e["summary"]
            an = tag_bottleneck_topic(engine, text, engine.analyse(text))
            r = {
                "key": k, "captured_et": L.now_et().isoformat(timespec="seconds"), "source_type": "arxiv",
                "category": cat, "category_label": row.get("label", ""), "arxiv_id": e["id"], "title": e["title"],
                "summary": e["summary"][:800], "authors": "; ".join(e["authors"][:6]), "categories": "; ".join(e["categories"]),
                "published": e["published"][:10], "updated": e["updated"][:10], "link": e["link"],
                "sector_hits": "; ".join(s for s, _n in engine.sector_hits(text)[:3]),
                "tickers": "; ".join(universe.match_tickers_in_text(text)),
                "private_matches": "; ".join(c for c, _s in private.match(text)[:4]),
            }
            row_from_analysis(r, an, kw.get("thresholds", {}))
            rows.append(r)
            new += 1
        stats[cat] = {"status": st, "entries": len(entries), "new": new}
    L.write_jsonl(day_path, rows)
    L.save_json(seen_path, list(seen)[-40000:])
    health["arxiv"] = stats
    health["arxiv_rows_today"] = len(rows)
    health["arxiv_kept"] = sum(1 for r in rows if r.get("kept"))


# =============================================================================== HackerNews


def run_hn(engine, universe, private, kw, health, date):
    queries = kw.get("hn_queries", [])
    min_points = kw.get("thresholds", {}).get("hn_min_points", 60)
    seen_path = os.path.join(DAILY, "hn", "_seen.json")
    seen = set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "hn", date.isoformat() + ".jsonl")
    rows = L.read_jsonl(day_path)
    stats = {}
    for q in queries:
        st, hits, err = L.hn_search(q, min_points=0, hits_per_page=30)
        if hits is None:
            stats[q] = {"status": st, "error": err}
            continue
        new = 0
        for h in hits:
            k = h.get("objectID") or L.item_key(h.get("title"), h.get("url"))
            if k in seen:
                continue
            seen.add(k)
            title = h.get("title") or h.get("story_title") or ""
            url = h.get("url") or h.get("story_url") or f"https://news.ycombinator.com/item?id={h.get('objectID')}"
            points = h.get("points") or 0
            text = title
            an = tag_bottleneck_topic(engine, text, engine.analyse(text + " . hacker news discussion"))
            r = {
                "key": k, "captured_et": L.now_et().isoformat(timespec="seconds"), "source_type": "hn",
                "query": q, "title": title, "url": url, "points": points, "num_comments": h.get("num_comments") or 0,
                "created_at": h.get("created_at", ""), "author": h.get("author", ""),
                "sector_hits": "; ".join(s for s, _n in engine.sector_hits(title)[:3]),
                "tickers": "; ".join(universe.match_tickers_in_text(title)),
                "private_matches": "; ".join(c for c, _s in private.match(title)[:4]),
                "above_points_threshold": points >= min_points,
            }
            row_from_analysis(r, an, kw.get("thresholds", {}))
            if r["kept"] and points < min_points:
                r["kept"] = False
                r["skipped"] = f"below hn_min_points threshold ({points} < {min_points})"
            rows.append(r)
            new += 1
        stats[q] = {"status": st, "hits": len(hits), "new": new}
    L.write_jsonl(day_path, rows)
    L.save_json(seen_path, list(seen)[-40000:])
    health["hn"] = stats
    health["hn_rows_today"] = len(rows)
    health["hn_kept"] = sum(1 for r in rows if r.get("kept"))


# =============================================================================== GitHub / Hugging Face


def run_github(engine, universe, private, kw, health, date):
    queries = kw.get("adoption_queries", [])
    seen_path = os.path.join(DAILY, "github", "_seen.json")
    seen = set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "github", date.isoformat() + ".jsonl")
    rows = L.read_jsonl(day_path)
    stats = {}
    for q in queries[:8]:  # unauthenticated rate limit (~60/hr) - stay well under it
        st, items, err = L.github_search_repos(q, sort="updated", order="desc", per_page=10)
        if items is None:
            stats[q] = {"status": st, "error": err}
            continue
        new = 0
        for it in items:
            k = str(it.get("id"))
            if k in seen:
                continue
            seen.add(k)
            desc = it.get("description") or ""
            text = it["full_name"] + ". " + desc
            an = tag_bottleneck_topic(engine, text, engine.analyse(text))
            r = {
                "key": k, "captured_et": L.now_et().isoformat(timespec="seconds"), "source_type": "github",
                "query": q, "repo": it["full_name"], "description": desc[:400], "stars": it.get("stargazers_count", 0),
                "created_at": (it.get("created_at") or "")[:10], "pushed_at": (it.get("pushed_at") or "")[:10],
                "url": it.get("html_url", ""),
                "sector_hits": "; ".join(s for s, _n in engine.sector_hits(text)[:3]),
                "tickers": "; ".join(universe.match_tickers_in_text(text)),
                "private_matches": "; ".join(c for c, _s in private.match(text)[:4]),
            }
            row_from_analysis(r, an, kw.get("thresholds", {}))
            if it.get("stargazers_count", 0) >= 50:
                r["families"] = (r["families"] + "; adoption_signal").strip("; ")
                r["kept"] = True if (r.get("sector_hits") or r.get("tickers") or r.get("private_matches")) else r["kept"]
            rows.append(r)
            new += 1
        stats[q] = {"status": st, "items": len(items), "new": new}
    L.write_jsonl(day_path, rows)
    L.save_json(seen_path, list(seen)[-40000:])
    health["github"] = stats
    health["github_rows_today"] = len(rows)


def run_hf(engine, universe, private, kw, health, date):
    queries = kw.get("adoption_queries", [])
    seen_path = os.path.join(DAILY, "hf", "_seen.json")
    seen = set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "hf", date.isoformat() + ".jsonl")
    rows = L.read_jsonl(day_path)
    stats = {}
    for q in queries:
        st, items, err = L.hf_search_models(q, sort="downloads", limit=10)
        if items is None:
            stats[q] = {"status": st, "error": err}
            continue
        new = 0
        for it in items:
            k = it.get("id") or it.get("modelId", "")
            if not k or k in seen:
                continue
            seen.add(k)
            text = k
            an = tag_bottleneck_topic(engine, text, engine.analyse(text))
            r = {
                "key": k, "captured_et": L.now_et().isoformat(timespec="seconds"), "source_type": "huggingface",
                "query": q, "model_id": k, "downloads": it.get("downloads", 0), "likes": it.get("likes", 0),
                "last_modified": (it.get("lastModified") or "")[:10], "url": "https://huggingface.co/" + k,
                "sector_hits": "; ".join(s for s, _n in engine.sector_hits(text)[:3]),
                "tickers": "; ".join(universe.match_tickers_in_text(text)),
                "private_matches": "; ".join(c for c, _s in private.match(text)[:4]),
            }
            row_from_analysis(r, an, kw.get("thresholds", {}))
            if it.get("downloads", 0) >= 10000:
                r["families"] = (r["families"] + "; adoption_signal").strip("; ")
            rows.append(r)
            new += 1
        stats[q] = {"status": st, "items": len(items), "new": new}
    L.write_jsonl(day_path, rows)
    L.save_json(seen_path, list(seen)[-40000:])
    health["hf"] = stats
    health["hf_rows_today"] = len(rows)


# =============================================================================== Patents (best-effort)


def _odp_row(q, item, engine, universe, private, kw):
    md = item.get("applicationMetaData", {}) or {}
    title = md.get("inventionTitle", "")
    assignee = md.get("firstApplicantName") or (md.get("firstInventorName") or "")
    text = title + ". " + assignee
    an = tag_bottleneck_topic(engine, text, engine.analyse(text))
    r = {
        "captured_et": L.now_et().isoformat(timespec="seconds"), "source_type": "patents-odp",
        "query": q, "publication_number": item.get("applicationNumberText", ""),
        "filing_date": md.get("filingDate", ""), "publication_date": md.get("applicationStatusDate", ""),
        "assignee": assignee, "title": title, "snippet": "",
        "sector_hits": "; ".join(s for s, _n in engine.sector_hits(text)[:3]),
        "tickers": "; ".join(universe.match_tickers_in_text(text)),
        "private_matches": "; ".join(c for c, _s in private.match(text)[:4]),
    }
    row_from_analysis(r, an, kw.get("thresholds", {}))
    return r


def _google_patents_row(q, p, engine, universe, private, kw):
    text = (p.get("title") or "") + ". " + (p.get("snippet") or "")
    an = tag_bottleneck_topic(engine, text, engine.analyse(text))
    r = {
        "captured_et": L.now_et().isoformat(timespec="seconds"), "source_type": "patents-best-effort",
        "query": q, "publication_number": p.get("publication_number", ""), "filing_date": p.get("filing_date", ""),
        "publication_date": p.get("publication_date", ""), "assignee": p.get("assignee", ""), "title": p.get("title", ""),
        "snippet": (p.get("snippet") or "")[:400],
        "sector_hits": "; ".join(s for s, _n in engine.sector_hits(text)[:3]),
        "tickers": "; ".join(universe.match_tickers_in_text(text)),
        "private_matches": "; ".join(c for c, _s in private.match(text)[:4]),
    }
    row_from_analysis(r, an, kw.get("thresholds", {}))
    return r


def run_patents(engine, universe, private, kw, health, date, budget=3):
    """USPTO Open Data Portal (api.uspto.gov) is the primary source whenever USPTO_ODP_API_KEY
    is set (registered 2026-10-17: authenticated, no rate-limit/bot-block seen in testing).
    Falls back to the best-effort patents.google.com scrape - see the module docstring at the
    top of this file - only when no key is configured. The Google path still aborts the whole
    module on the first bot-detection 503, same as before."""
    queries = ["silicon photonics", "CXL controller", "immersion cooling", "neuromorphic chip",
               "wafer-scale interconnect", "cryogenic memory"]
    day_path = os.path.join(DAILY, "patents", date.isoformat() + ".jsonl")
    rows = L.read_jsonl(day_path)
    stats = {}
    api_key = os.environ.get("USPTO_ODP_API_KEY")

    if api_key:
        for q in queries[:budget]:
            st, j, err = L.uspto_odp_search(q, api_key=api_key, limit=10)
            if j is None:
                stats[q] = {"status": st, "error": err}
                continue
            items = j.get("patentFileWrapperDataBag", []) or []
            new = 0
            for item in items:
                rows.append(_odp_row(q, item, engine, universe, private, kw))
                new += 1
            stats[q] = {"status": st, "count": j.get("count"), "items": len(items), "new": new}
        health["patents_source"] = "uspto_odp"
    else:
        blocked = False
        for q in queries[:budget]:
            if blocked:
                stats[q] = {"skipped": "aborted after a bot-detection 503 earlier this run"}
                continue
            st, items, err = L.google_patents_search("q=" + q.replace(" ", "+"), sort="new")
            if items is None:
                stats[q] = {"status": st, "error": err}
                if st == 503:
                    blocked = True
                    health["patents_bot_blocked"] = True
                continue
            new = 0
            for p in items[:10]:
                rows.append(_google_patents_row(q, p, engine, universe, private, kw))
                new += 1
            stats[q] = {"status": st, "items": len(items), "new": new}
        health["patents_source"] = "google_patents_best_effort"

    L.write_jsonl(day_path, rows)
    health["patents"] = stats
    health["patents_rows_today"] = len(rows)
    health["patents_note"] = ("dependable module via USPTO ODP" if api_key else
        "best-effort module - patents.google.com/xhr/query is bot-detection-gated; routine 03 / "
        "agents are primary patent coverage until USPTO_ODP_API_KEY is set")


# =============================================================================== feeds


def run_feeds(engine, universe, private, kw, health, date, google_window, fetch_bodies=40, fresh=False):
    path = os.path.join(L.CONFIG, "feeds.csv")
    if not os.path.exists(path):
        health["feeds_error"] = "config/feeds.csv missing - run tools/build_feeds.py"
        return
    allf = L.read_csv(path)
    feeds = [f for f in allf if (f.get("enabled") or "yes").lower() == "yes"]
    disabled = [f for f in allf if (f.get("enabled") or "yes").lower() != "yes"]
    th = kw.get("thresholds", {})
    seen_path = os.path.join(DAILY, "feeds", "_seen.json")
    seen = set() if fresh else set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "feeds", date.isoformat() + ".jsonl")
    day_items = [] if fresh else L.read_jsonl(day_path)
    fp = kw["feed_params"]
    stats, g_fail = {}, 0
    for fd in feeds:
        url, ftype = fd["url"], (fd.get("type") or "rss").lower()
        if ftype == "gnews":
            if g_fail >= 3:
                stats[fd["feed_id"]] = {"skipped": "google news blocking (3 consecutive failures)"}
                continue
            url = url.replace("when:3d", "when:" + google_window)
            gap = random.uniform(fp["gap_seconds_min"], fp["gap_seconds_max"])
        else:
            gap = 0.8
        st, body, ct, err = L.fetch(url, gap=gap, timeout=40)
        if body is None:
            stats[fd["feed_id"]] = {"status": st, "error": err}
            g_fail += 1 if ftype == "gnews" else 0
            continue
        if ftype == "gnews":
            g_fail = 0
        if ftype == "html-archive":
            text_all = L.strip_html(body.decode("utf-8", "ignore"))
            links = re.findall(r'href="(https://newsletter\.semianalysis\.com/p/[^"]+)"', body.decode("utf-8", "ignore"))
            items = [{"title": "", "link": lk, "published": "", "published_iso": "", "summary": "", "source": fd["name"]} for lk in list(dict.fromkeys(links))[:30]]
        else:
            items = L.parse_feed(body)
            if items is None:
                stats[fd["feed_id"]] = {"status": st, "error": "not parseable as RSS/Atom (interstitial?)"}
                continue
        stale = 0
        if ftype == "gnews":
            hours = {"1d": 24, "3d": 72, "7d": 168, "14d": 336}.get(google_window, 72) + 6
            cutoff = L.now_et() - dt.timedelta(hours=hours)
            kept = []
            for it in items:
                if it["published_iso"]:
                    try:
                        if dt.datetime.fromisoformat(it["published_iso"]) < cutoff:
                            stale += 1
                            continue
                    except ValueError:
                        pass
                kept.append(it)
            items = kept
        new = 0
        for it in items:
            k = L.item_key(it["title"], it["link"])
            if k in seen:
                continue
            seen.add(k)
            text = it["title"] + " . " + it["summary"]
            noise = engine.is_noise(text)
            an = engine.analyse(text)
            row = {"key": k, "captured_et": L.now_et().isoformat(timespec="seconds"), "source_type": "feed",
                   "feed_id": fd["feed_id"], "feed_name": fd["name"], "category": fd["category"], "title": it["title"],
                   "link": it["link"], "published_et": it["published_iso"], "source": it["source"], "summary": it["summary"][:500],
                   "tickers": "; ".join(universe.match_tickers_in_text(text)),
                   "sector_hits": "; ".join(s for s, _n in engine.sector_hits(text)[:3]),
                   "private_matches": "; ".join(c for c, _s in private.match(text)[:3]),
                   "noise": noise, "body_read": False}
            row_from_analysis(row, an, th)
            if row.get("kept"):
                gate_relevance(row)
            if row.get("noise"):
                row["kept"] = False
            day_items.append(row)
            new += 1
        stats[fd["feed_id"]] = {"status": st, "items": len(items), "new": new, **({"stale_dropped": stale} if stale else {})}

    opened = 0
    if fetch_bodies:
        def lead(r):
            gh = r.get("group_hits") or ""
            return any(g in gh for g in ("research_markers", "community_markers", "gov_rd_markers",
                                          "manufacturing_partnership_terms", "patent_markers", "spinout_terms",
                                          "bottleneck_terms", "conference_terms", "breakthrough_terms"))
        cand = [r for r in day_items if not r.get("body_read") and not r.get("noise") and r["score"] < th.get("keep_min_score", 3)
                and r["category"] in BODY_CATEGORIES and lead(r)]
        cand.sort(key=lambda r: -(bool(r.get("sector_hits")) + bool(r.get("tickers")) + bool(r.get("private_matches")) + random.random() * 0.5))
        for r in cand[:fetch_bodies]:
            st, body, ct, e = L.fetch(r["link"], max_bytes=600000, gap=0.8, timeout=25, retries=0)
            r["body_read"] = True
            opened += 1
            if not body:
                continue
            text = L.strip_html(body.decode("utf-8", "ignore"))[:25000]
            if len(text) < 300:
                continue
            an = engine.analyse(r["title"] + " . " + text)
            if an["score"] > r["score"]:
                row_from_analysis(r, an, th)
                if r.get("kept"):
                    gate_relevance(r)
                r["body_upgraded"] = True
    L.write_jsonl(day_path, day_items)
    L.save_json(seen_path, list(seen)[-80000:])
    health["feeds"] = stats
    health["feeds_disabled_agent_covered"] = [f"{f['feed_id']}: {f['notes']}" for f in disabled]
    health["feed_items_today"] = len(day_items)
    health["feed_bodies_opened"] = opened


# =============================================================================== SEC full-text (secondary)


def run_fts(engine, universe, private, kw, health, date, lookback_days=3):
    seen_path = os.path.join(DAILY, "fts", "_seen.json")
    seen = set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "fts", date.isoformat() + ".jsonl")
    rows = L.read_jsonl(day_path)
    start = (date - dt.timedelta(days=lookback_days)).isoformat()
    end = date.isoformat()
    stats = {}
    for q in kw.get("fts_queries", []):
        url = L.sec_fts_url(q, start, end)
        st, txt, err = L.fetch_text(url, gap=0.35)
        if not txt:
            stats[q] = {"status": st, "error": err}
            continue
        try:
            hits = json.loads(txt)["hits"]["hits"]
            total = json.loads(txt)["hits"]["total"]["value"]
        except Exception as e:
            stats[q] = {"status": st, "error": f"parse: {e}"}
            continue
        new = 0
        for h in hits:
            src = h.get("_source", {})
            fid = h.get("_id", "")
            if fid in seen:
                continue
            seen.add(fid)
            adsh = src.get("adsh") or (fid.split(":")[0])
            disp = (src.get("display_names") or [""])[0]
            row = {"captured_et": L.now_et().isoformat(timespec="seconds"), "query": q, "form": src.get("form", ""),
                   "filed": src.get("file_date", ""), "company": disp, "accession": adsh, "source_type": "sec-fts",
                   "score": 2, "families": "gov_rd_signal" if any(t in q.lower() for t in ("darpa", "chips", "nasa", "department of energy")) else "patent_signal",
                   "kept": True}
            rows.append(row)
            new += 1
        stats[q] = {"status": st, "total_hits": total, "returned": len(hits), "new": new}
    L.write_jsonl(day_path, rows)
    L.save_json(seen_path, list(seen)[-40000:])
    health["fts"] = stats
    health["fts_rows_today"] = len(rows)


# =============================================================================== main


def main():
    ap = argparse.ArgumentParser(description="Layer 1 daily collector")
    ap.add_argument("--date", help="ET date (default today ET)")
    ap.add_argument("--only", default="arxiv,hn,github,hf,feeds,fts")
    ap.add_argument("--max-minutes", type=float, default=60)
    ap.add_argument("--google-window", default="3d")
    ap.add_argument("--fetch-bodies", type=int, default=40)
    ap.add_argument("--patents-budget", type=int, default=3, help="patents module: max queries this run (best-effort, bot-gated)")
    ap.add_argument("--fresh", action="store_true")
    a = ap.parse_args()

    date = L.parse_date(a.date) if a.date else L.today_et()
    mods = {m.strip() for m in a.only.split(",")}
    started = time.time()
    health = {"run_date_et": date.isoformat(), "started_et": L.now_et().isoformat(timespec="seconds"), "modules": sorted(mods)}
    kw = L.load_keywords()
    engine = L.SignalEngine(kw)
    universe = L.Universe(fetch_tickers=False)
    private = L.PrivateMatcher()
    health["private_watchlist"] = len(private.items)
    health["tracked_tickers"] = len(universe.sectors_by_ticker)

    if "arxiv" in mods:
        run_arxiv(engine, universe, private, kw, health, date)
    if "hn" in mods:
        run_hn(engine, universe, private, kw, health, date)
    if "github" in mods:
        run_github(engine, universe, private, kw, health, date)
    if "hf" in mods:
        run_hf(engine, universe, private, kw, health, date)
    if "patents" in mods:
        run_patents(engine, universe, private, kw, health, date, budget=a.patents_budget)
    if "feeds" in mods:
        run_feeds(engine, universe, private, kw, health, date, a.google_window, fetch_bodies=a.fetch_bodies, fresh=a.fresh)
    if "fts" in mods:
        run_fts(engine, universe, private, kw, health, date)

    health["finished_et"] = L.now_et().isoformat(timespec="seconds")
    health["elapsed_min"] = round((time.time() - started) / 60, 1)
    L.save_json(os.path.join(DAILY, "health", date.isoformat() + ".json"), health)

    log = os.path.join(L.DATA, "run-log.csv")
    L.ensure_dir(L.DATA)
    new = not os.path.exists(log)
    with open(log, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["finished_et", "script", "date", "modules", "arxiv_kept", "hn_kept", "feed_items_today", "elapsed_min"])
        w.writerow([health["finished_et"], "daily", date.isoformat(), "+".join(sorted(mods)),
                    health.get("arxiv_kept", ""), health.get("hn_kept", ""), health.get("feed_items_today", ""), health["elapsed_min"]])
    brief = {k: v for k, v in health.items() if k not in ("feeds", "feeds_disabled_agent_covered", "fts", "patents")}
    print(json.dumps(brief, indent=1))


if __name__ == "__main__":
    main()
