#!/usr/bin/env python3
"""Reachability test for every static Layer 2 source (feeds, SEC, ATS job boards, free hiring datasets).

Writes analysis/source-reachability.json and config/feed-overrides.json (feeds that fail get
enabled=no and are left to the agent routines). Run again whenever feeds are added.

Usage: python tools/test_sources.py [--only feeds|sec|ats|datasets]
"""
import argparse
import datetime as dt
import json
import os
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
sys.path.insert(0, HERE)
import l2lib as L  # noqa: E402
import build_feeds as B  # noqa: E402

ATS = [
    ("greenhouse", "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs", "json", "jobs"),
    ("greenhouse-content", "https://boards-api.greenhouse.io/v1/boards/anthropic/jobs?content=true", "json", "jobs"),
    ("lever", "https://api.lever.co/v0/postings/palantir?mode=json", "json", None),
    ("ashby", "https://api.ashbyhq.com/posting-api/job-board/openai", "json", "jobs"),
    ("workable", "https://apply.workable.com/api/v1/widget/accounts/workable", "json", "jobs"),
    ("smartrecruiters", "https://api.smartrecruiters.com/v1/companies/bosch/postings?limit=5", "json", "content"),
]

DATASETS = [
    ("indeed-ai-tracker", "https://raw.githubusercontent.com/hiring-lab/ai-tracker/main/README.md", "text"),
    ("indeed-postings-index", "https://raw.githubusercontent.com/hiring-lab/job_postings_tracker/master/README.md", "text"),
    ("revelio-rpls", "https://www.reveliolabs.com/public-labor-statistics/", "text"),
    ("qed-c-workforce", "https://quantumconsortium.org/blog/quantum-technology-workforce-monitoring-report-3/", "text"),
    ("linkedin-economic-graph", "https://economicgraph.linkedin.com/workforce-data/publications", "text"),
    ("dol-oflc-performance", "https://www.dol.gov/agencies/eta/foreign-labor/performance", "text"),
    ("bls-jolts", "https://www.bls.gov/jlt/", "text"),
]


def test_feeds(res, overrides):
    for fid, name, cat, url, note in B.STATIC:
        st, body, ct, err = L.fetch(url, timeout=30, retries=1)
        r = {"name": name, "status": st, "error": err}
        if body is not None:
            items = L.parse_feed(body)
            if items is None:
                r["error"] = "not parseable as RSS/Atom (interstitial?)"
            else:
                r["items"] = len(items)
                r["newest"] = max((i.get("published_iso", "") for i in items), default="")
        ok = body is not None and "items" in r and r["items"] > 0
        r["ok"] = ok
        res["feeds"][fid] = r
        if not ok:
            overrides[fid] = {"enabled": "no", "note": f"failed {dt.date.today()}: {r['status']} {r.get('error') or 'empty'}; agents cover"}
        print(f"{'OK  ' if ok else 'FAIL'} {fid:24s} status={st} items={r.get('items')} {r.get('error') or ''}")


def test_sec(res):
    d = L.today_et()
    for back in range(1, 6):
        day = d - dt.timedelta(days=back)
        if day.weekday() >= 5:
            continue
        st, rows = L.sec_daily_index(day)
        if rows:
            eight = [r for r in rows if r["form"] in ("8-K", "6-K")]
            res["sec"]["daily_index"] = {"date": str(day), "status": st, "rows": len(rows), "8k_6k": len(eight)}
            print(f"OK   SEC daily index {day}: {len(rows)} rows, {len(eight)} 8-K/6-K")
            # open a few 8-K headers and look for Item 5.02
            n502, tested = 0, 0
            for r in eight[:40]:
                h = L.sec_header(r["cik"], r["accession"])
                it = L.parse_sec_header(h).get("items", [])
                tested += 1
                if any(x.lower().startswith("departure of directors or certain officers") for x in it):
                    n502 += 1
            res["sec"]["item_5_02_in_first_40"] = {"tested": tested, "with_5_02": n502}
            print(f"OK   SEC headers: {n502} of {tested} 8-K/6-K carry Item 5.02")
            break
    else:
        res["sec"]["daily_index"] = {"error": "no recent index reachable"}
        print("FAIL SEC daily index")
    q = '"appointed" "Chief Technology Officer" "formerly"'
    import urllib.parse
    url = "https://efts.sec.gov/LATEST/search-index?q=" + urllib.parse.quote(q) + "&forms=8-K&dateRange=custom&startdt=" + str(d - dt.timedelta(days=14)) + "&enddt=" + str(d)
    st, txt, err = L.fetch_text(url)
    hits = None
    if txt:
        try:
            hits = json.loads(txt).get("hits", {}).get("total", {}).get("value")
        except Exception:
            hits = None
    res["sec"]["full_text_search"] = {"status": st, "hits": hits, "error": err}
    print(f"{'OK  ' if hits is not None else 'FAIL'} SEC full-text search: status={st} hits={hits}")


def test_ats(res):
    for name, url, kind, key in ATS:
        st, body, ct, err = L.fetch(url, timeout=30, retries=1)
        r = {"status": st, "error": err}
        if body is not None:
            try:
                js = json.loads(body.decode("utf-8", "ignore"))
                n = len(js) if isinstance(js, list) else len(js.get(key, [])) if key else None
                r["items"] = n
                if isinstance(js, dict) and "meta" in js:
                    r["total"] = js["meta"].get("total")
                r["ok"] = bool(n)
            except Exception as e:
                r["error"] = f"bad json: {e}"
                r["ok"] = False
        else:
            r["ok"] = False
        res["ats"][name] = r
        print(f"{'OK  ' if r['ok'] else 'FAIL'} ATS {name:20s} status={st} items={r.get('items')} {r.get('error') or ''}")


def test_datasets(res):
    for name, url, kind in DATASETS:
        st, txt, err = L.fetch_text(url, timeout=30, retries=1)
        r = {"status": st, "error": err, "bytes": len(txt) if txt else 0, "ok": bool(txt) and st == 200}
        res["datasets"][name] = r
        print(f"{'OK  ' if r['ok'] else 'FAIL'} DATA {name:26s} status={st} bytes={r['bytes']} {err or ''}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", choices=["feeds", "sec", "ats", "datasets"])
    a = ap.parse_args()
    out_path = os.path.join(ROOT, "analysis", "source-reachability.json")
    res = L.load_json(out_path, {}) or {}
    for k in ("feeds", "sec", "ats", "datasets"):
        res.setdefault(k, {})
    res["tested_at_et"] = L.now_et().isoformat(timespec="seconds")
    res["tested_from"] = "workstation (re-run from GitHub Actions to confirm)"
    ov_path = os.path.join(L.CONFIG, "feed-overrides.json")
    overrides = {} if a.only in (None, "feeds") else (L.load_json(ov_path, {}) or {})
    if a.only in (None, "feeds"):
        test_feeds(res, overrides)
        L.save_json(ov_path, overrides)
    if a.only in (None, "sec"):
        test_sec(res)
    if a.only in (None, "ats"):
        test_ats(res)
    if a.only in (None, "datasets"):
        test_datasets(res)
    L.save_json(out_path, res)


if __name__ == "__main__":
    main()
