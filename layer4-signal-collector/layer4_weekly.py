#!/usr/bin/env python3
"""Layer 4 WEEKLY signal packet - discovery only.

Reads what the daily collector stored, pulls the cumulative sources that need no daily capture
(House Clerk trades, Open Cabinet executive-branch trades, Federal Register), runs the rules and
writes ONE packet for the discovery-routine agents.

  data/weekly/<end-date>/
     01_fund_positions.csv           13F positions vs prior quarter (registry funds + concentrated unknown funds)
     02_ownership_crossings.csv      13D / 13G
     03_insider_transactions.csv     Form 4 open-market purchases / sales
     03b_insider_clusters.csv        cluster-purchase rule (2+ insiders, 30 days) and net selling
     03c_planned_insider_sales.csv   Form 144 notices of proposed sales by insiders (ranked)
     04_event_8k_candidates.csv      8-K events after item-code gate, ranked
     05_private_raises_form_d.csv    Form D raises
     06_ipo_registrations.csv        S-1 / F-1 / 424B4
     07_politician_trades.csv        House periodic transaction reports (parsed)
     08_executive_branch_trades.csv  OGE 278-T via Open Cabinet (new records)
     09_government_items.csv         Federal Register notices on equity stakes / CHIPS / critical minerals
     10_etf_changes.csv              ETF holdings changes captured daily
     10b_etf_overlap.csv             ETF holdings that are in the tracked universe
     11_feed_hits.csv                tagged, clustered feed items (+ rounds extracted by pattern)
     12_status_flags.csv             private-watchlist status checks (IPO / acquired / filed)
     13_unknown_entities.csv         candidates for the entity registry
     14_periodic_reports_tracked.csv 10-Q / 10-K / 6-K / 20-F of tracked entities
     coverage.csv + coverage.json    expected vs collected per source; what the agents must cover by hand
     packet.xlsx                     all datasets, one sheet each
     SUMMARY.md                      human summary

Usage
  python layer4_weekly.py                         # window = 7 days ending today (ET)
  python layer4_weekly.py --end-date 2026-10-13
  python layer4_weekly.py --no-fill               # do not fetch SEC days the daily job has not stored
"""
import argparse
import collections
import csv
import datetime as dt
import io
import json
import os
import re
import sys
import time
import zipfile

import l4lib as L

DAILY = os.path.join(L.DATA, "daily")
WEEKLY = os.path.join(L.DATA, "weekly")


# ----------------------------------------------------------------------------- helpers

def window_days(end, n):
    return [end - dt.timedelta(days=i) for i in range(n - 1, -1, -1)]


def load_sec(day, name):
    return L.read_jsonl(os.path.join(DAILY, "sec", day.isoformat(), name + ".jsonl"))


def sec_day_done(day):
    return os.path.exists(os.path.join(DAILY, "sec", day.isoformat(), "_done.json"))


def sec_day_status(day):
    return L.load_json(os.path.join(DAILY, "sec", day.isoformat(), "_done.json"), None)


def fmt_pct(a, b):
    if a is None or b in (None, 0):
        return None
    return round((a - b) / b * 100, 1)


def parse_mdy(s):
    for f in ("%m/%d/%Y", "%Y-%m-%d", "%m-%d-%Y"):
        try:
            return dt.datetime.strptime(s.strip(), f).date()
        except Exception:
            continue
    return None


# ----------------------------------------------------------------------------- cusip -> ticker

class CusipMapper:
    def __init__(self, universe, budget=120):
        self.path = os.path.join(L.CACHE, "cusip.json")
        self.cache = L.load_json(self.path, {}) or {}
        self.budget = budget
        self.name_map = {}
        for cik, m in universe.tickers_meta.items():
            n = L.norm_name(m["title"])
            if n:
                self.name_map.setdefault(n, m["ticker"].upper())
        self.calls = 0

    def by_name(self, issuer):
        return self.name_map.get(L.norm_name(issuer), "")

    def resolve_many(self, pairs):
        """pairs: iterable of (cusip, issuer). Fills the cache; returns dict cusip -> ticker."""
        todo = []
        for c, issuer in pairs:
            if not c or c in self.cache:
                continue
            t = self.by_name(issuer)
            if t:
                self.cache[c] = t
            else:
                todo.append(c)
        todo = list(dict.fromkeys(todo))
        i = 0
        while i < len(todo) and self.calls < self.budget:
            chunk = todo[i:i + 10]
            i += 10
            body = json.dumps([{"idType": "ID_CUSIP", "idValue": c} for c in chunk]).encode()
            st, resp, ct, err = L.fetch("https://api.openfigi.com/v3/mapping", data=body,
                                        headers={"Content-Type": "application/json"}, gap=2.7, timeout=40)
            self.calls += 1
            if not resp:
                break
            try:
                out = json.loads(resp.decode())
            except Exception:
                break
            for c, o in zip(chunk, out):
                tk = ""
                for d in (o.get("data") or []):
                    if d.get("exchCode") == "US" and d.get("securityType2") in ("Common Stock", "ADR", "REIT", "ETP", "Depositary Receipt"):
                        tk = d.get("ticker", "")
                        break
                self.cache[c] = (tk or "").upper()
        L.save_json(self.path, self.cache)

    def get(self, cusip, issuer=""):
        return self.cache.get(cusip, "") or self.by_name(issuer)


# ----------------------------------------------------------------------------- 13F prior-quarter diff

def fetch_infotable(cik, accession):
    urls = L.sec_filing_urls(cik, accession)
    st, ij, err = L.fetch_text(urls["index_json"])
    if not ij:
        return None
    try:
        files = json.loads(ij)["directory"]["item"]
    except Exception:
        return None
    info = [f for f in files if f["name"].lower().endswith(".xml") and f["name"].lower() != "primary_doc.xml"]
    if not info:
        return None
    f = max(info, key=lambda x: int(x.get("size") or 0))
    st, body, ct, err = L.fetch(urls["folder"] + f["name"], max_bytes=32_000_000, timeout=90)
    if not body:
        return None
    xml = L.parse_xml(body.decode("utf-8", "ignore"))
    if xml is None:
        return None
    out = []
    for it in L.find_all(xml, "infoTable"):
        sh = L.find_first(it, "shrsOrPrnAmt")
        out.append({"issuer": L.child_text(it, "nameOfIssuer"), "class": L.child_text(it, "titleOfClass"),
                    "cusip": L.child_text(it, "cusip"), "value_usd": L.to_float(L.child_text(it, "value")),
                    "shares": L.to_float(L.text_of(sh, "sshPrnamt")) if sh is not None else None,
                    "put_call": L.child_text(it, "putCall")})
    return out


