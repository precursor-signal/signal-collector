#!/usr/bin/env python3
"""Layer 2 DAILY collector - human capital / talent-flow signals. Capture only, no judgement.

Two layers of capture run together:
  WHOLESALE  every filing / feed item that is reachable is stored with its date, source and link
             before any filter is applied (nothing is dropped because a keyword did not match);
  KEYWORD    every stored item is then analysed with the Layer 2 vocabulary and combination
             rules (config/keywords.json): appointment / departure verb + senior title + a person
             name + a pedigree organisation in the same sentence, board and academic moves,
             departures to found something, hiring-surge wording.

Modules
  sec     SEC daily index. 8-K filings carrying Item 5.02 (departure of directors or certain
          officers, election of directors, appointment of certain officers) are opened with their
          EX-99 press release; 6-K filings of tracked companies are opened; Form 3 (initial
          statement of ownership: a new officer or director) is read for tracked companies.
  fts     SEC full-text search on the high-yield talent phrases so a hire written up inside an
          exhibit of ANY filer is still caught.
  feeds   wire, trade-press, people-move columns, company newsrooms and Google News RSS items,
          stored whole, tagged, de-duplicated. Short items with a lead are opened and read.

Output (relative to data/daily/):
  sec/<YYYY-MM-DD>/filings.jsonl      analysed filings for that FILING date
  sec/<YYYY-MM-DD>/index_summary.json form counts of the whole day's index
  sec/<YYYY-MM-DD>/_done.json         marks the day complete
  fts/<YYYY-MM-DD>.jsonl              full-text hits captured on that day (ET)
  feeds/<YYYY-MM-DD>.jsonl            feed items captured on that day (ET)
  health/<YYYY-MM-DD>.json            counts and failures (what did NOT work today)
Run log: data/run-log.csv

Usage
  python layer2_daily.py                         # last 3 business days of SEC + fts + feeds
  python layer2_daily.py --only feeds
  python layer2_daily.py --only sec --lookback 5 --sec-limit 20
"""
import argparse
import csv
import datetime as dt
import json
import os
import random
import re
import time
import urllib.parse
import xml.etree.ElementTree as ET

import l2lib as L

DAILY = os.path.join(L.DATA, "daily")
BODY_MAX = 1_800_000
CURRENT = {"8-K", "8-K/A", "6-K", "6-K/A"}
INSIDER = {"3", "3/A"}
BODY_CATEGORIES = ("wire", "people-moves", "trade", "news", "newsroom", "aggregator")


class PrivateMatcher:
    """Private watchlist names (company + aliases) found in a text."""

    def __init__(self):
        self.items = []
        p = os.path.join(L.CONFIG, "private-watchlist.csv")
        for r in (L.read_csv(p) if os.path.exists(p) else []):
            names = [r["company"]] + [a for a in (r.get("aliases") or "").split(";") if a.strip()]
            pats = []
            for n in names:
                n = n.strip()
                if len(n) < 4:
                    continue
                pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])", re.I if len(n) > 6 else 0))
            if pats:
                self.items.append((r["company"], r.get("sector", ""), pats))

    def match(self, text):
        return [(c, s) for c, s, pats in self.items if any(p.search(text) for p in pats)]


def keep_flag(an, th):
    """A Layer 2 signal is kept when its combination score reaches the threshold."""
    return an["score"] >= th.get("keep_min_score", 3)


def gate_relevance(row):
    """A keyword match only counts when the item is tied to a tracked company, a private-watchlist
    company, a sector word, or a pedigree organisation - a regional bank's new CFO is not a Layer 2 signal."""
    if row.get("kept") and not (row.get("in_universe") or row.get("sector_hits") or row.get("private_matches")
                                or row.get("tickers") or "pedigree_orgs" in (row.get("group_hits") or "")):
        row["kept"] = False
        row["skipped"] = "no sector, entity or pedigree relevance"
    return row


def finalize_feed_row(row, th):
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
    gh = an["group_hits"]
    base.update({
        "families": "; ".join(an["families"]),
        "combos": "; ".join(an["combos"]),
        "score": an["score"],
        "persons": "; ".join(an["persons"][:6]),
        "titles": "; ".join(sorted(set(gh.get("exec_titles", []) + gh.get("tech_titles", []) + gh.get("board_terms", [])))[:8]),
        "pedigree_orgs": "; ".join(gh.get("pedigree_orgs", [])[:8]),
        "evidence": " || ".join(an["evidence"]),
        "group_hits": "; ".join(sorted(gh)),
        "low_value_title": an["low_value_title"],
        "kept": keep_flag(an, th),
    })
    if text_len:
        base["text_chars"] = text_len
    return base


