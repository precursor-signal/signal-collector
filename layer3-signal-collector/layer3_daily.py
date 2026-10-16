#!/usr/bin/env python3
"""Layer 3 DAILY collector - supply chain and procurement signals. Capture only, no judgement.

Two layers of capture run together:
  WHOLESALE  every filing / feed item that is reachable is stored with its date, source and link
             before any filter is applied (nothing is dropped because a keyword did not match);
  KEYWORD    every stored item is then analysed with the Layer 3 vocabulary and combination
             rules (config/keywords.json): action verb + contract object + a dollar / MW / GW /
             wafer figure in the same sentence, buyer named, binding status, signal family.

Modules
  sec     SEC daily index. ALL 8-K / 6-K filings are indexed; those carrying a gated item (or none)
          are opened (main document + EX-99 / EX-10 exhibits). 10-Q / 10-K / 20-F / 40-F are read for
          the tracked universe and buyer registry (backlog, RPO, customer / supplier disclosure).
  fts     SEC full-text search on the high-yield phrases (take-or-pay, capacity reservation, PPA ...)
          so a phrase deep inside an exhibit of ANY filer is still caught.
  feeds   wire, trade-press, buyer-newsroom, government and Google News RSS items, stored whole,
          tagged, de-duplicated. Short items with a lead are opened and read (--fetch-bodies).

Output (relative to data/daily/):
  sec/<YYYY-MM-DD>/filings.jsonl     analysed filings for that FILING date
  sec/<YYYY-MM-DD>/index_summary.json form counts of the whole day's index
  sec/<YYYY-MM-DD>/_done.json         marks the day complete
  fts/<YYYY-MM-DD>.jsonl             full-text hits captured on that day (ET)
  feeds/<YYYY-MM-DD>.jsonl           feed items captured on that day (ET)
  health/<YYYY-MM-DD>.json           counts and failures (what did NOT work today)
Run log: data/run-log.csv

Usage
  python layer3_daily.py                         # last 3 business days of SEC + fts + feeds
  python layer3_daily.py --only feeds
  python layer3_daily.py --only sec --lookback 5 --sec-limit 20
"""
import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import sys
import time
import urllib.parse

import l3lib as L

DAILY = os.path.join(L.DATA, "daily")
BODY_MAX = 1_800_000
PERIODIC = {"10-Q", "10-K", "10-K/A", "10-Q/A", "20-F", "40-F", "10-KT"}
REGISTRATION = {"S-1", "S-1/A", "F-1", "F-1/A", "424B4", "424B1"}
CURRENT = {"8-K", "8-K/A", "6-K", "6-K/A"}


def keep_flag(an, th):
    """A Layer 3 signal is kept when its combination score reaches the threshold."""
    return an["score"] >= th.get("keep_min_score", 2)


def gate_relevance(row):
    """A keyword match only counts when the item is tied to a tracked company, a registry buyer,
    or a sector word - a travel company's 'bookings' or a credit agreement is not a Layer 3 signal."""
    if row.get("kept") and not (row.get("in_universe") or row.get("sector_hits") or row.get("buyers") or row.get("tickers")):
        row["kept"] = False
        row["skipped"] = "no sector or entity relevance"
    fams = {f for f in (row.get("families") or "").split("; ") if f}
    if row.get("kept") and row.get("layer4_hints") and fams <= {"buyer_supplier_deal"} and (row.get("score") or 0) < 5:
        # funding / IPO / M&A wording with a bare "agreement": a Layer 4 event, not procurement
        row["kept"] = False
        row["skipped"] = "layer 4 event (funding / M&A / IPO) without capacity, power, government or backlog content"
    return row


def finalize_feed_row(row, th):
    """Post-analysis rules for a feed row, applied every time its score changes (title/summary pass and body pass)."""
    if row.get("noise"):
        row["kept"] = False
    maxage = th.get("feed_max_age_days", 14)
    if row.get("published_et"):
        try:
            age = (L.now_et() - dt.datetime.fromisoformat(row["published_et"])).days
            row["age_days_at_capture"] = age
            if age > maxage and row.get("kept"):
                row["kept"] = False
                row["skipped"] = f"published {age} days before capture (feed history)"
        except ValueError:
            pass
    return gate_relevance(row)