def prior_holdings(cik, period, cap_state):
    """Latest 13F-HR for `cik` with a report period before `period` (MM-DD-YYYY). Cached on disk."""
    pdate = parse_mdy(period) if period else None
    key = os.path.join(L.DATA, "cache", "f13_prior", f"{cik}_{period}.json")
    cached = L.load_json(key, None)
    if cached is not None:
        return cached
    if cap_state["n"] >= cap_state["max"]:
        return None
    cap_state["n"] += 1
    st, sub, err = L.fetch_text(f"https://data.sec.gov/submissions/CIK{int(cik):010d}.json")
    if not sub:
        return None
    try:
        rec = json.loads(sub)["filings"]["recent"]
    except Exception:
        return None
    best = None
    for form, acc, rep in zip(rec["form"], rec["accessionNumber"], rec["reportDate"]):
        if form != "13F-HR" or not rep:
            continue
        rd = parse_mdy(rep)
        if rd and pdate and rd < pdate and (best is None or rd > best[0]):
            best = (rd, acc)
    if not best:
        res = {"period": None, "holdings": []}
    else:
        h = fetch_infotable(cik, best[1])
        res = {"period": best[0].isoformat(), "accession": best[1], "holdings": h or []}
    L.save_json(key, res)
    return res


def build_fund_positions(win, universe, matcher, cm, health, max_prior=40):
    filers = []
    holdings = collections.defaultdict(list)
    for d in win:
        for f in load_sec(d, "f13_filers"):
            filers.append(f)
        for h in load_sec(d, "f13_holdings"):
            holdings[h["accession"]].append(h)
    fetched = [f for f in filers if f.get("holdings_fetched")]
    # resolve tickers for everything we will output
    cm.resolve_many([(h["cusip"], h["issuer"]) for f in fetched for h in holdings.get(f["accession"], [])])
    cap = {"n": 0, "max": max_prior}
    rows = []
    # A registry entity with a known CIK matches ONLY by CIK (a wealth manager called "Accel Wealth
    # Management" must not count as the VC "Accel"); entities without a CIK match by name.
    ents = L.load_entities()
    noncik = L.EntityMatcher([e for e in ents if not (e.get("cik") or "").strip() and e["type"] in ("fund", "vc", "sovereign", "corporate")])
    reg_ciks = set()
    for e in ents:
        for c in re.split(r"[;,\s]+", e.get("cik") or ""):
            if c.isdigit():
                reg_ciks.add(int(c))
    for f in fetched:
        f["is_registry_fund"] = int(f["filer_cik"]) in reg_ciks or bool(noncik.match(f.get("filer_name") or ""))
        cur0 = holdings.get(f["accession"], [])
        tot0 = sum(h["value_usd"] or 0 for h in cur0) or 1
        f["_uni_w"] = sum((h["value_usd"] or 0) for h in cur0
                          if cm.get(h["cusip"], h["issuer"]) in universe.sectors_by_ticker) / tot0 * 100
    # registry funds first, then unknown funds by how much of the book sits in tracked names
    fetched.sort(key=lambda f: (not f.get("is_registry_fund"), -f["_uni_w"]))
    for f in fetched:
        cur = holdings.get(f["accession"], [])
        total = sum(h["value_usd"] or 0 for h in cur) or f.get("total_value_usd") or 0
        etf_val = sum((h["value_usd"] or 0) for h in cur if (h.get("class") or "").upper() in ("ETF", "ETP", "ETN"))
        if total and etf_val / total > 0.6 and not f.get("is_registry_fund"):
            continue  # an ETF-allocation book, not stock-picking conviction
        if f.get("is_registry_fund") or f["_uni_w"] >= 5:
            prior = prior_holdings(f["filer_cik"], f.get("period"), cap)
        else:
            prior = None
        pmap = None
        if prior and prior.get("holdings") is not None and prior.get("period"):
            pmap = collections.defaultdict(lambda: {"shares": 0.0, "value": 0.0})
            for h in prior["holdings"]:
                k = (h["cusip"], h.get("put_call") or "")
                pmap[k]["shares"] += h.get("shares") or 0
                pmap[k]["value"] += h.get("value_usd") or 0
            cm.resolve_many([(h["cusip"], h["issuer"]) for h in prior["holdings"] if False])
        agg = collections.defaultdict(lambda: {"issuer": "", "class": "", "shares": 0.0, "value": 0.0})
        for h in cur:
            k = (h["cusip"], h.get("put_call") or "")
            a = agg[k]
            a["issuer"], a["class"] = h["issuer"], h["class"]
            a["shares"] += h.get("shares") or 0
            a["value"] += h.get("value_usd") or 0
        for (cusip, pc), a in agg.items():
            tk = cm.get(cusip, a["issuer"])
            info = universe.sectors_by_ticker.get(tk) if tk else None
            weight = (a["value"] / total * 100) if total else None
            status, pct = "", None
            if pmap is not None:
                p = pmap.get((cusip, pc))
                if p is None or p["shares"] == 0:
                    status = "NEW"
                else:
                    pct = fmt_pct(a["shares"], p["shares"])
                    if pct is not None and pct >= 25:
                        status = "INCREASED"
                    elif pct is not None and pct <= -25:
                        status = "DECREASED"
                    else:
                        status = "unchanged"
            else:
                status = "prior-not-fetched"
            keep = False
            w = weight or 0
            if f.get("is_registry_fund"):
                if status in ("NEW", "INCREASED", "DECREASED"):
                    keep = bool(info) or w >= 1
                if pc:
                    keep = keep or w >= 0.5
                if status == "prior-not-fetched":
                    keep = bool(info) or w >= 3
            else:
                # unknown fund: only conviction-sized positions in tracked names or very large positions
                if status in ("NEW", "INCREASED", "DECREASED"):
                    keep = (bool(info) and w >= 1.0) or w >= 5
                elif status == "prior-not-fetched":
                    keep = (bool(info) and w >= 3.0) or w >= 8
                if pc:
                    keep = keep or (bool(info) and w >= 1.0)
            if not keep:
                continue
            rows.append({"filer": f["filer_name"], "filer_cik": f["filer_cik"], "registry_fund": f.get("is_registry_fund"),
                         "entity_hits": f.get("entity_hits", ""), "period": f.get("period"), "filing_date": f["filing_date"],
                         "issuer": a["issuer"], "ticker": tk, "cusip": cusip, "class": a["class"], "put_call": pc,
                         "shares": a["shares"], "value_usd": a["value"], "pct_of_book": round(weight, 2) if weight is not None else None,
                         "change": status, "shares_change_pct": pct,
                         "prior_period": (prior or {}).get("period"), "in_universe": bool(info),
                         "sectors": "; ".join(info) if info else "", "book_positions": f.get("entries"),
                         "book_value_usd": f.get("total_value_usd"), "source_link": f["source_link"]})
        if pmap is not None:
            cur_keys = set(agg.keys())
            for (cusip, pc), p in pmap.items():
                if (cusip, pc) in cur_keys or p["shares"] <= 0:
                    continue
                issuer = next((h["issuer"] for h in (prior.get("holdings") or []) if h["cusip"] == cusip), "")
                tk = cm.get(cusip, issuer)
                info = universe.sectors_by_ticker.get(tk) if tk else None
                weight_prior = None
                if info or (p["value"] and prior.get("holdings")):
                    ptot = sum(h.get("value_usd") or 0 for h in prior["holdings"]) or 0
                    weight_prior = p["value"] / ptot * 100 if ptot else None
                wp = weight_prior or 0
                if (f.get("is_registry_fund") and (info or wp >= 1)) or (not f.get("is_registry_fund") and info and wp >= 2):
                    rows.append({"filer": f["filer_name"], "filer_cik": f["filer_cik"], "registry_fund": f.get("is_registry_fund"),
                                 "entity_hits": f.get("entity_hits", ""), "period": f.get("period"), "filing_date": f["filing_date"],
                                 "issuer": issuer, "ticker": tk, "cusip": cusip, "class": "", "put_call": pc, "shares": 0.0,
                                 "value_usd": 0.0, "pct_of_book": 0.0, "change": "EXITED", "shares_change_pct": -100.0,
                                 "prior_period": prior.get("period"), "in_universe": bool(info),
                                 "sectors": "; ".join(info) if info else "", "book_positions": f.get("entries"),
                                 "book_value_usd": f.get("total_value_usd"), "source_link": f["source_link"]})
    order = {"NEW": 0, "INCREASED": 1, "EXITED": 2, "DECREASED": 3}
    rows.sort(key=lambda r: (not r["registry_fund"], not r["in_universe"], order.get(r["change"], 9), -(r["value_usd"] or 0)))
    health["f13"] = {"filers_in_window": len(filers), "holdings_fetched_filers": len(fetched),
                     "prior_quarter_fetches": cap["n"], "openfigi_calls": cm.calls}
    return rows, filers, fetched, holdings


