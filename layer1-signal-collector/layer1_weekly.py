#!/usr/bin/env python3
"""Layer 1 WEEKLY packet builder - physics & engineering reality signals.

Reads the last 7 days of the daily collector's stored data and builds the packet the discovery
routines read. Output: data/weekly/<end-date>/  (window is Friday-Thursday; folder named for the
Thursday that closes it - same convention as Layer 2, which runs Thursday on a window named for
the Wednesday before it. Layer 1's build fires just after midnight Friday, so it is still
described as "Layer 1 runs on Friday" the same way L2/L3/L4 are each named for their own run day,
not their window's closing day: L4=Tuesday, L3=Wednesday, L2=Thursday, L1=Friday - the four do
not collide).

Steps
  1. consolidate the week's kept arxiv/hn/github/hf/patents/feeds/fts rows into de-duplicated
     signals, clustered by topic (bottleneck terms + sector), not by company - Layer 1 signals
     are frequently NOT yet tied to any tracked ticker (that mapping is routine 01/02's job)
  2. split by family: research_cluster, community_signal, conference_signal, patent_signal,
     gov_rd_signal, manufacturing_partnership_signal, spinout_signal, adoption_signal,
     usage_growth_signal
  3. topic convergence (same bottleneck term cluster appearing across arxiv + hn + feeds =
     a stronger signal than any one source alone) and company convergence (a tracked ticker or
     private-watchlist company with several signals this week and in the trailing 4 packets)
  4. conference-calendar window check (config/conference-calendar.csv) - which tracked
     conferences are in or near their window this week
  5. ETF overlap, status flags, continuity against the prior packet
  6. coverage ledger (what was NOT collected) + SUMMARY.md + packet.xlsx

Usage
  python layer1_weekly.py                  # window = the 7 days Friday-Thursday ending yesterday
  python layer1_weekly.py --end 2026-10-16
"""
import argparse
import csv
import datetime as dt
import os
import re
import time

import l1lib as L

DAILY = os.path.join(L.DATA, "daily")
WEEKLY = os.path.join(L.DATA, "weekly")
L4_ETF = os.path.normpath(os.path.join(L.ROOT, "..", "layer4-signal-collector", "data", "daily", "etf", "snapshots"))

MODULE_DIRS = ("arxiv", "hn", "github", "hf", "patents", "feeds", "fts")


def last_window_end(today):
    """The Thursday that closes the window: the latest Thursday on or before yesterday (a
    Friday run's 'yesterday' is already that Thursday). Window is Friday-Thursday, 7 days,
    named for the Thursday - same convention as Layer 2 (runs Thursday, window Thursday-
    Wednesday, named for Wednesday): the folder is named for the day BEFORE the run day, not
    the run day itself. (Caught 2026-10-17: the first version of this function targeted Friday
    directly, which for a Friday run returns the PRIOR Friday - 7 days too early - because
    'yesterday' on a Friday is Thursday, not Friday. Only surfaced once a real GitHub Actions
    schedule was being wired up; the manual --end-date tests before that had bypassed this
    function entirely.)"""
    d = today - dt.timedelta(days=1)
    while d.weekday() != 3:
        d -= dt.timedelta(days=1)
    return d


def window(end):
    return [end - dt.timedelta(days=i) for i in range(6, -1, -1)]


STOP = {"the", "and", "for", "with", "that", "from", "inc", "corp", "its", "has", "will", "into",
        "this", "are", "was", "were", "been", "have", "paper", "research", "signal"}


def toks(s):
    return set(w for w in re.findall(r"[a-z0-9-]{3,}", (s or "").lower())) - STOP


def jacc(a, b):
    return len(a & b) / max(1, len(a | b))


def pset(s):
    return {re.sub(r"[^a-z]", "", p.lower()) for p in (s or "").split("; ") if p}


# ----------------------------------------------------------------------------- load daily data


