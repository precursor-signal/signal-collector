#!/usr/bin/env python3
"""Find which public job-board system (Greenhouse, Lever, Ashby) each tracked company uses.

Input : config/private-watchlist.csv (private companies) + config/ticker-sectors.csv joined to SEC
        company names (public companies) + config/ats-seed.csv (hand-checked tokens, optional).
Output: config/ats-companies.csv   one row per company that has a live public board.

A board is accepted only when the slug is an exact, normalised form of the company name (or a seed
row, or the Greenhouse response itself names the company). That keeps look-alike boards out; the
agents (routine 03) cover everything this tool cannot map. Re-run any time; existing verified rows
are kept and only unmapped companies are probed again (use --recheck to probe all).

Usage: python tools/discover_ats.py [--limit N] [--recheck] [--workers 6]
"""
import argparse
import concurrent.futures as cf
import json
import os
import re
import sys
import threading

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import l2lib as L  # noqa: E402

OUT = os.path.join(L.CONFIG, "ats-companies.csv")
FIELDS = ["company", "ticker", "private", "sectors", "ats", "token", "status", "open_roles_at_discovery", "discovered_on", "notes"]
SUFFIX = r"\b(inc|incorporated|corp|corporation|co|company|ltd|limited|llc|lp|plc|holdings?|group|technologies|technology|systems|pbc|sa|ag|nv|gmbh)\b"

lock = threading.Lock()


def slug_variants(name, ticker=""):
    base = re.sub(r"[^a-z0-9 &\-]", " ", name.lower().replace("&", " and "))
    core = re.sub(SUFFIX, " ", base)
    core = re.sub(r"\s+", " ", core).strip(" -")
    full = re.sub(r"\s+", " ", base).strip(" -")
    out = []
    for s in (core, full):
        if not s:
            continue
        for v in (s.replace(" ", ""), s.replace(" ", "-"), s.replace(" ", "_")):
            if v and v not in out and len(v) >= 3:
                out.append(v)
    return out