# ----------------------------------------------------------------------------- insiders

C_LEVEL = re.compile(r"\b(chief|ceo|cfo|coo|cto|president|chair|founder|executive vice|evp)\b", re.I)


def insider_tables(end, win, lookback, th):
    alltx = []
    avail = 0
    for i in range(lookback + 1):
        d = end - dt.timedelta(days=i)
        if d.weekday() >= 5:
            continue
        rows = load_sec(d, "form4")
        if sec_day_done(d):
            avail += 1
        alltx += rows
    in_win = {d.isoformat() for d in win}
    ps = [r for r in alltx if r["code"] in ("P", "S") and not r["derivative"] and r["filing_date"] in in_win]
    # cluster: distinct insiders open-market buying within lookback
    groups = collections.defaultdict(list)
    for r in alltx:
        if r["code"] == "P" and not r["derivative"] and (r.get("value_usd") or 0) > 0:
            groups[r["issuer_cik"]].append(r)
    sells = collections.defaultdict(list)
    for r in alltx:
        if r["code"] == "S" and not r["derivative"] and (r.get("value_usd") or 0) > 0:
            sells[r["issuer_cik"]].append(r)
    clusters = []
    for cik, rs in groups.items():
        owners = {}
        for r in rs:
            for n in (r["owner_names"] or "").split("; "):
                if n:
                    owners.setdefault(n, []).append(r)
        # an insider counts only if their own open-market buying is meaningful (filters ESPP-size purchases)
        owners = {n: v for n, v in owners.items() if sum(x["value_usd"] for x in v) >= th.get("form4_cluster_min_per_insider_usd", 5000)}
        if len(owners) < th["form4_cluster_min_insiders"]:
            continue
        rs = [r for r in rs if any(n in owners for n in (r["owner_names"] or "").split("; "))]
        total = sum(r["value_usd"] for r in rs)
        if total < th["form4_cluster_min_value_usd"]:
            continue
        dir_or_off = any(r["is_director"] or r["is_officer"] for r in rs)
        c_level = any(C_LEVEL.search(r.get("officer_title") or "") or r["is_director"] for r in rs)
        s_total = sum(r["value_usd"] for r in sells.get(cik, []))
        s_plan = sum(r["value_usd"] for r in sells.get(cik, []) if r["plan_10b5_1"])
        newest = max(r["filing_date"] for r in rs)
        clusters.append({"issuer": rs[0]["issuer_name"], "ticker": rs[0]["ticker"], "issuer_cik": cik,
                         "distinct_buyers": len(owners), "buyers": "; ".join(sorted(owners)),
                         "titles": "; ".join(sorted({r.get("officer_title") or ("Director" if r["is_director"] else "") for r in rs} - {""})),
                         "buy_value_usd": round(total, 0), "first_tx": min(r["tx_date"] for r in rs), "last_tx": max(r["tx_date"] for r in rs),
                         "newest_filing_date": newest, "director_or_officer_present": dir_or_off, "c_level_or_director": c_level,
                         "net_sells_30d_usd": round(s_total, 0), "of_which_10b5_1_usd": round(s_plan, 0),
                         "in_universe": rs[0]["in_universe"], "sectors": rs[0]["sectors"], "lookback_days_with_data": avail,
                         "qualifies_cluster_rule": dir_or_off and len(owners) >= th["form4_cluster_min_insiders"],
                         "source_link": rs[0]["source_link"]})
    clusters.sort(key=lambda r: (not r["in_universe"], not r["qualifies_cluster_rule"], -r["buy_value_usd"]))
    ps.sort(key=lambda r: (not r["in_universe"], r["code"] != "P", -(r.get("value_usd") or 0)))
    return ps, clusters, avail


# ----------------------------------------------------------------------------- House trades

PTR_TX = re.compile(
    r"(?P<owner>\b(?:SP|JT|DC)\b\s+)?(?P<asset>[A-Z0-9][^\[\]\n]{2,160}?)\(\s*(?P<ticker>[A-Z][A-Z0-9.\-]{0,6})\s*\)\s*\[(?P<atype>[A-Z]{2})\]\s*"
    r"(?P<tx>P|S\s*\(partial\)|S|E)\s+(?P<tdate>\d{2}/\d{2}/\d{4})\s+(?P<ndate>\d{2}/\d{2}/\d{4})\s+"
    r"(?P<amt>\$[\d,]+(?:\s*-\s*\$[\d,]+)?)")


def committees_map(health):
    """name -> list of (committee name, relevant?) from the free congress-legislators dataset."""
    try:
        import yaml
    except Exception:
        health["committees"] = "pyyaml missing"
        return {}
    def get(name):
        p = os.path.join(L.CACHE, name)
        if not (os.path.exists(p) and time.time() - os.path.getmtime(p) < 7 * 86400):
            st, txt, err = L.fetch_text("https://raw.githubusercontent.com/unitedstates/congress-legislators/main/" + name)
            if txt:
                L.ensure_dir(L.CACHE)
                open(p, "w", encoding="utf-8").write(txt)
        return yaml.safe_load(open(p, encoding="utf-8")) if os.path.exists(p) else None
    mem = get("committee-membership-current.yaml")
    com = get("committees-current.yaml")
    leg = get("legislators-current.yaml")
    if not (mem and com and leg):
        health["committees"] = "dataset unavailable"
        return {}
    cname = {}
    for c in com:
        cname[c.get("thomas_id")] = c.get("name")
        for s in c.get("subcommittees", []) or []:
            cname[c.get("thomas_id") + s.get("thomas_id")] = c.get("name") + " / " + s.get("name")
    bio = {}
    for l in leg:
        n = l["name"]
        bio[l["id"]["bioguide"]] = (n.get("first", ""), n.get("last", ""), n.get("official_full", ""))
    out = collections.defaultdict(list)
    for code, members in mem.items():
        for m in members:
            b = m.get("bioguide")
            if b in bio and code in cname and "/" not in cname[code]:
                first, last, full = bio[b]
                out[(last.lower(), first.lower())].append(cname[code])
    health["committees"] = f"{len(out)} members mapped"
    return out