def row_from_analysis(base, an, th, text_len=0):
    base.update({
        "families": "; ".join(an["families"]),
        "combos": "; ".join(an["combos"]),
        "score": an["score"],
        "binding_status": an["binding_status"],
        "buyers": "; ".join(an["buyers"]),
        "money": "; ".join(an["quantities"].get("money", [])),
        "capacity_units": "; ".join(an["quantities"].get("capacity_units", [])),
        "percent": "; ".join(an["quantities"].get("percent", [])),
        "evidence": " || ".join(an["evidence"]),
        "group_hits": "; ".join(sorted(an["group_hits"])),
        "kept": keep_flag(an, th),
    })
    # supply-constraint wording alone (shortages, lead times, allocation) is context, not a commitment:
    # stored and listed in its own weekly dataset, but not kept as a signal
    if an["families"] and set(an["families"]) <= {"supply_constraint"}:
        base["context_only"] = True
        base["kept"] = False
    if text_len:
        base["text_chars"] = text_len
    return base


# =============================================================================== SEC


class SecCollector:
    def __init__(self, universe, engine, kw, deadline, limit=None, force=False):
        self.u, self.eng, self.kw = universe, engine, kw
        self.th = kw.get("thresholds", {})
        self.deadline, self.limit, self.force = deadline, limit, force
        self.gate = set(kw["item_codes_8k"]["gate"])
        self.name_to_code = {v.lower(): k for k, v in kw["item_codes_8k"]["names"].items()}
        self.buyer_ciks = set()
        for b in L.load_buyers():
            t = (b.get("ticker") or "").strip().upper()
            c = u_cik(universe, t)
            if c:
                self.buyer_ciks.add(c)

    def _day_dir(self, d):
        return os.path.join(DAILY, "sec", d.isoformat())

    def collect_day(self, d, health):
        day = self._day_dir(d)
        done = os.path.join(day, "_done.json")
        if os.path.exists(done) and not self.force:
            health["skipped_done"].append(d.isoformat())
            return
        st, rows = L.sec_daily_index(d)
        if rows is None:
            health["no_index"].append(f"{d.isoformat()} (HTTP {st})")
            return
        counts = {}
        for r in rows:
            counts[r["form"]] = counts.get(r["form"], 0) + 1
        todo = [r for r in rows if r["form"] in CURRENT]
        periodic = [r for r in rows if r["form"] in PERIODIC and (self.u.lookup_cik(r["cik"]) or r["cik"] in self.buyer_ciks)]
        regs = [r for r in rows if r["form"] in REGISTRATION][:15]
        if self.limit:
            todo, periodic, regs = todo[: self.limit], periodic[: self.limit], regs[: min(self.limit, 5)]
        out, errs, opened = [], 0, 0
        incomplete = False
        for r in todo + periodic + regs:
            if time.time() > self.deadline:
                incomplete = True
                break
            try:
                res, did_open, err = self._process(r)
            except Exception as e:  # never stop the day for one filing
                res, did_open, err = None, False, f"{type(e).__name__}: {e}"
            if err:
                errs += 1
            if res:
                out.append(res)
            opened += int(did_open)
        L.write_jsonl(os.path.join(day, "filings.jsonl"), out)
        summary = {"index_rows_all": len(rows), "form_counts_all": dict(sorted(counts.items(), key=lambda x: -x[1])[:40]),
                   "current_reports": len(todo), "periodic_universe": len(periodic), "registrations": len(regs),
                   "rows_written": len(out), "bodies_opened": opened, "kept": sum(1 for x in out if x.get("kept")),
                   "fetch_errors": errs, "incomplete_time_budget": incomplete}
        L.save_json(os.path.join(day, "index_summary.json"), summary)
        if not incomplete:
            L.save_json(done, {"done_et": L.now_et().isoformat(timespec="seconds")})
        health["sec_days"][d.isoformat()] = summary

    # -- one filing
    def _process(self, r):
        info = self.u.lookup_cik(r["cik"])
        form = r["form"]
        urls = L.sec_filing_urls(r["cik"], r["accession"])
        base = {"filed": f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:]}", "form": form, "company": r["company"],
                "cik": r["cik"], "accession": r["accession"], "url": urls["index_html"],
                "ticker": info["ticker"] if info else "", "sectors": "; ".join(info["sectors"]) if info else "",
                "in_universe": bool(info), "source_type": "sec"}
        items_txt = ""
        if form in CURRENT:
            hdr = L.parse_sec_header(L.sec_header(r["cik"], r["accession"]))
            codes = []
            for nm in hdr.get("items", []):
                c = self.name_to_code.get(nm.strip().lower())
                if c is None:
                    for k, v in self.name_to_code.items():
                        if nm.lower().startswith(k[:25]):
                            c = v
                            break
                if c:
                    codes.append(c)
            base["items"] = "; ".join(hdr.get("items", []))
            base["item_codes"] = "; ".join(codes)
            base["sic_desc"] = hdr.get("sic_desc", "")
            # skip filings that are only governance items (5.02 director, 5.07 votes, 3.03 etc.)
            if form.startswith("8-K") and codes and not (set(codes) & self.gate):
                base.update({"skipped": "no gated item (" + ",".join(codes) + ")", "kept": False})
                return base, False, None
        text, err = self._filing_text(urls, form)
        if not text:
            base.update({"kept": False, "skipped": "no text" if not err else err})
            return base, False, err
        if self.eng.is_noise(text[:1200]):
            base.update({"kept": False, "skipped": "noise"})
            return base, True, None
        an = self.eng.analyse(text, strict=(form in PERIODIC))
        sh = self.eng.sector_hits(text[:20000])
        base["sector_hits"] = "; ".join(s for s, _n in sh[:3])
        base["mentions_tickers"] = "; ".join(self.u.match_names_in_text(text[:20000]))
        l4 = self.eng.layer4_hits(text[:6000])
        base["layer4_hints"] = "; ".join(sorted(set(l4))[:6])
        row_from_analysis(base, an, self.th, len(text))
        if form in REGISTRATION:
            # IPO paperwork is a Layer 4 event; keep only when it carries a sector-relevant supply disclosure
            base["layer4_hints"] = "; ".join(filter(None, [base.get("layer4_hints"), "ipo-registration"]))
            if not base.get("sector_hits"):
                base["kept"] = False
        if base["kept"] is False and l4 and an["score"] == 0:
            base["layer4_only"] = True
        gate_relevance(base)
        return base, True, None

    def _filing_text(self, urls, form):
        st, ij, err = L.fetch_text(urls["index_json"])
        if not ij:
            return None, err or f"index HTTP {st}"
        try:
            files = json.loads(ij)["directory"]["item"]
        except Exception as e:
            return None, f"index parse {e}"
        names = [f["name"] for f in files if f["name"].lower().endswith((".htm", ".html", ".txt"))]
        main = [n for n in names if not re.search(r"(^|[-_])ex|exhibit|index|r\d+\.htm", n.lower())]
        ex = [n for n in names if re.search(r"(^|[-_])ex-?(99|10|2)|exhibit(99|10|2)|ex99|ex10", n.lower())]
        parts = []
        pick = (main[:1] if form in PERIODIC | REGISTRATION else main[:2]) + ex[:4]
        for n in pick:
            st, body, ct, e = L.fetch(urls["folder"] + n, max_bytes=BODY_MAX, gap=0.17)
            if body:
                txt = L.strip_html(body.decode("utf-8", "ignore"))
                parts.append(txt)
        text = " ".join(parts)
        if form in PERIODIC | REGISTRATION and len(text) > 400000:
            text = self._periodic_excerpt(text)
        return text, None

    def _periodic_excerpt(self, text):
        """Keep only sentences that touch the Layer 3 vocabulary (backlog, RPO, customers, capacity ...)."""
        keys = []
        for g in ("order_backlog_terms", "supplier_disclosure_terms", "capacity_terms", "supply_constraint_terms",
                  "contract_objects", "power_terms"):
            keys += [p for p, _rx in self.eng.groups.get(g, [])]
        pat = re.compile("|".join(re.escape(k) for k in keys[:150]), re.I)
        out = [s for s in L.split_sentences(text) if pat.search(s)]
        return " ".join(out)[:300000]