def load_window(win):
    days = {d.isoformat() for d in win}
    data = {m: [] for m in MODULE_DIRS}
    for m in MODULE_DIRS:
        root = os.path.join(DAILY, m)
        if os.path.isdir(root):
            for fn in sorted(os.listdir(root)):
                if fn.endswith(".jsonl") and fn[:-6] in days:
                    data[m] += L.read_jsonl(os.path.join(root, fn))
    return data


# ----------------------------------------------------------------------------- consolidate


def to_items(data):
    items = []
    for r in data["arxiv"]:
        if not r.get("kept"):
            continue
        items.append({"src": "arxiv", "date": r.get("published"), "topic_key": r.get("bottleneck_terms") or r.get("title", "")[:40],
                      "title": r.get("title", ""), "summary": r.get("summary", ""), "link": r.get("link"),
                      "tickers": r.get("tickers", ""), "sectors": r.get("sector_hits", ""), "private": r.get("private_matches", ""),
                      "r": r, "source_label": f"arXiv {r.get('category','')}"})
    for r in data["hn"]:
        if not r.get("kept"):
            continue
        items.append({"src": "hn", "date": (r.get("created_at") or "")[:10], "topic_key": r.get("bottleneck_terms") or r.get("title", "")[:40],
                      "title": r.get("title", ""), "summary": "", "link": r.get("url"),
                      "tickers": r.get("tickers", ""), "sectors": r.get("sector_hits", ""), "private": r.get("private_matches", ""),
                      "r": r, "source_label": f"HackerNews ({r.get('points',0)} pts)"})
    for r in data["github"]:
        if not r.get("kept") and "adoption_signal" not in (r.get("families") or ""):
            continue
        items.append({"src": "github", "date": (r.get("pushed_at") or "")[:10], "topic_key": r.get("repo", ""),
                      "title": r.get("repo", ""), "summary": r.get("description", ""), "link": r.get("url"),
                      "tickers": r.get("tickers", ""), "sectors": r.get("sector_hits", ""), "private": r.get("private_matches", ""),
                      "r": r, "source_label": f"GitHub ({r.get('stars',0)} stars)"})
    for r in data["hf"]:
        if not r.get("kept") and "adoption_signal" not in (r.get("families") or ""):
            continue
        items.append({"src": "huggingface", "date": (r.get("last_modified") or "")[:10], "topic_key": r.get("model_id", ""),
                      "title": r.get("model_id", ""), "summary": "", "link": r.get("url"),
                      "tickers": r.get("tickers", ""), "sectors": r.get("sector_hits", ""), "private": r.get("private_matches", ""),
                      "r": r, "source_label": f"Hugging Face ({r.get('downloads',0)} downloads)"})
    for r in data["patents"]:
        if not r.get("kept"):
            continue
        items.append({"src": "patents", "date": r.get("filing_date", ""), "topic_key": r.get("title", "")[:40],
                      "title": r.get("title", ""), "summary": r.get("snippet", ""), "link": "",
                      "tickers": r.get("tickers", ""), "sectors": r.get("sector_hits", ""), "private": r.get("private_matches", ""),
                      "r": r, "source_label": "Google Patents (best-effort)"})
    for r in data["feeds"]:
        if not r.get("kept"):
            continue
        items.append({"src": "feed", "date": (r.get("published_et") or r.get("captured_et") or "")[:10], "topic_key": r.get("bottleneck_terms") or r.get("title", "")[:40],
                      "title": r.get("title", ""), "summary": r.get("summary", ""), "link": r.get("link"),
                      "tickers": r.get("tickers", ""), "sectors": r.get("sector_hits", ""), "private": r.get("private_matches", ""),
                      "r": r, "source_label": f"{r.get('category')}:{r.get('feed_id')}"})
    for r in data["fts"]:
        if not r.get("kept"):
            continue
        items.append({"src": "sec-fts", "date": r.get("filed", ""), "topic_key": r.get("query", ""),
                      "title": f"{r.get('company')} {r.get('form')} - {r.get('query')}", "summary": "", "link": "",
                      "tickers": "", "sectors": "", "private": "", "r": r, "source_label": f"SEC full-text {r.get('form')}"})
    return items