REL_COMMITTEE = re.compile(r"armed services|intelligence|science|commerce|energy|financial services|strategic competition|foreign|homeland|appropriations", re.I)


def house_trades(win, universe, matcher, health):
    rows = []
    stat = {"ptr_filings_in_window": 0, "pdf_parsed": 0, "pdf_no_text": 0, "pdf_failed": 0, "trade_rows": 0, "years": []}
    try:
        from pypdf import PdfReader
    except Exception:
        health["house"] = {"error": "pypdf not installed"}
        return rows
    start, end = win[0], win[-1]
    years = sorted({start.year, end.year})
    cm = committees_map(health)
    registry_names = {}
    for e in L.load_entities():
        if e["type"] == "politician":
            registry_names[e["name"].lower()] = e["entity_id"]
    for y in years:
        st, body, ct, err = L.fetch(f"https://disclosures-clerk.house.gov/public_disc/financial-pdfs/{y}FD.zip", timeout=90)
        if not body:
            stat.setdefault("errors", []).append(f"{y} index {err}")
            continue
        stat["years"].append(y)
        z = zipfile.ZipFile(io.BytesIO(body))
        xmlname = next((n for n in z.namelist() if n.lower().endswith(".xml")), None)
        root = L.parse_xml(z.read(xmlname).decode("utf-8-sig", "ignore")) if xmlname else None
        if root is None:
            continue
        for m in L.find_all(root, "Member"):
            if L.child_text(m, "FilingType") != "P":
                continue
            fd = parse_mdy(L.child_text(m, "FilingDate"))
            if not fd or not (start <= fd <= end):
                continue
            stat["ptr_filings_in_window"] += 1
            doc = L.child_text(m, "DocID")
            last, first = L.child_text(m, "Last"), L.child_text(m, "First")
            link = f"https://disclosures-clerk.house.gov/public_disc/ptr-pdfs/{y}/{doc}.pdf"
            st, pdf, ct, err = L.fetch(link, timeout=60, gap=0.8)
            base = {"member": f"{first} {last}".strip(), "state_district": L.child_text(m, "StateDst"), "doc_id": doc,
                    "filing_date": fd.isoformat(), "source_link": link}
            cl = cm.get((last.lower(), first.lower().split(" ")[0])) or cm.get((last.lower(), first.lower())) or []
            base["committees"] = "; ".join(sorted(set(cl)))[:300]
            base["relevant_committee"] = any(REL_COMMITTEE.search(c) for c in cl)
            base["in_registry"] = any(re.sub(r"[^a-z ]", "", base["member"].lower()).split()[-1] in k for k in registry_names)
            if not pdf:
                stat["pdf_failed"] += 1
                rows.append({**base, "parse_status": f"fetch failed {err}"})
                continue
            try:
                rd = PdfReader(io.BytesIO(pdf))
                text = " ".join((p.extract_text() or "") for p in rd.pages)
            except Exception as ex:
                stat["pdf_failed"] += 1
                rows.append({**base, "parse_status": f"pdf error {type(ex).__name__}"})
                continue
            text = re.sub(r"\s+", " ", text)
            if len(text) < 200:
                stat["pdf_no_text"] += 1
                rows.append({**base, "parse_status": "no text layer (scanned) - agent must read"})
                continue
            n = 0
            for t in PTR_TX.finditer(text):
                tk = t.group("ticker").upper()
                rows.append({**base, "parse_status": "ok", "owner": (t.group("owner") or "").strip(),
                             "asset": t.group("asset").strip()[-120:], "ticker": tk, "asset_type": t.group("atype"),
                             "tx_type": t.group("tx").replace(" ", ""), "tx_date": t.group("tdate"),
                             "notification_date": t.group("ndate"), "amount_range": t.group("amt"),
                             "in_universe": tk in universe.sectors_by_ticker,
                             "sectors": "; ".join(universe.sectors_by_ticker.get(tk, []))})
                n += 1
            if n == 0:
                rows.append({**base, "parse_status": "text found but no transaction pattern matched - agent must read"})
            else:
                stat["pdf_parsed"] += 1
                stat["trade_rows"] += n
    health["house"] = stat
    rows.sort(key=lambda r: (not r.get("in_universe", False), not r.get("relevant_committee", False), r.get("member", "")))
    return rows


# ----------------------------------------------------------------------------- Open Cabinet (OGE)

def exec_branch(win, health, universe=None):
    path = os.path.join(L.CACHE, "open-cabinet-all-transactions.csv")
    fresh = os.path.exists(path) and time.time() - os.path.getmtime(path) < 20 * 3600
    if not fresh:
        st, body, ct, err = L.fetch("https://open-cabinet.org/data/all-transactions.csv", timeout=120, max_bytes=60_000_000)
        if not body:
            health["oge"] = {"error": f"download failed {err}"}
            return []
        L.ensure_dir(L.CACHE)
        open(path, "wb").write(body)
    lines = [l for l in open(path, encoding="utf-8-sig") if not l.startswith("#")]
    rows = list(csv.DictReader(lines))
    seen_path = os.path.join(L.DATA, "cache", "oge_seen.json")
    seen = L.load_json(seen_path, {}) or {}
    if isinstance(seen, list):  # older format: ids only
        seen = {i: "1970-01-01" for i in seen}
    first_run = not seen
    horizon = win[-1] - dt.timedelta(days=90)
    stamp = win[-1].isoformat()
    out = []
    for r in rows:
        rid = r.get("recordId")
        if not rid:
            continue
        d = parse_mdy(r.get("date") or "")
        first_seen = seen.get(rid)
        if first_seen is None:
            if first_run and (not d or d < horizon):
                seen[rid] = "1970-01-01"      # baseline: existing history, not news this week
                continue
            seen[rid] = stamp
            first_seen = stamp
        if first_run:
            if not d or d < horizon:
                continue
        elif first_seen < win[0].isoformat():
            continue   # seen in an earlier window; re-running this window stays idempotent
        out.append({"official": r.get("official_name"), "title": r.get("official_title"), "agency": r.get("agency"),
                    "ticker": r.get("resolved_ticker") or r.get("ticker"), "description": r.get("description"),
                    "type": r.get("type"), "tx_date": r.get("date"), "amount_range": r.get("amount_range"),
                    "late_filing": r.get("late_filing"), "verification": r.get("verificationState"),
                    "instrument_type": r.get("instrument_type"), "source_kind": r.get("source_kind"), "record_id": rid,
                    "first_seen_in_packet": first_seen,
                    "in_universe": bool(universe and (r.get("resolved_ticker") or r.get("ticker") or "").upper() in universe.sectors_by_ticker),
                    "sectors": "; ".join(universe.sectors_by_ticker.get((r.get("resolved_ticker") or r.get("ticker") or "").upper(), [])) if universe else "",
                    "source_link": r.get("source_filing_url")})
    L.save_json(seen_path, seen)
    health["oge"] = {"records_total": len(rows), "new_or_recent_rows": len(out), "first_run_window_days": 90 if first_run else None}
    out.sort(key=lambda r: (not r["in_universe"], (r.get("instrument_type") or "") != "common_stock", -int((r.get("tx_date") or "0000-00-00").replace("-", "") or 0)))
    return out