def u_cik(universe, ticker):
    return universe.cik_by_ticker.get((ticker or "").upper())


# =============================================================================== FTS


def run_fts(universe, engine, kw, deadline, date, health, lookback_days=2, max_open=40):
    """EDGAR full-text search on the high-yield phrases; opens hits not already read by the sec module."""
    seen_path = os.path.join(DAILY, "fts", "_seen.json")
    seen = set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "fts", date.isoformat() + ".jsonl")
    rows = L.read_jsonl(day_path)
    start = (date - dt.timedelta(days=lookback_days)).isoformat()
    end = date.isoformat()
    forms = "8-K,6-K,10-Q,10-K,20-F,40-F,S-1,F-1,424B4"
    stats, opened = {}, 0
    th = kw.get("thresholds", {})
    known = set()
    for r in L.read_jsonl(os.path.join(DAILY, "sec", date.isoformat(), "filings.jsonl")):
        known.add(r.get("accession"))
    for q in kw.get("fts_queries", []):
        if time.time() > deadline:
            health["fts_incomplete"] = True
            break
        url = ("https://efts.sec.gov/LATEST/search-index?q=" + urllib.parse.quote(q) +
               "&dateRange=custom&startdt=" + start + "&enddt=" + end + "&forms=" + forms)
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
            adsh = src.get("adsh") or (h.get("_id", "").split(":")[0])
            fid = h.get("_id", "")
            key = fid
            if not adsh or key in seen:
                continue
            seen.add(key)
            ciks = src.get("ciks") or []
            cik = int(ciks[0]) if ciks and str(ciks[0]).isdigit() else None
            fname = fid.split(":", 1)[1] if ":" in fid else ""
            disp = (src.get("display_names") or [""])[0]
            tick_in_name = re.findall(r"\(([A-Z]{1,5}(?:\.[A-Z])?)(?:,\s*[A-Z-]+)*\)", disp)
            clean = re.sub(r"\s*\((?:CIK \d+|[A-Z0-9,\s.-]+)\)", "", disp).strip()
            row = {"captured_et": L.now_et().isoformat(timespec="seconds"), "query": q, "form": src.get("form") or src.get("file_type", ""),
                   "filed": src.get("file_date", ""), "company": clean or disp, "cik": cik, "accession": adsh,
                   "file": fname, "source_type": "sec-fts", "in_sec_module": adsh in known}
            if cik:
                nodash = adsh.replace("-", "")
                row["url"] = f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/{fname}" if fname else f"https://www.sec.gov/Archives/edgar/data/{cik}/{nodash}/"
                info = universe.lookup_cik(cik)
                row["ticker"] = info["ticker"] if info else ""
                row["sectors"] = "; ".join(info["sectors"]) if info else ""
                row["in_universe"] = bool(info)
                if not info and tick_in_name and tick_in_name[0] in universe.sectors_by_ticker:
                    row["ticker"] = tick_in_name[0]
                    row["sectors"] = "; ".join(universe.sectors_by_ticker[tick_in_name[0]])
                    row["in_universe"] = True
            if (not row["in_sec_module"]) and cik and fname and opened < max_open and time.time() < deadline:
                st2, body, ct, e2 = L.fetch(row["url"], max_bytes=BODY_MAX, gap=0.17)
                opened += 1
                if body:
                    text = L.strip_html(body.decode("utf-8", "ignore"))
                    if len(text) > 400000:
                        text = " ".join(s for s in L.split_sentences(text) if re.search(re.escape(q.strip('"').split('"')[0][:30]), s, re.I))[:300000]
                    an = engine.analyse(text)
                    row["sector_hits"] = "; ".join(s for s, _n in engine.sector_hits(text[:20000])[:3])
                    row_from_analysis(row, an, th, len(text))
                    gate_relevance(row)
                else:
                    row["kept"] = False
                    row["skipped"] = e2 or f"HTTP {st2}"
            else:
                row.setdefault("kept", False)
                row["skipped"] = "already read by sec module" if row["in_sec_module"] else "not opened (budget or no file)"
            rows.append(row)
            new += 1
        stats[q] = {"status": st, "total_hits": total, "returned": len(hits), "new": new}
    L.write_jsonl(day_path, rows)
    L.save_json(seen_path, list(seen)[-40000:])
    health["fts"] = stats
    health["fts_rows_today"] = len(rows)
    health["fts_opened"] = opened