def cluster(items):
    """Greedy clustering: same tracked entity (ticker/private co), or shared bottleneck-term
    topic words with high title overlap."""
    clusters = []
    for it in items:
        it["_tok"] = toks(it["title"] + " " + it.get("summary", "")[:200])
        it["_tick"] = pset(it.get("tickers"))
        it["_priv"] = pset(it.get("private"))
        placed = False
        for c in clusters:
            h = c["items"][0]
            same_entity = bool((it["_tick"] & h["_tick"]) or (it["_priv"] & h["_priv"]))
            if same_entity and jacc(it["_tok"], h["_tok"]) >= 0.15:
                c["items"].append(it)
                placed = True
                break
            if not it["_tick"] and not it["_priv"] and not h["_tick"] and not h["_priv"] and jacc(it["_tok"], h["_tok"]) >= 0.45:
                c["items"].append(it)
                placed = True
                break
        if not placed:
            clusters.append({"items": [it]})
    return clusters


def build_signals(clusters, end, th):
    rows = []
    for n, c in enumerate(clusters, 1):
        its = c["items"]
        best = max(its, key=lambda x: (x["r"].get("score") or 0))
        r = best["r"]
        dates = sorted(d for d in (it.get("date") for it in its) if d and re.match(r"^\d{4}-\d{2}-\d{2}$", d))
        first = dates[0] if dates else None
        age = (end - dt.date.fromisoformat(first)).days if first else None
        flags = []
        if age is not None and age > th.get("very_stale_days", 90):
            flags.append("stale-over-90d")
        elif age is not None and age > th.get("stale_days", 30):
            flags.append("stale-over-30d")
        if len({it["src"] for it in its}) == 1 and len(its) == 1:
            flags.append("single-source")
        tick, priv = set(), set()
        for it in its:
            tick |= set((it.get("tickers") or "").split("; ")) - {""}
            priv |= set((it.get("private") or "").split("; ")) - {""}
        if not tick and not priv:
            flags.append("entity-not-yet-mapped")  # the common Layer-1 case: a real topic signal with no company attached yet
        fams, combos, sectors = [], [], []
        for it in its:
            for key, bucket in (("families", fams), ("combos", combos), ("sector_hits", sectors)):
                for f in (it["r"].get(key) or "").split("; "):
                    if f and f not in bucket:
                        bucket.append(f)
        entity = (sorted(tick)[0] if tick else (sorted(priv)[0] if priv else best["title"][:60]))
        rows.append({
            "signal_id": f"{end.isoformat()}-L1-{n:04d}",
            "entity": entity, "tickers": "; ".join(sorted(tick)), "private_matches": "; ".join(sorted(priv)),
            "sectors": "; ".join(sectors),
            "families": "; ".join(fams), "combos": "; ".join(combos), "score": max((it["r"].get("score") or 0) for it in its),
            "title": best["title"][:200], "evidence": r.get("evidence", "") or r.get("summary", "")[:300],
            "first_seen": first or "", "age_days": age, "n_items": len(its),
            "source_types": "; ".join(sorted({it["src"] for it in its})),
            "sources": " | ".join(dict.fromkeys(it.get("link") for it in its if it.get("link")))[:900],
            "source_labels": "; ".join(dict.fromkeys(it["source_label"] for it in its))[:300],
            "flags": "; ".join(flags),
        })
    rows.sort(key=lambda x: (-int(x["score"] or 0), x["entity"]))
    return rows


# ----------------------------------------------------------------------------- derived datasets


def by_family(signals, wanted):
    return [s for s in signals if set((s["families"] or "").split("; ")) & wanted]