# ----------------------------------------------------------------------------- Federal Register

def federal_register(win, health):
    start, end = win[0], win[-1]
    terms = ['"equity stake"', '"equity interest"', '"CHIPS"', '"Office of Strategic Capital"', '"critical minerals"',
             '"Defense Production Act"', '"strategic investment"']
    seen, rows, errs, dropped = set(), [], [], 0
    for t in terms:
        url = ("https://www.federalregister.gov/api/v1/documents.json?per_page=100&order=newest"
               f"&conditions[term]={t}&conditions[publication_date][gte]={start}&conditions[publication_date][lte]={end}")
        url += "".join(f"&conditions[agencies][]={a}" for a in ("commerce-department", "defense-department", "energy-department"))
        url += "&fields[]=title&fields[]=type&fields[]=agency_names&fields[]=abstract&fields[]=html_url&fields[]=publication_date&fields[]=document_number"
        st, txt, err = L.fetch_text(url.replace('"', "%22").replace(" ", "%20"))
        if not txt:
            errs.append(f"{t}: {err}")
            continue
        try:
            for d in json.loads(txt).get("results", []):
                if d["document_number"] in seen:
                    continue
                hay = ((d.get("title") or "") + " " + (d.get("abstract") or "")).lower()
                if t.strip('"').lower() not in hay:
                    dropped += 1  # matched only deep in the full text
                    continue
                seen.add(d["document_number"])
                rows.append({"publication_date": d.get("publication_date"), "type": d.get("type"),
                             "agencies": "; ".join(d.get("agency_names") or []), "title": d.get("title"),
                             "abstract": (d.get("abstract") or "")[:400], "matched_term": t.strip('"'),
                             "source_link": d.get("html_url")})
        except Exception as e:
            errs.append(f"{t}: parse {e}")
    health["fedreg"] = {"documents": len(rows), "dropped_fulltext_only": dropped, "errors": errs}
    rows.sort(key=lambda r: r["publication_date"], reverse=True)
    return rows


# ----------------------------------------------------------------------------- feeds

ROUND_RX = re.compile(
    r"^(?P<co>.{2,80}?)\s+(?:raises|raised|secures|lands|closes|nabs|snags|banks|bags|scores|gets|announces)\s+(?:a\s+)?"
    r"(?:\$|US\$)(?P<amt>[\d][\d.,]*\s?(?:million|billion|thousand|M|B|K|m|b|k)?)\b"
    r"(?:\s+(?:in|for|to)\b)?(?:\s*(?P<round>Series\s[A-H]\b|seed|pre-seed|growth|funding|venture|equity))?", re.I)
LEAD_RX = re.compile(r"(?:led by|co-led by|backed by|participation from|investors include|with participation from)\s+"
                     r"(?P<inv>[A-Z0-9][\w&.'\-]*(?:\s+[A-Z0-9][\w&.'\-]*){0,4}(?:\s*(?:,|and|&)\s*[A-Z0-9][\w&.'\-]*(?:\s+[A-Z0-9][\w&.'\-]*){0,4}){0,4})")


def cluster_items(items):
    """Greedy near-duplicate clustering on title token sets."""
    clusters = []
    for it in items:
        toks = set(re.findall(r"[a-z0-9]{3,}", it["title"].lower()))
        it["_toks"] = toks
        placed = False
        for c in clusters:
            inter = len(toks & c["toks"])
            union = len(toks | c["toks"]) or 1
            if inter / union >= 0.7:
                it["cluster_id"] = c["id"]
                c["n"] += 1
                placed = True
                break
        if not placed:
            cid = f"c{len(clusters) + 1:04d}"
            clusters.append({"id": cid, "toks": toks, "n": 1})
            it["cluster_id"] = cid
    size = {c["id"]: c["n"] for c in clusters}
    for it in items:
        it["cluster_size"] = size[it["cluster_id"]]
        it.pop("_toks", None)
    return items


def feed_hits(win, health, matcher):
    in_win = {d.isoformat() for d in win}
    items = []
    for d in win:
        items += L.read_jsonl(os.path.join(DAILY, "feeds", d.isoformat() + ".jsonl"))
    scored = []
    for it in items:
        s = 0
        if it["entity_hits"]:
            s += 2
        if it["keyword_groups"]:
            s += 2 * len(it["keyword_groups"].split("; "))
        if it["tickers"]:
            s += 1
        if it["category"] in ("newsroom", "gov", "regulator"):
            s += 1
        if it["sector_hits"]:
            s += 1
        it["score"] = s
        m = ROUND_RX.match(it["title"])
        it["round_extracted"] = bool(m)
        if m:
            it["extracted_company"] = m.group("co").strip()
            it["extracted_amount"] = "$" + m.group("amt").strip()
            it["extracted_round"] = (m.group("round") or "").strip()
        lead = LEAD_RX.search(it["title"] + ". " + it["summary"])
        if lead:
            it["extracted_investors"] = re.sub(r"\s+", " ", lead.group("inv")).strip()[:160]
        if s >= 3 or m or lead:
            scored.append(it)
    scored = cluster_items(sorted(scored, key=lambda r: -r["score"]))
    health["feeds"] = {"items_captured": len(items), "kept_for_agents": len(scored),
                       "days_with_data": sum(1 for d in win if os.path.exists(os.path.join(DAILY, "feeds", d.isoformat() + ".jsonl")))}
    return scored, items


# ----------------------------------------------------------------------------- ETF

