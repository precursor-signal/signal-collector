#!/usr/bin/env python3
"""Fill config/entities.csv `cik` for funds / VCs / corporates / individuals.

Sources (all free):
  * corporates with a ticker -> SEC company_tickers_exchange.json (cik_status = sec-ticker)
  * funds / VCs / sovereigns / individuals -> EDGAR full-text search over recent 13F-HR / SC 13D/G / Form 4 filings,
    taking the best name match among the `display_names` returned (cik_status = auto-unverified)

Every non-ticker CIK stays "auto-unverified" until a person confirms it. Candidates are written to
config/cik-candidates.csv so a reviewer can pick the right filer when several look similar.

  python tools/resolve_ciks.py            # only entities with an empty cik
  python tools/resolve_ciks.py --all      # re-resolve everything not marked verified
"""
import argparse
import csv
import datetime as dt
import json
import os
import re
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import l4lib as L  # noqa: E402


def toks(s):
    s = re.sub(r"\([^)]*\)", " ", s.upper())
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    stop = {"LLC", "LP", "L", "P", "INC", "CORP", "CORPORATION", "CO", "LTD", "THE", "OF", "AND", "GROUP", "PARTNERS",
            "MANAGEMENT", "CAPITAL", "INVESTMENT", "INVESTMENTS", "ADVISORS", "FUND", "HOLDINGS", "COMPANY", "CIK"}
    return {t for t in s.split() if t not in stop and len(t) > 1}


def score(query_names, cand):
    ct = toks(cand)
    best = 0.0
    for q in query_names:
        qt = toks(q)
        if not qt or not ct:
            continue
        inter = len(qt & ct)
        best = max(best, inter / max(len(qt), 1) * (inter / max(len(ct), 1)) ** 0.5)
    return best


def fts_candidates(query, forms):
    end = L.today_et()
    start = end - dt.timedelta(days=540)
    url = ("https://efts.sec.gov/LATEST/search-index?q=" + urllib.parse.quote(f'"{query}"') +
           "&forms=" + urllib.parse.quote(forms) + f"&dateRange=custom&startDate={start}&endDate={end}")
    st, txt, err = L.fetch_text(url)
    if not txt:
        return []
    try:
        hits = json.loads(txt)["hits"]["hits"]
    except Exception:
        return []
    out = {}
    for h in hits:
        src = h.get("_source", {})
        for dn, ck in zip(src.get("display_names", []), src.get("ciks", [])):
            name = re.sub(r"\s*\(CIK[^)]*\)", "", dn).strip()
            out[ck.lstrip("0")] = name
    return [(c, n) for c, n in out.items()]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--all", action="store_true")
    a = ap.parse_args()
    path = os.path.join(L.CONFIG, "entities.csv")
    rows = L.read_csv(path)
    fields = list(rows[0].keys())
    tick = L.sec_company_tickers()
    by_ticker = {}
    for cik, m in tick.items():
        by_ticker.setdefault(m["ticker"].upper(), cik)
    cand_rows = []
    for r in rows:
        if r["cik"] and not a.all:
            continue
        if r.get("cik_status") == "verified":
            continue
        typ = r["type"]
        if typ in ("government", "politician") or r["tier"] in ("committee", "executive-branch"):
            continue
        names = [r["name"]] + [x for x in (r["aliases"] or "").split(";") if x.strip()]
        if typ == "corporate" and r.get("ticker") and r["ticker"].upper() in by_ticker:
            r["cik"] = str(by_ticker[r["ticker"].upper()])
            r["cik_status"] = "sec-ticker"
            continue
        person = typ in ("executive", "individual") and "sec_13f" not in r["watch_sources"]
        order = ["4"] if person else ["13F-HR", "SC 13D,SC 13G,SCHEDULE 13D,SCHEDULE 13G"]
        found = {}
        for forms in order:
            for q in [names[0]] + [n for n in names[1:3] if len(n) > 6]:
                if len(q) < 4:
                    continue
                for cik, nm in fts_candidates(q, forms):
                    found.setdefault(cik, nm)
            # accept the first pass if it already has a strong match on the primary name
            if any(score([names[0]], nm) >= 0.75 for nm in found.values()):
                break
        names = [names[0]]
        scored = sorted(((score(names, nm), cik, nm) for cik, nm in found.items()), reverse=True)
        for sc, cik, nm in scored[:5]:
            cand_rows.append({"entity_id": r["entity_id"], "entity_name": r["name"], "candidate_cik": cik,
                              "candidate_name": nm, "score": round(sc, 2)})
        if scored and scored[0][0] >= 0.6:
            r["cik"] = scored[0][1]
            r["cik_status"] = "auto-unverified"
        else:
            r["cik_status"] = "no-match" if not scored else "low-confidence"
        print(f"{r['entity_id']:24s} -> {r['cik'] or '-':>10s}  {scored[0][2] if scored else ''}  ({scored[0][0]:.2f})" if scored else f"{r['entity_id']:24s} -> none")
    L.write_csv(path, rows, fields)
    L.write_csv(os.path.join(L.CONFIG, "cik-candidates.csv"), cand_rows,
                ["entity_id", "entity_name", "candidate_cik", "candidate_name", "score"])


if __name__ == "__main__":
    main()