def topic_convergence(signals):
    """A topic (bottleneck term) corroborated by 2+ distinct source types this week is the
    strongest version of a Layer 1 signal: e.g. the same bottleneck shows up in an arXiv paper
    AND a HackerNews thread AND a trade-press article in the same week."""
    out = []
    for s in signals:
        srcs = set((s["source_types"] or "").split("; "))
        if len(srcs) >= 2:
            out.append({"entity": s["entity"], "sectors": s["sectors"], "families": s["families"],
                        "source_types": s["source_types"], "n_sources": len(srcs), "score": s["score"],
                        "evidence": s["evidence"]})
    out.sort(key=lambda x: (-x["n_sources"], -x["score"]))
    return out


def company_convergence(signals, end):
    cur = {}
    for s in signals:
        ent = (s["tickers"] or s["private_matches"] or "").split("; ")[0]
        if not ent:
            continue
        cur.setdefault(ent, {"entity": ent, "this_week": 0, "trailing": 0, "weeks_seen": set(), "fams": set()})
        e = cur[ent]
        e["this_week"] += 1
        e["fams"].update(f for f in (s["families"] or "").split("; ") if f)
        e["weeks_seen"].add(end.isoformat())
    prev_dirs = sorted(d for d in os.listdir(WEEKLY) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < end.isoformat()) if os.path.isdir(WEEKLY) else []
    for d in prev_dirs[-4:]:
        p = os.path.join(WEEKLY, d, "01_signals.csv")
        if os.path.exists(p):
            for r in L.read_csv(p):
                ent = (r.get("tickers") or r.get("private_matches") or "").split("; ")[0]
                if not ent:
                    continue
                cur.setdefault(ent, {"entity": ent, "this_week": 0, "trailing": 0, "weeks_seen": set(), "fams": set()})
                e = cur[ent]
                e["trailing"] += 1
                e["weeks_seen"].add(d)
                e["fams"].update(f for f in (r.get("families") or "").split("; ") if f)
    out = []
    for ent, e in cur.items():
        total = e["this_week"] + e["trailing"]
        if total >= 2:
            out.append({"entity": ent, "signals_this_week": e["this_week"], "signals_trailing_4_packets": e["trailing"],
                        "weeks_seen": len(e["weeks_seen"]), "distinct_families": "; ".join(sorted(e["fams"]))})
    out.sort(key=lambda x: (-x["signals_this_week"], -x["weeks_seen"]))
    return out


def conference_window(end):
    cal = L.load_conference_calendar()
    out = []
    for r in cal:
        try:
            ws, we = dt.date.fromisoformat(r["window_start"]), dt.date.fromisoformat(r["window_end"])
        except Exception:
            continue
        lead = ws - dt.timedelta(days=14)
        if lead <= end <= we:
            out.append({"conference": r["conference"], "topic_area": r["topic_area"], "window_start": r["window_start"],
                        "window_end": r["window_end"], "status": "in_window" if ws <= end <= we else "upcoming_within_14d"})
    return out


def etf_overlap(signals, health):
    if not os.path.isdir(L4_ETF):
        health["etf_overlap"] = {"collected": False, "reason": "Layer 4 ETF snapshots not found at " + L4_ETF}
        return []
    tick = set()
    for s in signals:
        tick |= set((s["tickers"] or "").split("; ")) - {""}
    out = []
    for etf in sorted(os.listdir(L4_ETF)):
        folder = os.path.join(L4_ETF, etf)
        files = sorted(f for f in os.listdir(folder) if f.endswith(".csv")) if os.path.isdir(folder) else []
        if not files:
            continue
        rows = L.read_csv(os.path.join(folder, files[-1]))
        held = {(r.get("ticker") or "").upper() for r in rows}
        ov = sorted(held & tick)
        if ov:
            out.append({"etf": etf, "snapshot": files[-1][:-4], "holdings": len(rows), "overlap_count": len(ov),
                        "overlap_tickers": "; ".join(ov)})
    out.sort(key=lambda x: -x["overlap_count"])
    health["etf_overlap"] = {"collected": True, "etfs_with_overlap": len(out)}
    return out