def etf_tables(win, universe, health):
    changes = []
    for d in win:
        p = os.path.join(DAILY, "etf", "diffs", d.isoformat() + ".csv")
        if os.path.exists(p):
            changes += L.read_csv(p)
    out = []
    for c in changes:
        t = (c.get("ticker") or "").upper()
        info = universe.sectors_by_ticker.get(t)
        c["in_universe"] = bool(info)
        c["sectors"] = "; ".join(info) if info else ""
        out.append(c)
    out.sort(key=lambda r: (not r["in_universe"], r["change"] not in ("added", "entered_top25"), r["etf"]))
    # overlap table from the newest snapshot of each ETF
    snap_root = os.path.join(DAILY, "etf", "snapshots")
    overlap, by_ticker = [], collections.defaultdict(list)
    etfs_meta = {e["ticker"].upper(): e for e in L.read_csv(os.path.join(L.CONFIG, "etfs.csv"))}
    if os.path.isdir(snap_root):
        for t in sorted(os.listdir(snap_root)):
            files = sorted(f for f in os.listdir(os.path.join(snap_root, t)) if f.endswith(".csv"))
            if not files:
                continue
            rows = L.read_csv(os.path.join(snap_root, t, files[-1]))
            w_all = sum(L.to_float(r["weight_pct"]) or 0 for r in rows)
            inu = [r for r in rows if (r["ticker"] or "").upper() in universe.sectors_by_ticker]
            w_in = sum(L.to_float(r["weight_pct"]) or 0 for r in inu)
            meta = etfs_meta.get(t, {})
            overlap.append({"etf": t, "name": meta.get("name", ""), "snapshot": files[-1][:-4], "holdings": len(rows),
                            "holdings_in_universe": len(inu), "weight_in_universe_pct": round(w_in, 1),
                            "partial_source": meta.get("method") == "stockanalysis_html", "sector_hint": meta.get("sector_hint", "")})
            for r in inu:
                by_ticker[r["ticker"].upper()].append((t, L.to_float(r["weight_pct"])))
    overlap.sort(key=lambda r: -r["holdings_in_universe"])
    cross = [{"ticker": tk, "etf_count": len(v), "etfs": "; ".join(f"{e} {w:.1f}%" if w is not None else e for e, w in sorted(v, key=lambda x: -(x[1] or 0))),
              "sectors": "; ".join(universe.sectors_by_ticker.get(tk, []))} for tk, v in by_ticker.items()]
    cross.sort(key=lambda r: -r["etf_count"])
    health["etf"] = {"change_rows": len(out), "etfs_with_snapshot": len(overlap)}
    return out, overlap, cross


# ----------------------------------------------------------------------------- status + unknowns

def status_flags(win, universe, items, health, budget=60):
    path = os.path.join(L.CONFIG, "private-watchlist.csv")
    if not os.path.exists(path):
        return []
    rows, seen_n = [], set()
    for r in L.read_csv(path):
        k = L.norm_name(re.sub(r"[-_]+", " ", r["company"]))
        if k and k not in seen_n:
            seen_n.add(k)
            rows.append(r)
    tickers = {L.norm_name(m["title"]): (m["ticker"], cik) for cik, m in universe.tickers_meta.items()}
    status_words = ("ipo", "went public", "files for ipo", "filed for ipo", "to be acquired", "acquire", "acquisition", "merger", "spac", "direct listing")
    out = []
    n_fts = 0
    end = win[-1]
    start = end - dt.timedelta(days=120)
    for r in rows:
        name = r["company"].strip()
        nn = L.norm_name(name)
        flags, evid = [], []
        if nn in tickers:
            flags.append("HAS-SEC-TICKER")
            evid.append(f"{tickers[nn][0]} (CIK {tickers[nn][1]})")
        low = name.lower()
        hits = [it for it in items if low in (it["title"] + " " + it["summary"]).lower()]
        for it in hits:
            tl = (it["title"] + " " + it["summary"]).lower()
            if any(w in tl for w in status_words):
                flags.append("NEWS-STATUS-WORDS")
                evid.append(it["title"][:120] + " | " + it["link"])
                break
        if n_fts < budget and len(nn) >= 5:
            url = ("https://efts.sec.gov/LATEST/search-index?q=%22" + L.urllib.parse.quote(name) + "%22&forms=S-1,F-1,424B4,S-1/A,F-1/A"
                   f"&dateRange=custom&startDate={start}&endDate={end}")
            st, txt, err = L.fetch_text(url)
            n_fts += 1
            try:
                hs = json.loads(txt)["hits"]["hits"] if txt else []
            except Exception:
                hs = []
            for h in hs[:3]:
                src = h.get("_source", {})
                dn = " ".join(src.get("display_names", []))
                if nn and nn.split(" ")[0].lower() in dn.lower():
                    flags.append("SEC-REGISTRATION-FILING")
                    evid.append(f"{src.get('form')} {src.get('file_date')} {dn}")
                    break
        out.append({"company": name, "sector": r.get("sector", ""), "last_seen_run": r.get("last_seen_run", ""),
                    "flags": "; ".join(dict.fromkeys(flags)) or "none-found", "evidence": " || ".join(evid)[:500],
                    "news_items_mentioning": len(hits)})
    out.sort(key=lambda r: (r["flags"] == "none-found", r["company"]))
    health["status_checks"] = {"companies_checked": len(rows), "flagged": sum(1 for r in out if r["flags"] != "none-found"),
                               "sec_search_calls": n_fts, "budget_exhausted": n_fts >= budget}
    return out


def unknown_entities(fund_rows, filers, house_rows, own_rows, items, matcher, universe):
    out = []
    known_funds = {r["filer"] for r in fund_rows if r.get("registry_fund")}
    # unknown concentrated funds ranked by universe overlap
    by_f = collections.defaultdict(lambda: {"u": 0, "new_u": 0, "val": 0.0, "name": "", "link": "", "book": None, "cik": None, "reg": False})
    for r in fund_rows:
        f = by_f[r["filer"]]
        f["name"], f["link"], f["book"], f["cik"], f["reg"] = r["filer"], r["source_link"], r["book_value_usd"], r["filer_cik"], r["registry_fund"]
        if r["in_universe"]:
            f["u"] += 1
            f["val"] += r["value_usd"] or 0
            if r["change"] in ("NEW", "INCREASED"):
                f["new_u"] += 1
    for k, f in by_f.items():
        if not f["reg"] and f["u"] >= 1:
            out.append({"kind": "fund (13F, not in registry)", "name": f["name"], "cik": f["cik"],
                        "evidence": f"{f['u']} conviction-sized tracked-universe changes ({f['new_u']} new/increased), book ${f['book'] or 0:,.0f}",
                        "score": f["new_u"] * 3 + f["u"], "source_link": f["link"]})
    for r in own_rows:
        if r.get("in_universe") and not r.get("entity_hits"):
            out.append({"kind": "13D/13G filer (not in registry)", "name": r["filer_names"][:120], "cik": "",
                        "evidence": f"{r['submission_type']} {r['pct_of_class']}% of {r['subject_name']} ({r['ticker']})",
                        "score": 3 if "13D" in r["submission_type"] else 1, "source_link": r["source_link"]})
    mem = collections.defaultdict(lambda: {"n": 0, "u": 0, "link": ""})
    for r in house_rows:
        if r.get("parse_status") == "ok" and not r.get("in_registry"):
            m = mem[r["member"]]
            m["n"] += 1
            m["u"] += 1 if r.get("in_universe") else 0
            m["link"] = r["source_link"]
    for name, m in mem.items():
        if m["u"]:
            out.append({"kind": "politician (not in registry)", "name": name, "cik": "",
                        "evidence": f"{m['n']} trades, {m['u']} in tracked universe", "score": m["u"], "source_link": m["link"]})
    inv = collections.Counter()
    ex = {}
    for it in items:
        v = it.get("extracted_investors")
        if not v:
            continue
        for part in re.split(r"\s*(?:,|\band\b|&)\s*", v):
            part = part.strip(" .")
            if len(part) < 3 or part.lower() in ("the", "a"):
                continue
            inv[part] += 1
            ex.setdefault(part, it["link"])
    reg_names = {e["name"].lower() for e in L.load_entities()}
    for name, c in inv.most_common(60):
        if matcher.match(name) or name.lower() in reg_names:
            continue
        out.append({"kind": "investor named in round coverage (not in registry)", "name": name, "cik": "",
                    "evidence": f"named as investor in {c} feed item(s)", "score": c, "source_link": ex[name]})
    out.sort(key=lambda r: -r["score"])
    return out


