#!/usr/bin/env python3
"""Layer 3 WEEKLY packet builder - supply chain and procurement signals.

Reads the last 7 days of the daily collector's stored data and adds the slow sources that a
weekly pass is enough for. Output: data/weekly/<end-date>/ - the packet the discovery routines read.

Steps
  1. consolidate the week's kept filings / full-text hits / feed items into de-duplicated signals
  2. USAspending award sweep (sector vocabulary + registry recipient names)        [slow source]
  3. SAM.gov award notices (needs a free api key in SAM_API_KEY, otherwise reported as not collected)
  4. periodic-report backlog / supplier-disclosure extracts + change against the prior period
  5. ERCOT large-load page watch (new documents)
  6. counterparty convergence (same buyer named by several companies, trailing 4 packets)
  7. ETF overlap with the Layer 4 holdings snapshots (a coverage method, not a signal)
  8. status flags, continuity against the prior packet, stale flags, Layer 4 cross-reference
  9. coverage ledger (what was NOT collected) + SUMMARY.md + packet.xlsx

Usage
  python layer3_weekly.py                  # window = the 7 days Wednesday-Tuesday ending yesterday
  python layer3_weekly.py --end 2026-10-11
  python layer3_weekly.py --skip-slow      # no USAspending / SAM / ERCOT network calls
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.parse

import l3lib as L

DAILY = os.path.join(L.DATA, "daily")
WEEKLY = os.path.join(L.DATA, "weekly")
STATE = os.path.join(WEEKLY, "_state")
L4_ETF = os.path.normpath(os.path.join(L.ROOT, "..", "layer4-signal-collector", "data", "daily", "etf", "snapshots"))

USA_EXTRA_KEYWORDS = ["semiconductor", "microelectronics", "data center", "satellite", "quantum", "nuclear reactor",
                      "hypersonic", "rare earth", "critical mineral", "battery", "gallium", "advanced packaging",
                      "radiation hardened", "cooling", "transformer", "munitions", "launch services"]


def last_window_end(today):
    """The Tuesday that closes the window: the latest Tuesday on or before yesterday (a Wednesday run gets yesterday)."""
    d = today - dt.timedelta(days=1)
    while d.weekday() != 1:
        d -= dt.timedelta(days=1)
    return d


def window(end):
    return [end - dt.timedelta(days=i) for i in range(6, -1, -1)]


def d_of(s):
    try:
        return dt.date.fromisoformat((s or "")[:10])
    except ValueError:
        return None


def toks(s):
    return set(w for w in re.findall(r"[a-z0-9]{3,}", (s or "").lower())) - {"the", "and", "for", "with", "that", "from", "inc", "corp", "announces", "announced", "its", "has", "will", "into"}


def jacc(a, b):
    return len(a & b) / max(1, len(a | b))


# ----------------------------------------------------------------------------- load daily data


def load_window(win):
    days = {d.isoformat() for d in win}
    sec, fts, feeds = [], [], []
    # SEC days are keyed by filing date; fetch a margin because filings land on weekdays only
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


def to_items(sec, fts, feeds, th, end):
    items = []
    for r in sec:
        if not r.get("kept"):
            continue
        items.append({"src": "sec", "date": r.get("filed"), "entity": r.get("ticker") or L.norm_name(r.get("company", "")),
                      "company": r.get("company"), "ticker": r.get("ticker", ""), "sectors": r.get("sectors", ""),
                      "title": f"{r.get('company')} {r.get('form')} {r.get('item_codes', '')}", "link": r.get("url"),
                      "r": r, "source_label": f"SEC {r.get('form')}"})
    for r in fts:
        if not r.get("kept"):
            continue
        items.append({"src": "sec-fts", "date": r.get("filed"), "entity": r.get("ticker") or L.norm_name(r.get("company", "")),
                      "company": r.get("company"), "ticker": r.get("ticker", ""), "sectors": r.get("sectors", ""),
                      "title": f"{r.get('company')} {r.get('form')} full-text: {r.get('query')}", "link": r.get("url"),
                      "r": r, "source_label": f"SEC full-text {r.get('form')}"})
    for r in feeds:
        if not r.get("kept"):
            continue
        tk = (r.get("tickers") or "").split("; ")[0]
        items.append({"src": "feed", "date": (r.get("published_et") or r.get("captured_et") or "")[:10], "entity": tk or "",
                      "company": "", "ticker": tk, "sectors": r.get("sector_hits", ""), "title": r.get("title", ""),
                      "link": r.get("link"), "r": r, "source_label": f"{r.get('category')}:{r.get('feed_id')}"})
    return items


def cluster(items):
    """Greedy clustering: same entity + family + (same figure or similar title), or very similar titles."""
    clusters = []
    for it in items:
        r = it["r"]
        fam = (r.get("families") or "").split("; ")[0]
        first_money = (r.get("money") or "").split("; ")[0]
        mv = L.parse_money(first_money)
        qty = str(mv) if mv else (r.get("capacity_units") or "").split("; ")[0].lower().replace(" ", "")
        tk = toks(it["title"] + " " + (r.get("evidence") or "")[:160])
        it["_fam"], it["_qty"], it["_tok"] = fam, qty, tk
        placed = False
        for c in clusters:
            h = c["items"][0]
            same_ent = it["entity"] and it["entity"] == h["entity"]
            same_filing_day = it["src"] == "sec" and h["src"] == "sec" and it["date"] == h["date"]
            if same_ent and fam == h["_fam"] and (qty == h["_qty"] or jacc(tk, h["_tok"]) >= 0.25 or same_filing_day):
                c["items"].append(it); placed = True; break
            if jacc(tk, h["_tok"]) >= 0.6 and fam == h["_fam"]:
                c["items"].append(it); placed = True; break
        if not placed:
            clusters.append({"items": [it]})
    return clusters


def build_signals(clusters, end, th):
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
        if r.get("binding_status") in ("non-binding-or-prospective", "mixed-check"):
            flags.append("check-binding-status")
        if len({i["src"] for i in its}) == 1 and len(its) == 1:
            flags.append("single-source")
        if not (best["ticker"] or best["company"]):
            flags.append("entity-unresolved")
        fams = []
        for i in its:
            for f in (i["r"].get("families") or "").split("; "):
                if f and f not in fams:
                    fams.append(f)
        buyers = []
        for i in its:
            for b in (i["r"].get("buyers") or "").split("; "):
                if b and b not in buyers:
                    buyers.append(b)
        rows.append({
            "signal_id": f"{end.isoformat()}-L3-{n:04d}",
            "entity": best["company"] or best["ticker"] or best["title"][:60],
            "ticker": best["ticker"], "sectors": best["sectors"] or r.get("sector_hits", ""),
            "families": "; ".join(fams), "combos": r.get("combos", ""), "score": max((i["r"].get("score") or 0) for i in its),
            "binding_status": r.get("binding_status", ""), "buyers": "; ".join(buyers),
            "money": r.get("money", ""), "capacity_units": r.get("capacity_units", ""),
            "evidence": r.get("evidence", ""), "first_disclosed": first.isoformat() if first else "",
            "age_days": age, "n_items": len(its),
            "source_types": "; ".join(sorted({i["src"] for i in its})),
            "sources": " | ".join(dict.fromkeys(i["link"] for i in its if i.get("link")))[:900],
            "source_labels": "; ".join(dict.fromkeys(i["source_label"] for i in its))[:300],
            "in_universe": r.get("in_universe", ""), "layer4_hints": r.get("layer4_hints", ""),
            "flags": "; ".join(flags),
        })
    rows.sort(key=lambda x: (-int(x["score"] or 0), x["entity"]))
    return rows


# ----------------------------------------------------------------------------- slow sources


def usaspending(win, universe, engine, th, health, budget=160):
    start, end = win[0].isoformat(), win[-1].isoformat()
    url = "https://api.usaspending.gov/api/v2/search/spending_by_transaction/"
    fields = ["Award ID", "Recipient Name", "Transaction Amount", "Action Date", "Awarding Agency", "Awarding Sub Agency", "Transaction Description"]
    calls, rows, errors = 0, {}, []
    min_amt = th.get("usaspending_min_amount_usd", 1_000_000)

    def run(filters, label, max_pages=3):
        nonlocal calls
        for page in range(1, max_pages + 1):
            if calls >= budget:
                errors.append(f"budget exhausted before {label}")
                return
            payload = {"filters": filters, "fields": fields, "limit": 100, "page": page, "sort": "Transaction Amount", "order": "desc"}
            st, js, err = L.post_json(url, payload, timeout=60)
            calls += 1
            if js is None:
                errors.append(f"{label}: {err or st}")
                return False
            for res in js.get("results", []):
                amt = res.get("Transaction Amount") or 0
                if amt < min_amt:
                    continue
                key = (res.get("Award ID"), res.get("Action Date"), amt)
                rec = rows.setdefault(key, {"award_id": res.get("Award ID"), "recipient": res.get("Recipient Name"), "amount_usd": amt,
                                            "action_date": res.get("Action Date"), "agency": res.get("Awarding Agency"),
                                            "sub_agency": res.get("Awarding Sub Agency"), "description": res.get("Transaction Description"),
                                            "internal_id": res.get("generated_internal_id"), "matched_by": set()})
                rec["matched_by"].add(label)
            if not js.get("page_metadata", {}).get("hasNext"):
                return True
        return True

    base = {"award_type_codes": ["A", "B", "C", "D"], "time_period": [{"start_date": start, "end_date": end}]}
    for s in L.load_sectors():
        kws = [k for k in s["keywords"] if len(k) >= 3][:8]
        run({**base, "keywords": kws}, "sector:" + s["slug"], max_pages=2)
    for i in range(0, len(USA_EXTRA_KEYWORDS), 6):
        run({**base, "keywords": USA_EXTRA_KEYWORDS[i:i + 6]}, "topic:" + str(i // 6), max_pages=2)
    # registry recipients (universe titles + buyers), batched
    names = []
    for cik, info in universe.ciks.items():
        t = universe.tickers_meta.get(cik, {}).get("title", "")
        if t and len(t) >= 5:
            names.append(re.sub(r"[,.]", "", t).upper())
    names = sorted(set(names))
    batch = 8   # 25 names per call returned HTTP 503 in testing; 8 is reliable
    for i in range(0, len(names), batch):
        if calls >= budget:
            errors.append(f"recipient batches stopped at {i} of {len(names)} names (budget)")
            break
        grp = names[i:i + batch]
        ok = run({**base, "recipient_search_text": grp}, "recipient-batch:" + str(i // batch), max_pages=1)
        if ok is False and len(grp) > 1 and calls < budget:   # one retry with the batch split in two
            h = len(grp) // 2
            run({**base, "recipient_search_text": grp[:h]}, "recipient-batch:" + str(i // batch) + "a", max_pages=1)
            run({**base, "recipient_search_text": grp[h:]}, "recipient-batch:" + str(i // batch) + "b", max_pages=1)
    out = []
    for rec in rows.values():
        rec["matched_by"] = "; ".join(sorted(rec["matched_by"]))
        nm = L.norm_name(rec["recipient"] or "")
        tk = universe.name_to_ticker.get(nm, "")
        if not tk:
            for n2, t2 in universe.name_to_ticker.items():
                if len(n2) >= 6 and (nm.startswith(n2) or n2.startswith(nm) and len(nm) >= 6):
                    tk = t2
                    break
        rec["ticker"] = tk
        rec["sector_hits"] = "; ".join(s for s, _n in engine.sector_hits(rec["description"] or "")[:3])
        rec["url"] = ("https://www.usaspending.gov/award/" + rec["internal_id"]) if rec.get("internal_id") else ""
        rec["relevant"] = bool(tk or rec["sector_hits"])
        an = engine.analyse((rec["description"] or "") + " " + (rec["agency"] or ""))
        rec["families"] = "gov_award"
        rec["combos"] = "; ".join(an["combos"])
        out.append(rec)
    dropped = sum(1 for r in out if not r["relevant"])
    out = [r for r in out if r["relevant"]]   # a keyword hit elsewhere in the record with no sector word in the description
    out.sort(key=lambda x: -(x["amount_usd"] or 0))
    health["usaspending"] = {"calls": calls, "rows": len(out), "dropped_not_relevant": dropped, "errors": errors[:10], "window": [start, end]}
    return out


def sam_notices(win, health):
    key = os.environ.get("SAM_API_KEY", "").strip()
    if not key:
        health["sam_gov"] = {"collected": False, "reason": "SAM_API_KEY not set (free key from sam.gov); the official API returns 404 without it. Awards not covered here are left to the agents."}
        return []
    rows, calls, err_list = [], 0, []
    mm = lambda d: d.strftime("%m/%d/%Y")
    for term in ["semiconductor", "data center", "microelectronics", "satellite", "quantum", "nuclear", "battery", "rare earth"]:
        url = ("https://api.sam.gov/opportunities/v2/search?api_key=" + urllib.parse.quote(key) + "&postedFrom=" + mm(win[0]) +
               "&postedTo=" + mm(win[-1]) + "&ptype=a&limit=100&title=" + urllib.parse.quote(term))
        st, txt, err = L.fetch_text(url, timeout=60)
        calls += 1
        if not txt:
            err_list.append(f"{term}: {err or st}")
            continue
        try:
            js = json.loads(txt)
        except Exception:
            err_list.append(f"{term}: not json")
            continue
        for o in js.get("opportunitiesData", []):
            rows.append({"notice_id": o.get("noticeId"), "title": o.get("title"), "agency": o.get("fullParentPathName"),
                         "posted": o.get("postedDate"), "type": o.get("type"), "award_amount": (o.get("award") or {}).get("amount"),
                         "awardee": (o.get("award") or {}).get("awardee", {}).get("name"), "url": o.get("uiLink"), "term": term})
    health["sam_gov"] = {"collected": True, "calls": calls, "rows": len(rows), "errors": err_list[:5]}
    return rows


def ercot_watch(health):
    url = "https://www.ercot.com/services/rq/large-load-integration"
    st, txt, err = L.fetch_text(url, timeout=60)
    if not txt:
        health["ercot"] = {"collected": False, "reason": err or f"HTTP {st}"}
        return []
    links = sorted(set(re.findall(r'href="(https://www\.ercot\.com/files/docs/[^"]+)"', txt)))
    state_path = os.path.join(STATE, "ercot_links.json")
    prev = L.load_json(state_path, None)
    first = prev is None
    new = [l for l in links if first or l not in set(prev)]
    L.save_json(state_path, links)
    health["ercot"] = {"collected": True, "documents_on_page": len(links), "new_documents": 0 if first else len(new),
                       "first_snapshot": first, "note": "document list only; named load parties are not published in aggregate reports"}
    return [{"document": l, "title": os.path.basename(urllib.parse.unquote(l)), "status": "baseline" if first else "new"} for l in (links if first else new)]


# ----------------------------------------------------------------------------- derived datasets


def periodic_rows(sec, health):
    state_path = os.path.join(STATE, "backlog.json")
    state = L.load_json(state_path, {}) or {}
    out = []
    for r in sec:
        if r.get("form") not in {"10-Q", "10-K", "10-K/A", "10-Q/A", "20-F", "40-F"}:
            continue
        fams = (r.get("families") or "")
        if not any(f in fams for f in ("order_backlog", "supplier_disclosure", "capacity_commitment", "supply_constraint")):
            continue
        key = str(r.get("cik"))
        money = r.get("money", "")
        prev = state.get(key)
        change = "first-seen" if prev is None else ("changed" if prev.get("money") != money else "same-figures")
        state[key] = {"money": money, "filed": r.get("filed"), "accession": r.get("accession")}
        out.append({"filed": r.get("filed"), "form": r.get("form"), "company": r.get("company"), "ticker": r.get("ticker"),
                    "families": fams, "money": money, "capacity_units": r.get("capacity_units", ""), "percent": r.get("percent", ""),
                    "evidence": r.get("evidence", ""), "vs_prior_period": change, "prior_figures": (prev or {}).get("money", ""),
                    "url": r.get("url")})
    L.save_json(state_path, state)
    return out


def convergence(signals, end):
    cur = {}
    for s in signals:
        ent = s["ticker"] or s["entity"]
        for b in (s["buyers"] or "").split("; "):
            if b:
                cur.setdefault(b, {"this_week": set(), "trailing": set()})["this_week"].add(ent)
    # trailing 4 packets
    prev_dirs = sorted(d for d in os.listdir(WEEKLY) if re.fullmatch(r"\d{4}-\d{2}-\d{2}", d) and d < end.isoformat()) if os.path.isdir(WEEKLY) else []
    for d in prev_dirs[-4:]:
        p = os.path.join(WEEKLY, d, "01_signals.csv")
        if os.path.exists(p):
            for r in L.read_csv(p):
                for b in (r.get("buyers") or "").split("; "):
                    if b:
                        cur.setdefault(b, {"this_week": set(), "trailing": set()})["trailing"].add(r.get("ticker") or r.get("entity"))
    out = []
    for b, v in cur.items():
        allent = v["this_week"] | v["trailing"]
        out.append({"counterparty": b, "entities_this_week": len(v["this_week"]), "entities_trailing_4_packets": len(v["trailing"]),
                    "distinct_entities_total": len(allent), "entities": "; ".join(sorted(allent))[:600]})
    out.sort(key=lambda x: -x["distinct_entities_total"])
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
                        "last_evidence": (r.get("evidence") or "")[:240], "status": "absent-this-week-explain"})
    return out, prev_dirs[-1]


# ----------------------------------------------------------------------------- output


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
    ap = argparse.ArgumentParser(description="Layer 3 weekly packet")
    ap.add_argument("--end", "--end-date", dest="end", help="window end date (a Tuesday by default: the latest Tuesday on or before yesterday ET; the window is Wednesday-Tuesday)")
    ap.add_argument("--skip-slow", action="store_true")
    ap.add_argument("--usa-budget", type=int, default=160)
    a = ap.parse_args()
    end = L.parse_date(a.end) if a.end else last_window_end(L.today_et())
    win = window(end)
    started = time.time()
    kw = L.load_keywords()
    th = kw.get("thresholds", {})
    health = {"window": [win[0].isoformat(), win[-1].isoformat()], "started_et": L.now_et().isoformat(timespec="seconds")}
    universe = L.Universe()
    engine = L.SignalEngine(kw)

    sec, fts, feeds = load_window(win)
    health["daily_inputs"] = {"sec_rows": len(sec), "sec_kept": sum(1 for r in sec if r.get("kept")), "fts_rows": len(fts),
                              "fts_kept": sum(1 for r in fts if r.get("kept")), "feed_items": len(feeds),
                              "feed_kept": sum(1 for r in feeds if r.get("kept"))}
    sec_days = [d.isoformat() for d in win if d.weekday() < 5]
    stored = [d for d in sec_days if os.path.exists(os.path.join(DAILY, "sec", d, "_done.json"))]
    feed_days = [d.isoformat() for d in win if os.path.exists(os.path.join(DAILY, "feeds", d.isoformat() + ".jsonl"))]
    health["sec_days"] = {"weekdays": len(sec_days), "stored_days": len(stored), "missing_days": [d for d in sec_days if d not in stored]}
    health["feed_days_with_data"] = len(feed_days)

    items = to_items(sec, fts, feeds, th, end)
    signals = build_signals(cluster(items), end, th)
    # items that matched only Layer 4 vocabulary
    l4 = []
    for src, rows in (("sec", sec), ("sec-fts", fts), ("feed", feeds)):
        for r in rows:
            if r.get("layer4_hints") and not r.get("kept") and not r.get("noise"):
                l4.append({"source": src, "date": r.get("filed") or (r.get("published_et") or "")[:10], "entity": r.get("company") or r.get("title", "")[:80],
                           "ticker": r.get("ticker") or r.get("tickers", ""), "layer4_hints": r.get("layer4_hints"), "url": r.get("url") or r.get("link"),
                           "title": r.get("title", "")})

    slow = {}
    if not a.skip_slow:
        slow["usa"] = usaspending(win, universe, engine, th, health, budget=a.usa_budget)
        slow["sam"] = sam_notices(win, health)
        slow["ercot"] = ercot_watch(health)
    else:
        for k in ("usaspending", "sam_gov", "ercot"):
            health[k] = {"collected": False, "reason": "--skip-slow"}
        slow = {"usa": [], "sam": [], "ercot": []}

    periodic = periodic_rows(sec, health)
    supplier = [s for s in signals if "supplier_disclosure" in s["families"]]
    conv = convergence(signals, end)
    etf = etf_overlap(signals, health)
    flags = status_flags(signals, universe)
    cont, prev_packet = continuity(signals, end)

    outdir = L.ensure_dir(os.path.join(WEEKLY, end.isoformat()))
    datasets = {
        "01_signals": signals,
        "02_sec_filings_all": sec,
        "03_gov_awards_usaspending": slow["usa"],
        "04_sam_award_notices": slow["sam"],
        "05_periodic_backlog": periodic,
        "06_supplier_disclosures": supplier,
        "07_counterparty_convergence": conv,
        "08_etf_overlap": etf,
        "09_status_flags": flags,
        "10_continuity": cont,
        "11_layer4_crossref": l4,
        "12_ercot_documents": slow["ercot"],
        "13_sec_fulltext_hits": fts,
        "14_feed_items_kept": [r for r in feeds if r.get("kept")],
        "15_supply_constraint_context": [
            {"source": src, "date": r.get("filed") or (r.get("published_et") or "")[:10], "entity": r.get("company") or r.get("title", "")[:80],
             "ticker": r.get("ticker") or r.get("tickers", ""), "sectors": r.get("sectors") or r.get("sector_hits", ""),
             "evidence": r.get("evidence", ""), "url": r.get("url") or r.get("link")}
            for src, rows in (("sec", sec), ("sec-fts", fts), ("feed", feeds)) for r in rows if r.get("context_only")],
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
        "Sources blocked to scripted clients (see feeds.csv enabled=no): GlobeNewswire, Business Wire, GovConWire, Mining.com, DoD contract announcements, FERC, Commerce, Oracle/Anthropic/TSMC newsrooms -> agents",
        "PJM interconnection queue and data (data miner needs a subscription key) -> agents",
        "ERCOT large-load: document list only; named load parties are not in the published aggregate reports -> agents",
        "County permits, zoning and utility commission dockets (not machine readable at scale) -> agents",
        "Foreign-exchange filings and releases (TWSE, KRX, TSE, HKEX, Euronext, ASX/TSX) -> agents",
        "Earnings-call prose and investor presentations (outside filings) -> agents",
        "Paywalled outlets (The Information, Bloomberg, Reuters, FT) -> agents",
        "Deals announced by the supplier in wording that matches no vocabulary combination -> cross-sector headline sweep",
    ]
    if not health.get("sam_gov", {}).get("collected"):
        health["not_collected_by_script"].append("SAM.gov award notices (no API key configured) -> agents")
    if not health.get("usaspending", {}).get("rows") and not a.skip_slow:
        health["not_collected_by_script"].append("USAspending returned no rows in the window -> agents")
    health["finished_et"] = L.now_et().isoformat(timespec="seconds")
    health["elapsed_min"] = round((time.time() - started) / 60, 1)
    L.save_json(os.path.join(outdir, "coverage.json"), health)
    L.save_json(os.path.join(outdir, "run-health.json"), {k: v for k, v in health.items() if k != "not_collected_by_script"})
    xl = write_xlsx(outdir, {k: v for k, v in datasets.items() if k not in ("02_sec_filings_all", "13_sec_fulltext_hits", "14_feed_items_kept")})

    lines = [f"# Layer 3 weekly packet - window {win[0]} to {win[-1]}", "",
             f"Built {health['finished_et']} (ET). Prior packet: {prev_packet or 'none'}.", "",
             "## Counts", ""]
    lines += [f"- {k}: {v}" for k, v in ds_counts.items()]
    lines += ["", "## Signals by family", ""] + [f"- {k}: {v}" for k, v in sorted(fam_counts.items(), key=lambda x: -x[1])]
    lines += ["", "## Coverage", "", f"- SEC days stored: {len(stored)} of {len(sec_days)} weekdays; missing: {health['sec_days']['missing_days'] or 'none'}",
              f"- Feed days with data: {len(feed_days)} of 7",
              f"- USAspending: {health.get('usaspending')}", f"- SAM.gov: {health.get('sam_gov')}", f"- ERCOT: {health.get('ercot')}",
              "", "## Not collected by script (agents must cover)", ""] + [f"- {x}" for x in health["not_collected_by_script"]]
    with open(os.path.join(outdir, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    log = os.path.join(L.DATA, "run-log.csv")
    with open(log, "a", encoding="utf-8-sig", newline="") as f:
        csv.writer(f).writerow([health["finished_et"], "weekly", end.isoformat(), "packet", "", "", "", "", len(signals), health["elapsed_min"]])
    print(json.dumps({"window": health["window"], "daily_inputs": health["daily_inputs"], "sec_days": health["sec_days"],
                      "signals": len(signals), "by_family": fam_counts, "dataset_rows": ds_counts,
                      "usaspending": health.get("usaspending"), "sam_gov": health.get("sam_gov"), "ercot": health.get("ercot"),
                      "xlsx": xl, "elapsed_min": health["elapsed_min"]}, indent=1, default=str))


if __name__ == "__main__":
    main()