def continuity(signals, end):
    prev_dirs = sorted(d for d in os.listdir(WEEKLY) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < end.isoformat()) if os.path.isdir(WEEKLY) else []
    if not prev_dirs:
        return [], None
    p = os.path.join(WEEKLY, prev_dirs[-1], "01_signals.csv")
    if not os.path.exists(p):
        return [], prev_dirs[-1]
    cur = {(s["tickers"] or s["private_matches"] or s["entity"]) for s in signals}
    out = []
    for r in L.read_csv(p):
        k = r.get("tickers") or r.get("private_matches") or r.get("entity")
        if k and k not in cur:
            out.append({"entity": r.get("entity"), "last_packet": prev_dirs[-1], "last_families": r.get("families"),
                        "last_evidence": (r.get("evidence") or "")[:240], "status": "absent-this-week-explain"})
    return out, prev_dirs[-1]


def write_xlsx(outdir, datasets):
    try:
        import openpyxl
    except ImportError:
        return False
    wb = openpyxl.Workbook()
    wb.remove(wb.active)
    for name, rows in datasets.items():
        ws = wb.create_sheet(name[:31])
        if not rows:
            ws.append(["(empty)"])
            continue
        cols = list(rows[0].keys())
        ws.append(cols)
        for r in rows[:20000]:
            ws.append([("" if r.get(c) is None else (str(r.get(c))[:32000])) for c in cols])
    wb.save(os.path.join(outdir, "packet.xlsx"))
    return True