# ----------------------------------------------------------------------------- 8-K ranking

def rank_8k(rows):
    out = []
    for r in rows:
        s = 0
        s += 3 if r.get("in_universe") else 0
        s += 3 if r.get("entity_hits") else 0
        s += 2 * len([g for g in (r.get("keyword_groups") or "").split("; ") if g])
        codes = (r.get("item_codes") or "")
        s += 2 if any(c in codes for c in ("1.01", "3.02", "5.01")) else 0
        s += 1 if r.get("amounts") else 0
        s += 1 if r.get("sector_hits") else 0
        r = dict(r)
        r["score"] = s
        out.append(r)
    out.sort(key=lambda r: -r["score"])
    return out


# ----------------------------------------------------------------------------- main

def write_packet(outdir, name, rows, fields=None):
    L.write_csv(os.path.join(outdir, name + ".csv"), rows, fields)
    return len(rows)


def main():
    ap = argparse.ArgumentParser(description="Layer 4 weekly signal packet")
    ap.add_argument("--end-date")
    ap.add_argument("--days", type=int, default=7)
    ap.add_argument("--no-fill", action="store_true", help="do not fetch SEC days missing from the daily store")
    ap.add_argument("--no-house", action="store_true")
    ap.add_argument("--no-network-extras", action="store_true", help="skip House, OGE, Federal Register, status checks")
    ap.add_argument("--max-prior-13f", type=int, default=40)
    a = ap.parse_args()

    end = L.parse_date(a.end_date) if a.end_date else L.today_et()
    win = window_days(end, a.days)
    started = time.time()
    health = {"window_start": win[0].isoformat(), "window_end": win[-1].isoformat(), "started_et": L.now_et().isoformat(timespec="seconds")}
    kw = L.load_keywords()
    universe = L.Universe()
    matcher = L.EntityMatcher()
    tagger = L.KeywordTagger(kw)
    th = kw["thresholds"]
    outdir = os.path.join(WEEKLY, end.isoformat())
    L.ensure_dir(outdir)

    # ---- SEC days present?
    weekdays = [d for d in win if d.weekday() < 5]
    missing = [d for d in weekdays if not sec_day_done(d)]
    health["sec_days_expected"] = len(weekdays)
    health["sec_days_missing_before_fill"] = [d.isoformat() for d in missing]
    if missing and not a.no_fill:
        import layer4_daily as D
        sc = D.SecCollector(universe, matcher, tagger, kw, time.time() + 40 * 60)
        hh = {"sec_days": {}, "skipped_done": [], "no_index": [], "pending": []}
        for d in missing:
            sc.collect_day(d, hh)
        health["filled_by_weekly"] = {"days": list(hh["sec_days"].keys()), "no_index": hh["no_index"], "pending": hh["pending"]}
        missing = [d for d in weekdays if not sec_day_done(d)]
    no_index_days = []
    for d in weekdays:
        s = sec_day_status(d)
        if s and s.get("status") == "no-index":
            no_index_days.append(d.isoformat())
    health["sec_days_no_index_holiday"] = no_index_days
    health["sec_days_still_missing"] = [d.isoformat() for d in missing if d.isoformat() not in no_index_days]

    # ---- datasets
    cm = CusipMapper(universe)
    fund_rows, filers, fetched, holdings = build_fund_positions(win, universe, matcher, cm, health, a.max_prior_13f)
    own = []
    f4_all = []
    f8k, fd, reg, per, f144 = [], [], [], [], []
    for d in win:
        f144 += load_sec(d, "form144")
        own += load_sec(d, "ownership13dg")
        f8k += load_sec(d, "events8k")
        fd += load_sec(d, "form_d")
        reg += load_sec(d, "registrations")
        per += load_sec(d, "periodic")
    own.sort(key=lambda r: (not r.get("in_universe"), "13D" not in r["submission_type"], -(r.get("pct_of_class") or 0)))
    for r in own:
        r["is_amendment"] = r["submission_type"].endswith("/A")
        r["activist_form"] = "13D" in r["submission_type"]
    ins, clusters, lookback_have = insider_tables(end, win, th["form4_cluster_lookback_days"], th)
    f8k_ranked = rank_8k(f8k)
    seen144, p144 = set(), []
    for r in f144:
        if r["accession"] in seen144:
            continue
        seen144.add(r["accession"])
        p144.append(r)
    p144.sort(key=lambda r: (not r.get("in_universe"), not r.get("entity_hits"), -(r.get("aggregate_market_value") or 0)))
    # Form D priority
    hi_ind = ("technology", "computers", "other technology", "telecommunications", "energy", "electric", "oil", "mining",
              "manufacturing", "biotechnology", "other", "business services", "environmental")
    for r in fd:
        pri = "low"
        ig = (r.get("industry_group") or "").lower()
        if r.get("in_universe") or r.get("entity_hits") or r.get("sector_hits"):
            pri = "high"
        elif any(k in ig for k in ("technology", "computers", "telecom", "energy", "electric", "mining", "manufactur")):
            pri = "medium"
        r["priority"] = pri
    fd.sort(key=lambda r: ({"high": 0, "medium": 1, "low": 2}[r["priority"]], -(r.get("offering_amount") or 0)))

    extras = not a.no_network_extras
    house = house_trades(win, universe, matcher, health) if (extras and not a.no_house) else []
    if not extras or a.no_house:
        health["house"] = "skipped"
    oge = exec_branch(win, health, universe) if extras else []
    fedreg = federal_register(win, health) if extras else []
    feed_rows, all_feed = feed_hits(win, health, matcher)
    etf_changes, etf_overlap, etf_cross = etf_tables(win, universe, health)
    status = status_flags(win, universe, all_feed, health) if extras else []
    unknown = unknown_entities(fund_rows, filers, house, own, feed_rows, matcher, universe)

    datasets = [
        ("01_fund_positions", fund_rows), ("02_ownership_crossings", own), ("03_insider_transactions", ins),
        ("03b_insider_clusters", clusters), ("03c_planned_insider_sales", p144), ("04_event_8k_candidates", f8k_ranked), ("05_private_raises_form_d", fd),
        ("06_ipo_registrations", reg), ("07_politician_trades", house), ("08_executive_branch_trades", oge),
        ("09_government_items", fedreg), ("10_etf_changes", etf_changes), ("10b_etf_overlap", etf_overlap),
        ("10c_etf_cross_holdings", etf_cross), ("11_feed_hits", feed_rows), ("12_status_flags", status),
        ("13_unknown_entities", unknown), ("14_periodic_reports_tracked", per),
    ]
    counts = {}
    for name, rows in datasets:
        counts[name] = write_packet(outdir, name, rows)

    # ---- coverage ledger
    daily_health = []
    for d in win:
        h = L.load_json(os.path.join(DAILY, "health", d.isoformat() + ".json"), None)
        if h:
            daily_health.append(h)
    feed_fail, etf_fail, etf_stale, etf_partial = {}, {}, set(), set()
    for h in daily_health:
        for k, v in (h.get("feeds") or {}).items():
            if "error" in v or "skipped" in v:
                feed_fail[k] = v.get("error") or v.get("skipped")
        for k, v in (h.get("etf") or {}).items():
            if "error" in v:
                etf_fail[k] = v["error"]
            if v.get("stale"):
                etf_stale.add(k)
            if str(v.get("coverage", "")).startswith("partial"):
                etf_partial.add(k)
    disabled_feeds = [f["feed_id"] + " (" + f["notes"] + ")" for f in L.read_csv(os.path.join(L.CONFIG, "feeds.csv"))
                      if (f.get("enabled") or "yes").lower() != "yes"]
    ledger = {
        "window": f"{win[0]} .. {win[-1]}",
        "sec": {"weekdays_in_window": len(weekdays), "stored_days": len(weekdays) - len(health["sec_days_still_missing"]) - len(no_index_days),
                "holiday_or_no_index": no_index_days, "missing_days": health["sec_days_still_missing"],
                "form4_lookback_days_with_data": lookback_have, "form4_cluster_lookback_target": th["form4_cluster_lookback_days"],
                "form13f_filers": health["f13"]["filers_in_window"], "form13f_holdings_fetched": health["f13"]["holdings_fetched_filers"],
                "form13f_skipped_large_books": health["f13"]["filers_in_window"] - health["f13"]["holdings_fetched_filers"]},
        "house": health.get("house"), "oge": health.get("oge"), "federal_register": health.get("fedreg"),
        "feeds": {**health["feeds"], "failed_feeds": feed_fail, "disabled_feeds": disabled_feeds},
        "etf": {"failed": etf_fail, "stale_source": sorted(etf_stale), "partial_top25_only": sorted(etf_partial)},
        "status_checks": health.get("status_checks"),
        "not_collected_by_script": [
            "Senate periodic transaction reports (efdsearch.senate.gov needs a session/terms form) -> agent",
            "USAspending / SAM.gov awards (structured but not wired) -> agent",
            "Private rounds with investors named only in article text / paywalled pages -> agent",
            "Corporate strategic investments announced by the TARGET rather than the investor -> agent",
            "Sources blocked for scripted access: " + "; ".join(disabled_feeds) + " -> agent",
            "13F books skipped as large/diversified (holdings not parsed) -> universe-wide reverse check not done",
            "Statements, interviews, endorsements (Stream 4) -> agent",
            "Non-US filings of foreign-listed tracked names -> agent",
        ],
        "dataset_rows": counts,
    }
    L.save_json(os.path.join(outdir, "coverage.json"), ledger)
    cov_rows = [{"area": k, "detail": json.dumps(v, ensure_ascii=False)[:900]} for k, v in ledger.items() if k != "dataset_rows"]
    L.write_csv(os.path.join(outdir, "coverage.csv"), cov_rows, ["area", "detail"])

    # ---- xlsx
    try:
        import openpyxl
        wb = openpyxl.Workbook()
        wb.remove(wb.active)
        for name, rows in datasets:
            ws = wb.create_sheet(name[:31])
            if not rows:
                ws.append(["(no rows)"])
                continue
            cols = []
            for r in rows[:5000]:
                for k in r:
                    if k not in cols:
                        cols.append(k)
            ws.append(cols)
            for r in rows[:5000]:
                ws.append([("" if r.get(c) is None else (str(r[c]) if isinstance(r.get(c), (list, dict)) else r.get(c))) for c in cols])
        wb.save(os.path.join(outdir, "packet.xlsx"))
    except Exception as e:
        health["xlsx_error"] = str(e)

    # ---- summary
    top = lambda rows, n=5: rows[:n]
    md = [f"# Layer 4 weekly signal packet - window {win[0]} to {win[-1]}", "",
          f"Generated {L.now_et().isoformat(timespec='seconds')} ET by layer4_weekly.py. Discovery only - nothing here is scored or tiered.", "",
          "## Dataset sizes", ""]
    for name, rows in datasets:
        md.append(f"- {name}: {len(rows)} rows")
    md += ["", "## Highest-signal items (script-ranked)", ""]
    md.append("### Fund positions (new / increased, tracked universe or registry funds)")
    for r in [x for x in fund_rows if x["change"] in ("NEW", "INCREASED")][:8]:
        md.append(f"- {r['filer']}: {r['change']} {r['issuer']} ({r['ticker'] or 'no ticker'}) {r['pct_of_book']}% of book - {r['source_link']}")
    md.append("\n### Insider clusters")
    for r in clusters[:6]:
        md.append(f"- {r['issuer']} ({r['ticker']}): {r['distinct_buyers']} buyers, ${r['buy_value_usd']:,.0f}, cluster rule {r['qualifies_cluster_rule']} - {r['source_link']}")
    md.append("\n### 8-K events")
    for r in f8k_ranked[:6]:
        md.append(f"- {r['company']} items {r['item_codes']} score {r['score']}: {r.get('excerpt','')[:160]} - {r['source_link']}")
    md.append("\n### Politician trades (in tracked universe)")
    for r in [x for x in house if x.get("in_universe")][:6]:
        md.append(f"- {r['member']}: {r['tx_type']} {r['ticker']} {r['amount_range']} ({r['tx_date']}) - {r['source_link']}")
    md.append("\n### Coverage gaps to cover by hand")
    for g in ledger["not_collected_by_script"]:
        md.append(f"- {g}")
    open(os.path.join(outdir, "SUMMARY.md"), "w", encoding="utf-8").write("\n".join(md) + "\n")

    health["elapsed_min"] = round((time.time() - started) / 60, 1)
    health["dataset_rows"] = counts
    L.save_json(os.path.join(outdir, "run-health.json"), health)
    log = os.path.join(L.DATA, "run-log.csv")
    with open(log, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if os.path.getsize(log) == 0:
            w.writerow(["finished_et", "script", "date"])
        w.writerow([L.now_et().isoformat(timespec="seconds"), "weekly", end.isoformat(), "packet",
                    sum(counts.values()), "", "", "", "", health["elapsed_min"]])
    print(json.dumps({"window": ledger["window"], "rows": counts, "elapsed_min": health["elapsed_min"],
                      "missing_sec_days": health["sec_days_still_missing"]}, indent=1))


if __name__ == "__main__":
    main()
