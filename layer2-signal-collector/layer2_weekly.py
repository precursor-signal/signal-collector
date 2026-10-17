#!/usr/bin/env python3
"""Layer 2 WEEKLY packet builder - human capital / talent-flow signals.

Reads the last 7 days of the daily collector's stored data, adds the weekly hiring snapshot
(layer2_hiring.py) and builds the packet the discovery routines read.
Output: data/weekly/<end-date>/  (the window is Thursday-Wednesday; the folder is named for the Wednesday)

Steps
  1. consolidate the week's kept filings / full-text hits / feed items into de-duplicated talent signals
  2. split them by kind: leadership moves, board / academic moves, pedigree moves, departures,
     departures to found, hiring-surge wording
  3. SEC Item 5.02 and Form 3 datasets (the structured sources of officer and director changes)
  4. join each signal to the hiring snapshot (open roles, change, surge flag) when the company is mapped
  5. talent-source convergence (which pedigree organisations lost people to which companies) and
     company convergence (same company, several signals, trailing 4 packets)
  6. copy the hiring datasets (company, sector, role family, surges, titles) into the packet
  7. ETF overlap (a coverage method, not a signal), status flags, continuity against the prior packet
  8. coverage ledger (what was NOT collected) + SUMMARY.md + packet.xlsx

Usage
  python layer2_weekly.py                  # window = the 7 days Thursday-Wednesday ending yesterday
  python layer2_weekly.py --end 2026-10-14
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import shutil
import time

import l2lib as L

DAILY = os.path.join(L.DATA, "daily")
WEEKLY = os.path.join(L.DATA, "weekly")
HIRING_WEEKLY = os.path.join(L.DATA, "hiring", "weekly")
L4_ETF = os.path.normpath(os.path.join(L.ROOT, "..", "layer4-signal-collector", "data", "daily", "etf", "snapshots"))

DEPARTURE_FAMILIES = {"departure", "departure_to_found"}


def last_window_end(today):
    """The Wednesday that closes the window: the latest Wednesday on or before yesterday (a Thursday run gets yesterday)."""
    d = today - dt.timedelta(days=1)
    while d.weekday() != 2:
        d -= dt.timedelta(days=1)
    return d


def window(end):
    return [end - dt.timedelta(days=i) for i in range(6, -1, -1)]


def d_of(s):
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


STOP = {"the", "and", "for", "with", "that", "from", "inc", "corp", "announces", "announced", "its", "has", "will", "into", "names", "named",
        "appoints", "appointed", "joins", "joined", "chief", "officer", "board", "directors", "president", "company"}


def toks(s):
    return set(w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())) - STOP


def jacc(a, b):
    return len(a & b) / max(1, len(a | b))


def pset(s):
    return {re.sub(r"[^a-z]", "", p.lower()) for p in (s or "").split("; ") if p}


# ----------------------------------------------------------------------------- load daily data


def load_window(win):
    days = {d.isoformat() for d in win}
    sec, fts, feeds = [], [], []
    sec_root = os.path.join(DAILY, "sec")
    if os.path.isdir(sec_root):
        for name in sorted(os.listdir(sec_root)):
            if name in days:
                sec += L.read_jsonl(os.path.join(sec_root, name, "filings.jsonl"))
    for sub, bucket in (("fts", fts), ("feeds", feeds)):
        root = os.path.join(DAILY, sub)
        if os.path.isdir(root):
            for fn in sorted(os.listdir(root)):
                if fn.endswith(".jsonl") and fn[:-6] in days:
                    bucket += L.read_jsonl(os.path.join(root, fn))
    return sec, fts, feeds


# ----------------------------------------------------------------------------- consolidate


def to_items(sec, fts, feeds):
    items = []
    for r in sec:
        if not r.get("kept"):
            continue
        ent = r.get("ticker") or r.get("issuer_symbol") or L.norm_name(r.get("company", ""))
        items.append({"src": "sec", "date": r.get("filed"), "entity": ent, "company": r.get("company"), "ticker": r.get("ticker", ""),
                      "sectors": r.get("sectors", "") or r.get("sector_hits", ""), "title": f"{r.get('company')} {r.get('form')} {r.get('evidence', '')[:120]}",
                      "link": r.get("url"), "r": r, "source_label": f"SEC {r.get('form')}"})
    for r in fts:
        if not r.get("kept"):
            continue
        items.append({"src": "sec-fts", "date": r.get("filed"), "entity": r.get("ticker") or L.norm_name(r.get("company", "")),
                      "company": r.get("company"), "ticker": r.get("ticker", ""), "sectors": r.get("sectors", "") or r.get("sector_hits", ""),
                      "title": f"{r.get('company')} {r.get('form')} full-text: {r.get('query')}", "link": r.get("url"),
                      "r": r, "source_label": f"SEC full-text {r.get('form')}"})
    for r in feeds:
        if not r.get("kept"):
            continue
        tk = (r.get("tickers") or "").split("; ")[0]
        pm = (r.get("private_matches") or "").split("; ")[0]
        items.append({"src": "feed", "date": (r.get("published_et") or r.get("captured_et") or "")[:10], "entity": tk or L.norm_name(pm),
                      "company": pm, "ticker": tk, "sectors": r.get("sector_hits", ""), "title": r.get("title", ""),
                      "link": r.get("link"), "r": r, "source_label": f"{r.get('category')}:{r.get('feed_id')}"})
    return items


def cluster(items):
    """Greedy clustering: same entity and a shared person or similar words; or the same person pair; or very similar titles."""
    clusters = []
    for it in items:
        r = it["r"]
        it["_persons"] = pset(r.get("persons"))
        it["_tok"] = toks(it["title"] + " " + (r.get("evidence") or "")[:200])
        placed = False
        for c in clusters:
            h = c["items"][0]
            same_ent = it["entity"] and it["entity"] == h["entity"]
            shared_person = bool(it["_persons"] & h["_persons"])
            if same_ent and (shared_person or jacc(it["_tok"], h["_tok"]) >= 0.25):
                c["items"].append(it)
                placed = True
                break
            if shared_person and len(it["_persons"] & h["_persons"]) >= 1 and jacc(it["_tok"], h["_tok"]) >= 0.2:
                c["items"].append(it)
                placed = True
                break
            if jacc(it["_tok"], h["_tok"]) >= 0.6:
                c["items"].append(it)
                placed = True
                break
        if not placed:
            clusters.append({"items": [it]})
    return clusters


def build_signals(clusters, end, th, hiring_by_company):
    rows = []
    for n, c in enumerate(clusters, 1):
        its = c["items"]
        best = max(its, key=lambda x: (x["r"].get("score") or 0, x["src"] == "sec"))
        r = best["r"]
        dates = sorted(d for d in (d_of(i["date"]) for i in its) if d)
        first = dates[0] if dates else None
        age = (end - first).days if first else None
        flags = []
        if age is not None and age > th.get("very_stale_days", 90):
            flags.append("stale-over-90d")
        elif age is not None and age > th.get("stale_days", 30):
            flags.append("stale-over-30d")
        if len({i["src"] for i in its}) == 1 and len(its) == 1:
            flags.append("single-source")
        if not (best["ticker"] or best["company"]):
            flags.append("entity-unresolved")
        if any(i["r"].get("low_value_title") for i in its):
            flags.append("low-value-title")
        if all(i["r"].get("form") in ("3", "3/A") for i in its):
            flags.append("form3-only-check-new-insider")
        fams, persons, titles, orgs, combos = [], [], [], [], []
        for i in its:
            for key, bucket in (("families", fams), ("persons", persons), ("titles", titles), ("pedigree_orgs", orgs), ("combos", combos)):
                for f in (i["r"].get(key) or "").split("; "):
                    if f and f not in bucket:
                        bucket.append(f)
        entity = best["company"] or best["ticker"] or best["title"][:60]
        hk = hiring_by_company.get((best["ticker"] or "").upper()) or hiring_by_company.get(L.norm_name(entity))
        rows.append({
            "signal_id": f"{end.isoformat()}-L2-{n:04d}",
            "entity": entity, "ticker": best["ticker"], "sectors": best["sectors"] or r.get("sector_hits", ""),
            "private_watchlist": r.get("private_matches", ""),
            "families": "; ".join(fams), "combos": "; ".join(combos), "score": max((i["r"].get("score") or 0) for i in its),
            "persons": "; ".join(persons[:8]), "titles": "; ".join(titles[:8]), "pedigree_orgs": "; ".join(orgs[:8]),
            "evidence": r.get("evidence", ""), "first_disclosed": first.isoformat() if first else "",
            "age_days": age, "n_items": len(its),
            "source_types": "; ".join(sorted({i["src"] for i in its})),
            "sources": " | ".join(dict.fromkeys(i["link"] for i in its if i.get("link")))[:900],
            "source_labels": "; ".join(dict.fromkeys(i["source_label"] for i in its))[:300],
            "in_universe": r.get("in_universe", ""),
            "hiring_open_roles": hk["total_open"] if hk else "", "hiring_change_pct": hk["change_pct"] if hk else "",
            "hiring_flag": hk["flag"] if hk else "", "hiring_top_families": hk["top_families"] if hk else "",
            "flags": "; ".join(flags),
        })
    rows.sort(key=lambda x: (-int(x["score"] or 0), x["entity"]))
    return rows


# ----------------------------------------------------------------------------- hiring snapshot


def latest_hiring(end):
    """The newest hiring snapshot dated within the window (+1 day), else the newest earlier one (reported as stale)."""
    if not os.path.isdir(HIRING_WEEKLY):
        return None, None
    dirs = sorted(d for d in os.listdir(HIRING_WEEKLY) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d))
    ok = [d for d in dirs if d <= (end + dt.timedelta(days=1)).isoformat()]
    if not ok:
        return None, None
    d = ok[-1]
    age = (end - dt.date.fromisoformat(d)).days
    return d, age


def hiring_index(hdir):
    idx = {}
    if not hdir:
        return idx
    p = os.path.join(HIRING_WEEKLY, hdir, "company-hiring.csv")
    if not os.path.exists(p):
        return idx
    for r in L.read_csv(p):
        if r.get("ticker"):
            idx[r["ticker"].upper()] = r
        idx[L.norm_name(r["company"])] = r
    return idx


# ----------------------------------------------------------------------------- derived datasets


def by_family(signals, wanted):
    return [s for s in signals if set((s["families"] or "").split("; ")) & wanted]


def convergence(signals, end):
    cur = {}
    for s in signals:
        ent = s["ticker"] or s["entity"]
        cur.setdefault(ent, {"entity": s["entity"], "ticker": s["ticker"], "this_week": 0, "trailing": 0, "weeks_seen": set(), "fams": set(), "persons": set()})
        e = cur[ent]
        e["this_week"] += 1
        e["fams"].update(f for f in (s["families"] or "").split("; ") if f)
        e["persons"].update(p for p in (s["persons"] or "").split("; ") if p)
        e["weeks_seen"].add(end.isoformat())
    prev_dirs = sorted(d for d in os.listdir(WEEKLY) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < end.isoformat()) if os.path.isdir(WEEKLY) else []
    for d in prev_dirs[-4:]:
        p = os.path.join(WEEKLY, d, "01_signals.csv")
        if os.path.exists(p):
            for r in L.read_csv(p):
                ent = r.get("ticker") or r.get("entity")
                cur.setdefault(ent, {"entity": r.get("entity"), "ticker": r.get("ticker"), "this_week": 0, "trailing": 0, "weeks_seen": set(), "fams": set(), "persons": set()})
                e = cur[ent]
                e["trailing"] += 1
                e["weeks_seen"].add(d)
                e["fams"].update(f for f in (r.get("families") or "").split("; ") if f)
                e["persons"].update(p2 for p2 in (r.get("persons") or "").split("; ") if p2)
    out = []
    for ent, e in cur.items():
        total = e["this_week"] + e["trailing"]
        if total >= 2:
            out.append({"entity": e["entity"], "ticker": e["ticker"], "signals_this_week": e["this_week"], "signals_trailing_4_packets": e["trailing"],
                        "weeks_seen": len(e["weeks_seen"]), "distinct_families": "; ".join(sorted(e["fams"])),
                        "distinct_people": len(e["persons"]), "people": "; ".join(sorted(e["persons"]))[:400]})
    out.sort(key=lambda x: (-x["signals_this_week"], -x["distinct_people"], -x["weeks_seen"]))
    return out


def talent_sources(signals):
    """Which pedigree organisations show up in this week's moves, and into which companies."""
    src = {}
    for s in signals:
        for o in (s["pedigree_orgs"] or "").split("; "):
            if o:
                v = src.setdefault(o, {"org": o, "moves": 0, "destinations": set(), "people": set()})
                v["moves"] += 1
                v["destinations"].add(s["entity"])
                v["people"].update(p for p in (s["persons"] or "").split("; ") if p)
    out = [{"pedigree_org": v["org"], "moves_this_week": v["moves"], "destination_companies": len(v["destinations"]),
            "destinations": "; ".join(sorted(v["destinations"]))[:400], "people": "; ".join(sorted(v["people"]))[:300]} for v in src.values()]
    out.sort(key=lambda x: (-x["destination_companies"], -x["moves_this_week"]))
    return out