def main():
    ap = argparse.ArgumentParser(description="Layer 1 weekly packet")
    ap.add_argument("--end", "--end-date", dest="end", help="window end date (a Thursday by default)")
    a = ap.parse_args()
    end = L.parse_date(a.end) if a.end else last_window_end(L.today_et())
    win = window(end)
    started = time.time()
    kw = L.load_keywords()
    th = kw.get("thresholds", {})
    health = {"window": [win[0].isoformat(), win[-1].isoformat()], "started_et": L.now_et().isoformat(timespec="seconds")}

    data = load_window(win)
    health["daily_inputs"] = {m: {"rows": len(data[m]), "kept": sum(1 for r in data[m] if r.get("kept"))} for m in MODULE_DIRS}

    items = to_items(data)
    signals = build_signals(cluster(items), end, th)
    research = by_family(signals, {"research_cluster"})
    community = by_family(signals, {"community_signal"})
    conf = by_family(signals, {"conference_signal"})
    patent = by_family(signals, {"patent_signal"})
    gov_rd = by_family(signals, {"gov_rd_signal"})
    mfg = by_family(signals, {"manufacturing_partnership_signal"})
    spinout = by_family(signals, {"spinout_signal"})
    adoption = by_family(signals, {"adoption_signal"})
    usage = by_family(signals, {"usage_growth_signal"})
    topic_conv = topic_convergence(signals)
    co_conv = company_convergence(signals, end)
    conf_cal = conference_window(end)
    etf = etf_overlap(signals, health)
    cont, prev_packet = continuity(signals, end)

    arxiv_days = [d.isoformat() for d in win if os.path.exists(os.path.join(DAILY, "arxiv", d.isoformat() + ".jsonl"))]
    feed_days = [d.isoformat() for d in win if os.path.exists(os.path.join(DAILY, "feeds", d.isoformat() + ".jsonl"))]
    health["arxiv_days_with_data"] = len(arxiv_days)
    health["feed_days_with_data"] = len(feed_days)

    outdir = L.ensure_dir(os.path.join(WEEKLY, end.isoformat()))
    datasets = {
        "01_signals": signals,
        "02_research_clusters": research,
        "03_community_signals": community,
        "04_conference_signals": conf,
        "05_patent_signals": patent,
        "06_gov_rd_signals": gov_rd,
        "07_manufacturing_partnership_signals": mfg,
        "08_spinout_signals": spinout,
        "09_adoption_signals": adoption,
        "10_usage_growth_signals": usage,
        "11_topic_convergence": topic_conv,
        "12_company_convergence": co_conv,
        "13_conference_calendar_window": conf_cal,
        "14_etf_overlap": etf,
        "15_sec_fulltext_all": data["fts"],
        "16_continuity": cont,
    }
    ds_counts = {}
    for name, rows in datasets.items():
        L.write_csv(os.path.join(outdir, name + ".csv"), rows)
        ds_counts[name] = len(rows)
    fam_counts = {}
    for s in signals:
        for f in (s["families"] or "").split("; "):
            if f:
                fam_counts[f] = fam_counts.get(f, 0) + 1
    health["signals_by_family"] = fam_counts
    health["dataset_rows"] = ds_counts
    health["prior_packet"] = prev_packet
    health["not_collected_by_script"] = [
        "Patents: patents.google.com/xhr/query is bot-detection-gated after a handful of requests (confirmed 2026-10-17, HTTP 503) - best-effort only, budgeted to a few queries/run. A registered USPTO_ODP_API_KEY (free, api.uspto.gov) would make this reliable; not yet obtained -> agents (routine 03) are primary patent coverage.",
        "LinkedIn job-search/hiring signals: login-walled for scripted clients (200 but a sign-in prompt, confirmed 2026-10-17) -> agents.",
        "Patent assignment/filer searches beyond the best-effort Google Patents module (USPTO PatFT, Lens.org, EPO OPS) remain blocked or fair-use-gated to a scripted client -> agents, same status as the retired framework found.",
        "Semiconductor Today, optics.org and TrendForce: homepages work but no reachable RSS path was found 2026-10-17 -> agents, or a future re-test once the real feed URL is found.",
        "Conference programme committees, paper/startup line-ups (Hot Chips, ISSCC, DAC...) themselves: the calendar module only flags the WINDOW is open, not what's on the programme -> agents read the actual programme once it is published.",
        "G2/Gartner review-volume, SimilarWeb/BuiltWith footprint growth (usage_growth_signal's richest sources): G2 is 403 to scripted clients, SimilarWeb/BuiltWith are JS-rendered (202, no body) -> agents.",
        "Items whose wording matches no vocabulary combination -> cross-sector/source-gap routine (03) is the backstop.",
    ]
    health["finished_et"] = L.now_et().isoformat(timespec="seconds")
    health["elapsed_min"] = round((time.time() - started) / 60, 1)
    L.save_json(os.path.join(outdir, "coverage.json"), health)
    big = ("15_sec_fulltext_all",)
    xl = write_xlsx(outdir, {k: v for k, v in datasets.items() if k not in big})

    lines = [f"# Layer 1 weekly packet - window {win[0]} to {win[-1]}", "",
             f"Built {health['finished_et']} (ET). Prior packet: {prev_packet or 'none'}.", "", "## Counts", ""]
    lines += [f"- {k}: {v}" for k, v in ds_counts.items()]
    lines += ["", "## Signals by family", ""] + [f"- {k}: {v}" for k, v in sorted(fam_counts.items(), key=lambda x: -x[1])]
    lines += ["", "## Coverage", "", f"- arXiv days with data: {len(arxiv_days)} of 7", f"- Feed days with data: {len(feed_days)} of 7",
              "", "## Not collected by script (agents must cover)", ""] + [f"- {x}" for x in health["not_collected_by_script"]]
    with open(os.path.join(outdir, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log = os.path.join(L.DATA, "run-log.csv")
    with open(log, "a", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerow([health["finished_et"], "weekly", end.isoformat(), "packet", "", "", len(signals), health["elapsed_min"]])
    print({"window": health["window"], "signals": len(signals), "by_family": fam_counts, "dataset_rows": ds_counts,
           "xlsx": xl, "elapsed_min": health["elapsed_min"]})


if __name__ == "__main__":
    main()