# =============================================================================== feeds


def run_feeds(universe, engine, kw, deadline, google_window, health, date, fetch_bodies=60):
    path = os.path.join(L.CONFIG, "feeds.csv")
    if not os.path.exists(path):
        health["feeds_error"] = "config/feeds.csv missing - run tools/build_feeds.py"
        return
    feeds = [f for f in L.read_csv(path) if (f.get("enabled") or "yes").lower() == "yes"]
    disabled = [f for f in L.read_csv(path) if (f.get("enabled") or "yes").lower() != "yes"]
    th = kw.get("thresholds", {})
    seen_path = os.path.join(DAILY, "feeds", "_seen.json")
    seen = set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "feeds", date.isoformat() + ".jsonl")
    day_items = L.read_jsonl(day_path)
    fp = kw["feed_params"]
    stats, g_fail = {}, 0
    for fd in feeds:
        if time.time() > deadline:
            health["feeds_incomplete"] = True
            break
        url, ftype = fd["url"], (fd.get("type") or "rss").lower()
        if ftype == "gnews":
            if g_fail >= 3:
                stats[fd["feed_id"]] = {"skipped": "google news blocking (3 consecutive failures)"}
                continue
            url = url.replace("when%3A1d", "when%3A" + google_window)
            gap = random.uniform(fp["gap_seconds_min"], fp["gap_seconds_max"])
        else:
            gap = 0.8
        st, body, ct, err = L.fetch(url, gap=gap, timeout=40)
        if body is None and ftype != "gnews":
            time.sleep(4)
            st, body, ct, err = L.fetch(url, gap=gap, timeout=40)
        if body is None:
            stats[fd["feed_id"]] = {"status": st, "error": err}
            g_fail += 1 if ftype == "gnews" else 0
            continue
        if ftype == "gnews":
            g_fail = 0
        items = L.parse_feed(body)
        if items is None:
            stats[fd["feed_id"]] = {"status": st, "error": "not parseable as RSS/Atom (interstitial?)"}
            continue
        stale = 0
        if ftype == "gnews":
            hours = {"1d": 24, "2d": 48, "3d": 72, "7d": 168, "14d": 336, "30d": 720}.get(google_window, 24) + 6
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
            l4 = engine.layer4_hits(text)
            row = {"key": k, "captured_et": L.now_et().isoformat(timespec="seconds"), "source_type": "feed",
                   "feed_id": fd["feed_id"], "feed_name": fd["name"], "category": fd["category"], "title": it["title"],
                   "link": it["link"], "published_et": it["published_iso"], "source": it["source"], "summary": it["summary"][:500],
                   "tickers": "; ".join(universe.match_names_in_text(text)),
                   "sector_hits": "; ".join(s for s, _n in engine.sector_hits(text)[:3]),
                   "noise": noise, "layer4_hints": "; ".join(sorted(set(l4))[:6]), "body_read": False}
            row_from_analysis(row, an, th)
            finalize_feed_row(row, th)   # noise, feed-history age and relevance gates
            day_items.append(row)
            new += 1
        stats[fd["feed_id"]] = {"status": st, "items": len(items), "new": new, **({"stale_dropped": stale} if stale else {})}
        if stale and stale > len(items):
            stats[fd["feed_id"]]["warning"] = "window ignored by Google - most items were stale"

    # open the page body of leads whose title/summary did not reach the keep score
    opened = 0
    if fetch_bodies:
        cand = [r for r in day_items if not r.get("body_read") and not r.get("noise") and r["score"] < th.get("keep_min_score", 2)
                and r["category"] in ("wire", "buyer", "gov", "trade")
                and (r.get("sector_hits") or r.get("tickers") or r.get("buyers")) and r.get("group_hits")]
        random.shuffle(cand)
        for r in cand[:fetch_bodies]:
            if time.time() > deadline:
                break
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
                finalize_feed_row(r, th)
                r["body_upgraded"] = True
    L.write_jsonl(day_path, day_items)
    L.save_json(seen_path, list(seen)[-80000:])
    health["feeds"] = stats
    health["feeds_disabled_agent_covered"] = [f"{f['feed_id']}: {f['notes']}" for f in disabled]
    health["feed_items_today"] = len(day_items)
    health["feed_bodies_opened"] = opened