def etf_overlap(signals, health):
    if not os.path.isdir(L4_ETF):
        health["etf_overlap"] = {"collected": False, "reason": "Layer 4 ETF snapshots not found at " + L4_ETF}
        return []
    tick = {s["ticker"] for s in signals if s["ticker"]}
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
                        "overlap_pct_of_holdings": round(100 * len(ov) / max(1, len(rows)), 1), "overlap_tickers": "; ".join(ov)})
    out.sort(key=lambda x: -x["overlap_count"])
    health["etf_overlap"] = {"collected": True, "etfs_with_overlap": len(out)}
    return out


def status_flags(signals, universe):
    out = []
    for s in signals:
        t = s["ticker"]
        if not t:
            continue
        if "." in t:
            out.append({"ticker": t, "entity": s["entity"], "flag": "foreign-listed-no-sec-cik"})
        elif t not in universe.cik_by_ticker:
            out.append({"ticker": t, "entity": s["entity"], "flag": "not-in-sec-ticker-list (delisted, renamed or foreign)"})
    return out


def continuity(signals, end):
    prev_dirs = sorted(d for d in os.listdir(WEEKLY) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < end.isoformat()) if os.path.isdir(WEEKLY) else []
    if not prev_dirs:
        return [], None
    p = os.path.join(WEEKLY, prev_dirs[-1], "01_signals.csv")
    if not os.path.exists(p):
        return [], prev_dirs[-1]
    cur = {s["ticker"] or s["entity"] for s in signals}
    out = []
    for r in L.read_csv(p):
        k = r.get("ticker") or r.get("entity")
        if k and k not in cur:
            out.append({"entity": r.get("entity"), "ticker": r.get("ticker"), "last_packet": prev_dirs[-1], "last_families": r.get("families"),
                        "last_persons": r.get("persons"), "last_evidence": (r.get("evidence") or "")[:240], "status": "absent-this-week-explain"})
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
    ap = argparse.ArgumentParser(description="Layer 2 weekly packet")
    ap.add_argument("--end", "--end-date", dest="end", help="window end date (a Wednesday by default: the latest Wednesday on or before yesterday ET; the window is Thursday-Wednesday)")
    a = ap.parse_args()
    end = L.parse_date(a.end) if a.end else last_window_end(L.today_et())
    win = window(end)
    started = time.time()
    kw = L.load_keywords()
    th = kw.get("thresholds", {})
    health = {"window": [win[0].isoformat(), win[-1].isoformat()], "started_et": L.now_et().isoformat(timespec="seconds")}
    universe = L.Universe()

    sec, fts, feeds = load_window(win)
    health["daily_inputs"] = {"sec_rows": len(sec), "sec_kept": sum(1 for r in sec if r.get("kept")), "fts_rows": len(fts),
                              "fts_kept": sum(1 for r in fts if r.get("kept")), "feed_items": len(feeds),
                              "feed_kept": sum(1 for r in feeds if r.get("kept"))}
    sec_days = [d.isoformat() for d in win if d.weekday() < 5]
    stored = [d for d in sec_days if os.path.exists(os.path.join(DAILY, "sec", d, "_done.json"))]
    feed_days = [d.isoformat() for d in win if os.path.exists(os.path.join(DAILY, "feeds", d.isoformat() + ".jsonl"))]
    health["sec_days"] = {"weekdays": len(sec_days), "stored_days": len(stored), "missing_days": [d for d in sec_days if d not in stored]}
    health["feed_days_with_data"] = len(feed_days)

    hdir, hage = latest_hiring(end)
    hidx = hiring_index(hdir)
    health["hiring_snapshot"] = {"folder": hdir, "age_days_at_window_end": hage, "companies": len({v["company"] for v in hidx.values()}) if hidx else 0,
                                 "note": "none found - run layer2_hiring.py" if not hdir else ("stale: older than the window" if hage is not None and hage > 8 else "ok")}

    items = to_items(sec, fts, feeds)
    signals = build_signals(cluster(items), end, th, hidx)
    leadership = by_family(signals, {"leadership_move"})
    board_acad = by_family(signals, {"board_move", "academic_move"})
    pedigree = by_family(signals, {"pedigree_move"})
    departures = by_family(signals, DEPARTURE_FAMILIES)
    hiring_words = by_family(signals, {"hiring_surge"})
    conv = convergence(signals, end)
    srcs = talent_sources(signals)
    etf = etf_overlap(signals, health)
    flags = status_flags(signals, universe)
    cont, prev_packet = continuity(signals, end)
    sec_502 = [r for r in sec if r.get("item_5_02")]
    form3 = [r for r in sec if r.get("form") in ("3", "3/A")]
    not_kept_502 = [r for r in sec_502 if not r.get("kept")]

    outdir = L.ensure_dir(os.path.join(WEEKLY, end.isoformat()))
    datasets = {
        "01_signals": signals,
        "02_leadership_moves": leadership,
        "03_board_and_academic": board_acad,
        "04_pedigree_moves": pedigree,
        "05_departures_and_founders": departures,
        "06_hiring_wording_signals": hiring_words,
        "07_sec_item_5_02_all": sec_502,
        "08_sec_form3_new_insiders": form3,
        "09_sec_fulltext_hits": fts,
        "10_feed_items_kept": [r for r in feeds if r.get("kept")],
        "11_company_convergence": conv,
        "12_talent_source_orgs": srcs,
        "13_item_5_02_below_keep_score": not_kept_502,
        "14_etf_overlap": etf,
        "15_status_flags": flags,
        "16_continuity": cont,
    }
    # hiring datasets: copied from the snapshot so the packet is self-contained
    hiring_files = {"17_hiring_company": "company-hiring.csv", "18_hiring_sector": "sector-hiring.csv", "19_hiring_family": "family-hiring.csv",
                    "20_hiring_surges": "surges.csv", "21_hiring_titles": "title-trends.csv", "22_hiring_new_roles": "new-roles.csv"}
    for name, fn in hiring_files.items():
        p = os.path.join(HIRING_WEEKLY, hdir, fn) if hdir else None
        rows = L.read_csv(p) if p and os.path.exists(p) else []
        if name == "17_hiring_company":
            rows = [{k: v for k, v in r.items() if k != "families_json"} for r in rows]
        datasets[name] = rows
        if name == "22_hiring_new_roles":
            datasets[name] = rows[:3000]
    if hdir:
        for fn in ("market-context.json", "coverage.json", "SUMMARY.md"):
            p = os.path.join(HIRING_WEEKLY, hdir, fn)
            if os.path.exists(p):
                shutil.copy(p, os.path.join(L.ensure_dir(outdir), "hiring-" + fn))
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
        "Sources blocked to scripted clients (see feeds.csv enabled=no): GlobeNewswire, Business Wire, ACCESS Newswire, GovConWire, ExecutiveBiz, Washington Technology, Quantum Computing Report, TheMinerMag, Intel and Anthropic newsrooms -> agents (routine 03)",
        "Feeds that returned 404 or an interstitial: GeekWire Tech Moves tag, Data Center Frontier, optics.org, Fortune tech -> agents",
        "LinkedIn profile and 'new position' moves, X / Substack personal announcements, podcasts (no scripted access) -> agents",
        "Private-company officers and directors (SEC Form D related persons; Crunchbase / PitchBook) -> agents",
        "Company career pages, investor-relations leadership pages and board pages (only changes show up, never the page itself) -> agents",
        "University and national-laboratory appointment pages, conference programme committees -> agents",
        "Foreign-exchange filings and releases (TWSE, KRX, TSE, HKEX, Euronext, ASX/TSX) -> agents",
        "Paywalled outlets (The Information, Bloomberg, Reuters, WSJ, FT) -> agents",
        "Patent-assignment moves (confirmed not executable with current tools, 2026-07-09) -> not covered",
        "Moves announced in wording that matches no vocabulary combination -> cross-sector headline sweep (routine 02)",
        "Hiring: companies without a public Greenhouse / Lever / Ashby board and all public mega-caps; LinkedIn / Indeed / Glassdoor counts -> agents (see hiring-coverage.json)",
    ]
    if not hdir:
        health["not_collected_by_script"].append("NO hiring snapshot found for this window -> agents must cover hiring signals this week")
    elif hage is not None and hage > 8:
        health["not_collected_by_script"].append(f"Hiring snapshot is {hage} days old (stale) -> agents must refresh hiring signals")
    health["finished_et"] = L.now_et().isoformat(timespec="seconds")
    health["elapsed_min"] = round((time.time() - started) / 60, 1)
    L.save_json(os.path.join(outdir, "coverage.json"), health)
    L.save_json(os.path.join(outdir, "run-health.json"), {k: v for k, v in health.items() if k != "not_collected_by_script"})
    big = ("07_sec_item_5_02_all", "09_sec_fulltext_hits", "10_feed_items_kept", "13_item_5_02_below_keep_score", "22_hiring_new_roles")
    xl = write_xlsx(outdir, {k: v for k, v in datasets.items() if k not in big})

    lines = [f"# Layer 2 weekly packet - window {win[0]} to {win[-1]}", "",
             f"Built {health['finished_et']} (ET). Prior packet: {prev_packet or 'none'}. Hiring snapshot: {hdir or 'none'}.", "", "## Counts", ""]
    lines += [f"- {k}: {v}" for k, v in ds_counts.items()]
    lines += ["", "## Signals by family", ""] + [f"- {k}: {v}" for k, v in sorted(fam_counts.items(), key=lambda x: -x[1])]
    lines += ["", "## Coverage", "", f"- SEC days stored: {len(stored)} of {len(sec_days)} weekdays; missing: {health['sec_days']['missing_days'] or 'none'}",
              f"- Feed days with data: {len(feed_days)} of 7", f"- Hiring: {health['hiring_snapshot']}",
              "", "## Not collected by script (agents must cover)", ""] + [f"- {x}" for x in health["not_collected_by_script"]]
    with open(os.path.join(outdir, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log = os.path.join(L.DATA, "run-log.csv")
    with open(log, "a", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerow([health["finished_et"], "weekly", end.isoformat(), "packet", "", "", "", "", len(signals), health["elapsed_min"]])
    print(json.dumps({"window": health["window"], "daily_inputs": health["daily_inputs"], "sec_days": health["sec_days"],
                      "hiring_snapshot": health["hiring_snapshot"], "signals": len(signals), "by_family": fam_counts, "dataset_rows": ds_counts,
                      "xlsx": xl, "elapsed_min": health["elapsed_min"]}, indent=1, default=str))


if __name__ == "__main__":
    main()