def probe(ats, slug):
    """Return (job_count, company_name_or_None) for a live board, else (0, None)."""
    if ats == "greenhouse":
        st, body, _ct, _e = L.fetch(f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs", timeout=25, retries=0, gap=0.15)
        if body is None:
            return 0, None
        try:
            js = json.loads(body.decode("utf-8", "ignore"))
        except Exception:
            return 0, None
        jobs = js.get("jobs") or []
        nm = (jobs[0].get("company_name") if jobs and isinstance(jobs[0], dict) else None)
        return len(jobs), nm
    if ats == "lever":
        st, body, _ct, _e = L.fetch(f"https://api.lever.co/v0/postings/{slug}?mode=json", timeout=25, retries=0, gap=0.15)
        if body is None:
            return 0, None
        try:
            js = json.loads(body.decode("utf-8", "ignore"))
        except Exception:
            return 0, None
        return (len(js), None) if isinstance(js, list) else (0, None)
    if ats == "ashby":
        st, body, _ct, _e = L.fetch(f"https://api.ashbyhq.com/posting-api/job-board/{slug}", timeout=25, retries=0, gap=0.15)
        if body is None:
            return 0, None
        try:
            js = json.loads(body.decode("utf-8", "ignore"))
        except Exception:
            return 0, None
        return len(js.get("jobs") or []), None
    return 0, None


def candidates():
    cands = {}
    for r in L.read_csv(os.path.join(L.CONFIG, "private-watchlist.csv")):
        cands[r["company"]] = {"company": r["company"], "ticker": "", "private": "yes", "sectors": r.get("sector", ""),
                               "aliases": [a.strip() for a in (r.get("aliases") or "").split(";") if a.strip()]}
    uni = L.Universe()
    for t, secs in uni.sectors_by_ticker.items():
        cik = uni.cik_by_ticker.get(t)
        title = uni.tickers_meta.get(cik, {}).get("title", "") if cik else ""
        if title:
            cands[title] = {"company": title, "ticker": t, "private": "no", "sectors": "; ".join(secs), "aliases": []}
    return list(cands.values())


def extended_variants(name, ticker=""):
    """Looser slugs, probed on Greenhouse only (its response names the company, so a wrong board is rejected)."""
    base = re.sub(r"[^a-z0-9 ]", " ", name.lower())
    core = re.sub(SUFFIX, " ", base)
    words = [w for w in core.split() if w]
    out = []
    if words and len(words[0]) >= 5:
        out.append(words[0])
    if words:
        out += [words[0] + "hq", "".join(words) + "inc", "".join(words) + "co", "".join(words) + "ai", "".join(words) + "labs"]
    if ticker and len(ticker) >= 3 and "." not in ticker:
        out.append(ticker.lower())
    return [v for v in dict.fromkeys(out) if len(v) >= 3]


def work(c):
    names = [c["company"]] + c["aliases"]
    seen = set()
    for nm in names:
        for slug in slug_variants(nm, c["ticker"]):
            if slug in seen:
                continue
            seen.add(slug)
            for ats in ("greenhouse", "lever", "ashby"):
                n, boardname = probe(ats, slug)
                if n > 0:
                    # accept: exact slug variant of the name (already guaranteed), and for Greenhouse the board's own company name must agree
                    if ats == "greenhouse" and boardname:
                        a = re.sub(r"[^a-z0-9]", "", boardname.lower())
                        b = re.sub(r"[^a-z0-9]", "", c["company"].lower())
                        ok = a[:6] == b[:6] or a in b or b in a
                        if not ok:
                            continue
                    return {**{k: c[k] for k in ("company", "ticker", "private", "sectors")}, "ats": ats, "token": slug,
                            "status": "active", "open_roles_at_discovery": n, "discovered_on": str(L.today_et()),
                            "notes": f"board name: {boardname}" if boardname else ""}
    for slug in extended_variants(c["company"], c["ticker"]):
        if slug in seen:
            continue
        seen.add(slug)
        n, boardname = probe("greenhouse", slug)
        if n > 0 and boardname:
            a = re.sub(r"[^a-z0-9]", "", boardname.lower())
            b = re.sub(r"[^a-z0-9]", "", c["company"].lower())
            if a[:6] == b[:6] or a in b or b in a:
                return {**{k: c[k] for k in ("company", "ticker", "private", "sectors")}, "ats": "greenhouse", "token": slug,
                        "status": "active", "open_roles_at_discovery": n, "discovered_on": str(L.today_et()),
                        "notes": f"board name: {boardname} (extended slug)"}
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int)
    ap.add_argument("--recheck", action="store_true")
    ap.add_argument("--workers", type=int, default=6)
    a = ap.parse_args()
    existing = {}
    if os.path.exists(OUT) and not a.recheck:
        for r in L.read_csv(OUT):
            existing[r["company"]] = r
    seed_path = os.path.join(L.CONFIG, "ats-seed.csv")
    if os.path.exists(seed_path):
        for r in L.read_csv(seed_path):
            existing[r["company"]] = {**{f: "" for f in FIELDS}, **r, "status": "active", "discovered_on": str(L.today_et()), "notes": "seed (hand-checked)"}
    todo = [c for c in candidates() if c["company"] not in existing]
    if a.limit:
        todo = todo[: a.limit]
    print(f"{len(existing)} already mapped, probing {len(todo)} companies with {a.workers} workers")
    found = 0
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        for i, res in enumerate(ex.map(work, todo), 1):
            if res:
                with lock:
                    existing[res["company"]] = res
                found += 1
                print(f"  + {res['company']:40s} {res['ats']:10s} {res['token']:28s} {res['open_roles_at_discovery']} roles")
            if i % 100 == 0:
                print(f"  ... {i}/{len(todo)} probed, {found} found")
                L.write_csv(OUT, sorted(existing.values(), key=lambda r: r["company"].lower()), FIELDS)
    uniq = {}
    for r in sorted(existing.values(), key=lambda r: (r.get("notes", "").startswith("seed") is False, r["company"].lower())):
        uniq.setdefault((r["ats"], r["token"]), r)
    existing = {r["company"]: r for r in uniq.values()}
    L.write_csv(OUT, sorted(existing.values(), key=lambda r: r["company"].lower()), FIELDS)
    by = {}
    for r in existing.values():
        by[r["ats"]] = by.get(r["ats"], 0) + 1
    print(f"done: {len(existing)} companies mapped; by ATS {by}")


if __name__ == "__main__":
    main()