# =============================================================================== main


def main():
    ap = argparse.ArgumentParser(description="Layer 3 daily collector")
    ap.add_argument("--date", help="ET date (default today ET)")
    ap.add_argument("--lookback", type=int, default=3, help="business days of SEC to (re)check, default 3")
    ap.add_argument("--only", default="sec,fts,feeds")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-minutes", type=float, default=75)
    ap.add_argument("--google-window", default="1d")
    ap.add_argument("--sec-limit", type=int, help="max filings per group per day (testing)")
    ap.add_argument("--fetch-bodies", type=int, default=60)
    ap.add_argument("--fts-max-open", type=int, default=40)
    a = ap.parse_args()

    date = L.parse_date(a.date) if a.date else L.today_et()
    mods = {m.strip() for m in a.only.split(",")}
    started = time.time()
    deadline = started + a.max_minutes * 60
    health = {"run_date_et": date.isoformat(), "started_et": L.now_et().isoformat(timespec="seconds"), "modules": sorted(mods),
              "sec_days": {}, "skipped_done": [], "no_index": []}
    kw = L.load_keywords()
    universe = L.Universe()
    engine = L.SignalEngine(kw)
    health["universe_tickers"] = len(universe.sectors_by_ticker)
    health["universe_ciks"] = len(universe.ciks)

    if "sec" in mods:
        sc = SecCollector(universe, engine, kw, deadline, limit=a.sec_limit, force=a.force)
        for d in L.business_days_back(date, a.lookback):
            if time.time() > deadline:
                health["sec_time_budget_exhausted"] = True
                break
            sc.collect_day(d, health)
    if "fts" in mods:
        run_fts(universe, engine, kw, deadline, date, health, max_open=a.fts_max_open)
    if "feeds" in mods:
        run_feeds(universe, engine, kw, deadline, a.google_window, health, date, fetch_bodies=a.fetch_bodies)

    health["finished_et"] = L.now_et().isoformat(timespec="seconds")
    health["elapsed_min"] = round((time.time() - started) / 60, 1)
    L.save_json(os.path.join(DAILY, "health", date.isoformat() + ".json"), health)

    log = os.path.join(L.DATA, "run-log.csv")
    L.ensure_dir(L.DATA)
    new = not os.path.exists(log)
    with open(log, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["finished_et", "script", "date", "modules", "sec_days_done", "sec_rows", "sec_kept", "fts_rows", "feed_items_today", "elapsed_min"])
        sec_rows = sum(s["rows_written"] for s in health["sec_days"].values())
        sec_kept = sum(s["kept"] for s in health["sec_days"].values())
        w.writerow([health["finished_et"], "daily", date.isoformat(), "+".join(sorted(mods)), len(health["sec_days"]), sec_rows, sec_kept,
                    health.get("fts_rows_today", ""), health.get("feed_items_today", ""), health["elapsed_min"]])
    brief = {k: v for k, v in health.items() if k not in ("sec_days", "feeds", "fts", "feeds_disabled_agent_covered")}
    print(json.dumps(brief, indent=1))
    for day, s in health["sec_days"].items():
        print(day, s["index_rows_all"], "index rows;", s["rows_written"], "written;", s["kept"], "kept;", "errors:", s["fetch_errors"])
    if "feeds" in health:
        bad = {k: v for k, v in health["feeds"].items() if "error" in v or "skipped" in v}
        print("feeds ok:", len(health["feeds"]) - len(bad), "failed:", bad)
    if "fts" in health:
        bad = {k: v for k, v in health["fts"].items() if "error" in v}
        print("fts queries ok:", len(health["fts"]) - len(bad), "failed:", bad)


if __name__ == "__main__":
    main()