# =============================================================================== SEC


class SecCollector:
    def __init__(self, universe, engine, kw, private, deadline, limit=None, force=False, six_k_limit=60):
        self.u, self.eng, self.kw, self.private = universe, engine, kw, private
        self.th = kw.get("thresholds", {})
        self.deadline, self.limit, self.force, self.six_k_limit = deadline, limit, force, six_k_limit
        self.gate = set(kw["sec_item_5_02"]["gate"])
        self.name_to_code = {v.lower(): k for k, v in kw["sec_item_5_02"]["names"].items()}

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
        eight = [r for r in rows if r["form"] in ("8-K", "8-K/A")]
        six = [r for r in rows if r["form"] in ("6-K", "6-K/A")]
        six = [r for r in six if self.u.lookup_cik(r["cik"])] + [r for r in six if not self.u.lookup_cik(r["cik"])][: self.six_k_limit]
        insiders = [r for r in rows if r["form"] in INSIDER and self.u.lookup_cik(r["cik"])]
        if self.limit:
            eight, six, insiders = eight[: self.limit], six[: self.limit], insiders[: self.limit]
        out, errs, opened = [], 0, 0
        incomplete = False
        for r in eight + six + insiders:
            if time.time() > self.deadline:
                incomplete = True
                break
            try:
                if r["form"] in INSIDER:
                    res, did_open, err = self._process_insider(r)
                else:
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
                   "current_reports": len(eight) + len(six), "insider_forms_tracked": len(insiders),
                   "rows_written": len(out), "bodies_opened": opened, "kept": sum(1 for x in out if x.get("kept")),
                   "fetch_errors": errs, "incomplete_time_budget": incomplete}
        L.save_json(os.path.join(day, "index_summary.json"), summary)
        if not incomplete:
            L.save_json(done, {"done_et": L.now_et().isoformat(timespec="seconds")})
        health["sec_days"][d.isoformat()] = summary

    def _base(self, r, urls):
        info = self.u.lookup_cik(r["cik"])
        return {"filed": f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:]}", "form": r["form"], "company": r["company"],
                "cik": r["cik"], "accession": r["accession"], "url": urls["index_html"],
                "ticker": info["ticker"] if info else "", "sectors": "; ".join(info["sectors"]) if info else "",
                "in_universe": bool(info), "source_type": "sec"}

    # -- one 8-K / 6-K
    def _process(self, r):
        form = r["form"]
        urls = L.sec_filing_urls(r["cik"], r["accession"])
        base = self._base(r, urls)
        ctx = {}
        if form.startswith("8-K"):
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
            if not (set(codes) & self.gate):
                base.update({"skipped": "no Item 5.02", "kept": False})
                return base, False, None
            ctx["item502"] = True
            base["item_5_02"] = True
        text, err = self._filing_text(urls)
        if not text:
            base.update({"kept": False, "skipped": "no text" if not err else err})
            return base, False, err
        if self.eng.is_noise(text[:1200]):
            base.update({"kept": False, "skipped": "noise"})
            return base, True, None
        if ctx.get("item502"):
            i502 = text.lower().find("item 5.02")
            if i502 > 0:
                text = text[i502:]          # drop the cover page (address, 'emerging growth company' check boxes ...)
        # the standard Item 5.02 heading names "departure / election / appointment of ... officers"; it is the form's title, not content
        text = re.sub(r"(?i)departure of directors or certain officers[;:,]?\s*election of directors[;:,]?\s*appointment of certain officers[;:,]?\s*compensatory arrangements of certain officers\.?", " ", text)
        an = self.eng.analyse(text, ctx)
        sh = self.eng.sector_hits((" ".join(an["evidence"]) + " " + text[:3000]))
        base["sector_hits"] = "; ".join(s for s, _n in sh[:3])
        base["mentions_tickers"] = "; ".join(self.u.match_names_in_text(text[:20000]))
        base["private_matches"] = "; ".join(c for c, _s in self.private.match(text[:20000])[:4])
        row_from_analysis(base, an, self.th, len(text))
        if ctx.get("item502") and not base["kept"] and an["score"] >= 1:
            base["note"] = "Item 5.02 filing, below keep score: officer / director change wording not recognised - agents may read it"
        gate_relevance(base)
        return base, True, None

    # -- one Form 3 (initial statement of ownership = a new officer or director)
    def _process_insider(self, r):
        urls = L.sec_filing_urls(r["cik"], r["accession"])
        base = self._base(r, urls)
        st, ij, err = L.fetch_text(urls["index_json"])
        if not ij:
            base.update({"kept": False, "skipped": err or f"index HTTP {st}"})
            return base, False, err
        try:
            files = [f["name"] for f in json.loads(ij)["directory"]["item"]]
        except Exception as e:
            base.update({"kept": False, "skipped": f"index parse {e}"})
            return base, False, str(e)
        xmls = [n for n in files if n.lower().endswith(".xml") and not n.lower().startswith(("xsl", "primary_doc.xsl"))]
        if not xmls:
            base.update({"kept": False, "skipped": "no xml"})
            return base, False, None
        st, body, ct, e = L.fetch(urls["folder"] + xmls[0], max_bytes=400000, gap=0.17)
        if not body:
            base.update({"kept": False, "skipped": e or f"HTTP {st}"})
            return base, False, e
        try:
            root = ET.fromstring(body.decode("utf-8", "ignore"))
        except Exception as ex:
            base.update({"kept": False, "skipped": f"xml parse {ex}"})
            return base, True, None

        def tx(path, node=root):
            el = node.find(path)
            return (el.text or "").strip() if el is not None and el.text else ""
        issuer = tx("issuer/issuerName")
        symbol = tx("issuer/issuerTradingSymbol")
        owners = []
        for ro in root.findall("reportingOwner"):
            rel = ro.find("reportingOwnerRelationship")
            is_dir = (tx("isDirector", rel) if rel is not None else "") in ("1", "true")
            is_off = (tx("isOfficer", rel) if rel is not None else "") in ("1", "true")
            owners.append({"name": tx("reportingOwnerId/rptOwnerName", ro), "director": is_dir, "officer": is_off,
                           "title": tx("officerTitle", rel) if rel is not None else ""})
        person_rows = [o for o in owners if o["director"] or o["officer"]]
        base.update({"issuer": issuer, "issuer_symbol": symbol, "owners": json.dumps(owners)[:600],
                     "persons": "; ".join(o["name"] for o in person_rows),
                     "titles": "; ".join(o["title"] or ("Director" if o["director"] else "") for o in person_rows),
                     "combos": "form3_new_insider", "families": "board_move" if any(o["director"] for o in person_rows) and not any(o["officer"] for o in person_rows) else "leadership_move",
                     "evidence": f"Form 3 (initial statement of ownership): " + "; ".join(
                         f"{o['name']} - {o['title'] or ('director' if o['director'] else 'insider')}" for o in person_rows) + f" at {issuer} ({symbol})",
                     "note": "A Form 3 is also filed when a company IPOs or a person re-files; check whether this is a genuinely new officer or director."})
        base["score"] = 3 if person_rows else 0
        base["kept"] = bool(person_rows)
        if not person_rows:
            base["skipped"] = "reporting owner is neither officer nor director (10% holder only)"
        return base, True, None

    def _filing_text(self, urls):
        st, ij, err = L.fetch_text(urls["index_json"])
        if not ij:
            return None, err or f"index HTTP {st}"
        try:
            files = json.loads(ij)["directory"]["item"]
        except Exception as e:
            return None, f"index parse {e}"
        names = [f["name"] for f in files if f["name"].lower().endswith((".htm", ".html", ".txt"))]
        main = [n for n in names if not re.search(r"(^|[-_])ex|exhibit|index|r\d+\.htm", n.lower())]
        ex = [n for n in names if re.search(r"(^|[-_])ex-?(99|10)|exhibit(99|10)|ex99|ex10", n.lower())]
        parts = []
        for n in main[:2] + ex[:3]:
            st, body, ct, e = L.fetch(urls["folder"] + n, max_bytes=BODY_MAX, gap=0.17)
            if body:
                parts.append(L.strip_html(body.decode("utf-8", "ignore")))
        return " ".join(parts), None


# =============================================================================== FTS


def run_fts(universe, engine, kw, private, deadline, date, health, lookback_days=2, max_open=40):
    """EDGAR full-text search on the high-yield phrases; opens hits not already read by the sec module."""
    seen_path = os.path.join(DAILY, "fts", "_seen.json")
    seen = set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "fts", date.isoformat() + ".jsonl")
    rows = L.read_jsonl(day_path)
    start = (date - dt.timedelta(days=lookback_days)).isoformat()
    end = date.isoformat()
    forms = "8-K,6-K,10-Q,10-K,20-F,40-F"
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
            if not adsh or fid in seen:
                continue
            seen.add(fid)
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
                        text = " ".join(s for s in L.split_sentences(text) if re.search(r"appoint|joined|named|resign|depart|former", s, re.I))[:300000]
                    an = engine.analyse(text)
                    row["sector_hits"] = "; ".join(s for s, _n in engine.sector_hits(text[:20000])[:3])
                    row["private_matches"] = "; ".join(c for c, _s in private.match(text[:20000])[:4])
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


def run_feeds(universe, engine, kw, private, deadline, google_window, health, date, fetch_bodies=60, fresh=False):
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
            row = {"key": k, "captured_et": L.now_et().isoformat(timespec="seconds"), "source_type": "feed",
                   "feed_id": fd["feed_id"], "feed_name": fd["name"], "category": fd["category"], "title": it["title"],
                   "link": it["link"], "published_et": it["published_iso"], "source": it["source"], "summary": it["summary"][:500],
                   "tickers": "; ".join(universe.match_names_in_text(text)),
                   "sector_hits": "; ".join(s for s, _n in engine.sector_hits(text)[:3]),
                   "private_matches": "; ".join(c for c, _s in private.match(text)[:3]),
                   "noise": noise, "body_read": False}
            row_from_analysis(row, an, th)
            finalize_feed_row(row, th)
            day_items.append(row)
            new += 1
        stats[fd["feed_id"]] = {"status": st, "items": len(items), "new": new, **({"stale_dropped": stale} if stale else {})}
        if stale and stale > len(items):
            stats[fd["feed_id"]]["warning"] = "window ignored by Google - most items were stale"

    # open the page body of leads whose title/summary did not reach the keep score
    opened = 0
    if fetch_bodies:
        def lead(r):
            gh = r.get("group_hits") or ""
            moved = any(g in gh for g in ("move_appoint", "move_depart", "found_launch"))
            titled = any(g in gh for g in ("exec_titles", "tech_titles", "board_terms"))
            return moved and titled and "funding_terms" not in gh
        cand = [r for r in day_items if not r.get("body_read") and not r.get("noise") and r["score"] < th.get("keep_min_score", 3)
                and r["category"] in BODY_CATEGORIES and lead(r)]
        # leads that already show a sector, tracked company or pedigree organisation are read first
        cand.sort(key=lambda r: -(bool(r.get("sector_hits")) + bool(r.get("tickers")) + bool(r.get("private_matches")) + bool(r.get("pedigree_orgs"))
                                  + random.random() * 0.5))
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
    ap = argparse.ArgumentParser(description="Layer 2 daily collector")
    ap.add_argument("--date", help="ET date (default today ET)")
    ap.add_argument("--lookback", type=int, default=3, help="business days of SEC to (re)check, default 3")
    ap.add_argument("--only", default="sec,fts,feeds")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--max-minutes", type=float, default=75)
    ap.add_argument("--google-window", default="1d")
    ap.add_argument("--sec-limit", type=int, help="max filings per group per day (testing)")
    ap.add_argument("--fetch-bodies", type=int, default=60)
    ap.add_argument("--fts-max-open", type=int, default=40)
    ap.add_argument("--fresh", action="store_true", help="feeds: ignore the seen-items list and start the day's file anew (testing / re-runs)")
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
    private = PrivateMatcher()
    health["universe_tickers"] = len(universe.sectors_by_ticker)
    health["universe_ciks"] = len(universe.ciks)
    health["private_watchlist"] = len(private.items)

    if "sec" in mods:
        sc = SecCollector(universe, engine, kw, private, deadline, limit=a.sec_limit, force=a.force)
        for d in L.business_days_back(date, a.lookback):
            if time.time() > deadline:
                health["sec_time_budget_exhausted"] = True
                break
            sc.collect_day(d, health)
    if "fts" in mods:
        run_fts(universe, engine, kw, private, deadline, date, health, max_open=a.fts_max_open)
    if "feeds" in mods:
        run_feeds(universe, engine, kw, private, deadline, a.google_window, health, date, fetch_bodies=a.fetch_bodies, fresh=a.fresh)

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
