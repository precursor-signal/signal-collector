#!/usr/bin/env python3
"""Layer 4 DAILY collector - capture only, no analysis.

Modules
  sec    incremental SEC pull: daily index -> parsed Form 4, 13D/13G, 13F, 8-K events, Form D,
         Form 144, S-1/F-1/424B4 registrations, periodic reports of tracked entities
  feeds  wire / newsroom / trade-press / government / news-search feed items, tagged, de-duplicated
  etf    holdings snapshot per ETF in config/etfs.csv, plus a diff against the previous snapshot

Output (relative to data/daily/):
  sec/<YYYY-MM-DD>/*.jsonl      parsed filings for that FILING date
  feeds/<YYYY-MM-DD>.jsonl      new feed items captured on that day (ET)
  etf/snapshots/<T>/<date>.csv  holdings
  etf/diffs/<date>.csv          changes vs previous snapshot
  health/<date>.json            counts, failures (what did NOT work today)
Run log: data/run-log.csv

Usage
  python layer4_daily.py                      # last 5 business days of SEC (catch-up) + feeds + etf
  python layer4_daily.py --only sec --lookback 10
  python layer4_daily.py --only feeds --google-window 7d
  python layer4_daily.py --sec-limit 25       # cap filings per form per day (testing)
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
import traceback
import urllib.parse
from io import BytesIO

import l4lib as L

DAILY = os.path.join(L.DATA, "daily")


# =============================================================================== SEC

class SecCollector:
    def __init__(self, universe, matcher, tagger, kw, deadline, limit=None, force=False, only_forms=None):
        self.u = universe
        self.m = matcher
        self.t = tagger
        self.kw = kw
        self.deadline = deadline
        self.limit = limit
        self.force = force
        self.only_forms = set(only_forms) if only_forms else None
        self.registry_ciks = set()
        for e in L.load_entities():
            for c in re.split(r"[;,\s]+", e.get("cik") or ""):
                if c.strip().isdigit():
                    self.registry_ciks.add(int(c))
        # registry entities that have a CIK match ONLY by CIK; those without one match by name
        self.m_nocik = L.EntityMatcher([e for e in L.load_entities()
                                        if not (e.get("cik") or "").strip() and e["type"] in ("fund", "vc", "sovereign", "corporate")])
        self.gate_items = set(kw["item_codes_8k"]["gate"])
        self.item_name_to_code = {v.lower(): k for k, v in kw["item_codes_8k"]["names"].items()}
        self.all_phrases = []
        for g in ("investment_verbs", "round_vocabulary", "government_vocabulary",
                  "status_change_vocabulary", "commitment_vocabulary"):
            self.all_phrases += [p.lower() for p in kw.get(g, [])]
        self.th = kw["thresholds"]
        self.forms = set(kw["sec_forms_collected"])

    # ---- helpers
    def over_time(self):
        return time.time() > self.deadline

    def base(self, r, urls):
        info = self.u.lookup_cik(r["cik"])
        return {"accession": r["accession"], "filing_date": f"{r['date'][:4]}-{r['date'][4:6]}-{r['date'][6:]}",
                "form": r["form"], "source_link": urls["index_html"]}

    def tag_company(self, row, cik, name):
        info = self.u.lookup_cik(cik) if cik else None
        row["in_universe"] = bool(info)
        row["ticker"] = row.get("ticker") or (info["ticker"] if info else "")
        row["sectors"] = "; ".join(info["sectors"]) if info else ""
        row["entity_hits"] = "; ".join(sorted(set(self.m.match(name or "") + (["registry-cik"] if cik in self.registry_ciks else []))))
        return row

    # ---- parsers
    def parse_form4(self, r, txt, urls):
        rows = []
        for blk in L.xml_blocks(txt, "ownershipDocument"):
            root = L.parse_xml(blk)
            if root is None:
                continue
            issuer = L.find_first(root, "issuer")
            icik = L.child_text(issuer, "issuerCik") if issuer is not None else ""
            iname = L.child_text(issuer, "issuerName") if issuer is not None else ""
            sym = L.child_text(issuer, "issuerTradingSymbol") if issuer is not None else ""
            owners = L.find_all(root, "reportingOwner")
            onames, ociks = [], []
            isdir = isoff = istp = False
            title = ""
            for ro in owners:
                onames.append(L.text_of(ro, "rptOwnerName"))
                ociks.append(L.text_of(ro, "rptOwnerCik"))
                rel = L.find_first(ro, "reportingOwnerRelationship")
                if rel is not None:
                    isdir |= L.child_text(rel, "isDirector") in ("1", "true", "True")
                    isoff |= L.child_text(rel, "isOfficer") in ("1", "true", "True")
                    istp |= L.child_text(rel, "isTenPercentOwner") in ("1", "true", "True")
                    title = title or L.child_text(rel, "officerTitle")
            plan = bool(re.search(r"10b5-1", blk, re.I)) or L.text_of(root, "aff10b5One") in ("1", "true")
            txs = [(t, False) for t in L.find_all(root, "nonDerivativeTransaction")] + \
                  [(t, True) for t in L.find_all(root, "derivativeTransaction")]
            for tx, deriv in txs:
                def v(tag):
                    e = L.find_first(tx, tag)
                    return L.text_of(e, "value") if e is not None else ""
                shares = L.to_float(v("transactionShares"))
                price = L.to_float(v("transactionPricePerShare"))
                row = self.base(r, urls)
                row.update({
                    "issuer_cik": icik, "issuer_name": iname, "ticker": sym.upper(),
                    "owner_names": "; ".join(n for n in onames if n), "owner_ciks": "; ".join(c for c in ociks if c),
                    "is_director": isdir, "is_officer": isoff, "is_ten_pct_owner": istp, "officer_title": title,
                    "security": v("securityTitle"), "tx_date": v("transactionDate")[:10],
                    "code": L.text_of(tx, "transactionCode"), "acq_disp": v("transactionAcquiredDisposedCode"),
                    "shares": shares, "price": price,
                    "value_usd": round(shares * price, 2) if (shares is not None and price) else None,
                    "shares_after": L.to_float(v("sharesOwnedFollowingTransaction")),
                    "ownership": v("directOrIndirectOwnership"), "derivative": deriv, "plan_10b5_1": plan,
                })
                try:
                    c = int(icik)
                except Exception:
                    c = r["cik"]
                rows.append(self.tag_company(row, c, iname))
        return rows

    def parse_13dg(self, r, txt, urls):
        out = []
        for blk in L.xml_blocks(txt, "edgarSubmission"):
            root = L.parse_xml(blk)
            if root is None:
                continue
            stype = L.text_of(root, "submissionType")
            iname = L.text_of(root, "issuerName", "nameOfIssuer")
            icik = L.text_of(root, "issuerCik", "issuerCIK")
            cusip = L.text_of(root, "issuerCusip", "issuerCUSIP", "cusip", "issuerCusipNumber")
            sec_title = L.text_of(root, "securityTitle", "titleOfClassOfSecurities")
            names = [L.text_of(e, "reportingPersonName") or "".join(e.itertext()).strip()
                     for e in L.find_all(root, "reportingPersonName")]
            names = [n for n in dict.fromkeys(names) if n]
            pcts = [L.to_float("".join(e.itertext())) for e in L.find_all(root, "percentOfClass", "classPercent")]
            pcts = [p for p in pcts if p is not None]
            shs = [L.to_float("".join(e.itertext())) for e in L.find_all(
                root, "aggregateAmountOwned", "reportingPersonBeneficiallyOwnedAggregateNumberOfShares")]
            shs = [s for s in shs if s is not None]
            ev = L.text_of(root, "dateOfEvent", "eventDateRequiresFilingThisStatement")
            ptypes = list(dict.fromkeys("".join(e.itertext()).strip() for e in L.find_all(root, "typeOfReportingPerson")))
            purpose = L.strip_html(L.text_of(root, "item4", "purposeOfTransaction", "itemFourPurposeOfTransaction"))[:500]
            row = self.base(r, urls)
            row.update({"submission_type": stype or r["form"], "subject_cik": icik, "subject_name": iname,
                        "cusip": cusip, "security_title": sec_title, "filer_names": "; ".join(names),
                        "pct_of_class": max(pcts) if pcts else None, "shares": max(shs) if shs else None,
                        "event_date": ev, "reporting_person_types": "; ".join(p for p in ptypes if p),
                        "purpose_excerpt": purpose})
            try:
                c = int(icik)
            except Exception:
                c = None
            row = self.tag_company(row, c, iname + " " + "; ".join(names))
            out.append(row)
        return out

    def parse_form_d(self, r, txt, urls):
        out = []
        for blk in L.xml_blocks(txt, "edgarSubmission"):
            root = L.parse_xml(blk)
            if root is None:
                continue
            pi = L.find_first(root, "primaryIssuer")
            name = L.child_text(pi, "entityName") if pi is not None else ""
            state = L.text_of(pi, "stateOrCountry") if pi is not None else ""
            etype = L.child_text(pi, "entityType") if pi is not None else ""
            inc = L.text_of(pi, "jurisdictionOfInc") if pi is not None else ""
            ind = L.text_of(root, "industryGroupType")
            def amt(tag):
                return L.to_float(L.text_of(root, tag))
            related = []
            for rp in L.find_all(root, "relatedPersonInfo"):
                nm = " ".join(x for x in (L.text_of(rp, "firstName"), L.text_of(rp, "middleName"), L.text_of(rp, "lastName")) if x)
                rel = ", ".join("".join(e.itertext()).strip() for e in L.find_all(rp, "relationship"))
                related.append(f"{nm} ({rel})" if rel else nm)
            row = self.base(r, urls)
            row.update({"issuer_name": name, "issuer_cik": r["cik"], "state": state, "entity_type": etype,
                        "jurisdiction": inc, "industry_group": ind,
                        "is_amendment": L.text_of(root, "isAmendment"),
                        "date_first_sale": (L.text_of(L.find_first(root, "dateOfFirstSale"), "value")
                                            if L.find_first(root, "dateOfFirstSale") is not None else "") or "yet to occur",
                        "offering_amount": amt("totalOfferingAmount"), "amount_sold": amt("totalAmountSold"),
                        "investors_so_far": L.text_of(root, "totalNumberAlreadyInvested"),
                        "related_persons": "; ".join(related)[:600],
                        "sector_hits": "; ".join(s for s, _n in self.t.sector_hits(name + " " + ind)[:3])})
            out.append(self.tag_company(row, r["cik"], name))
        return out

    def parse_144(self, r, txt, urls):
        out = []
        for blk in L.xml_blocks(txt, "edgarSubmission"):
            root = L.parse_xml(blk)
            if root is None:
                continue
            row = self.base(r, urls)
            iname = L.text_of(root, "issuerName", "nameOfIssuer")
            row.update({"issuer_name": iname,
                        "seller": L.text_of(root, "nameOfPersonForWhoseAccountTheSecuritiesAreToBeSold"),
                        "relationship": L.text_of(root, "relationshipToIssuer", "relationshipsToIssuer"),
                        "units_to_sell": L.to_float(L.text_of(root, "noOfUnitsSold", "noOfUnitsToBeSold", "amountOfSecuritiesToBeSold")),
                        "aggregate_market_value": L.to_float(L.text_of(root, "aggregateMarketValue")),
                        "approx_sale_date": L.text_of(root, "approxSaleDate"),
                        "security_class": L.text_of(root, "securitiesClassTitle")})
            out.append(self.tag_company(row, r["cik"], iname))
        return out

    def parse_13f(self, r, urls, cutoffs):
        """Return (filer_row, holdings_rows)."""
        st, ij, err = L.fetch_text(urls["index_json"])
        if not ij:
            return {"_error": f"index {err}"}, []
        try:
            files = json.loads(ij)["directory"]["item"]
        except Exception:
            return {"_error": "index parse"}, []
        prim = next((f for f in files if f["name"].lower() == "primary_doc.xml"), None)
        info = [f for f in files if f["name"].lower().endswith(".xml") and f["name"].lower() != "primary_doc.xml"]
        if not prim:
            return {"_error": "no primary_doc"}, []
        base = urls["folder"]
        st, ptxt, err = L.fetch_text(base + prim["name"])
        root = L.parse_xml(ptxt or "")
        name = L.text_of(root, "name") if root is not None else r["company"]
        fm = L.find_first(root, "filingManager") if root is not None else None
        if fm is not None:
            name = L.text_of(fm, "name") or name
        period = L.text_of(root, "periodOfReport", "reportCalendarOrQuarter") if root is not None else ""
        entries = L.to_float(L.text_of(root, "tableEntryTotal")) if root is not None else None
        total = L.to_float(L.text_of(root, "tableValueTotal")) if root is not None else None
        is_reg = r["cik"] in self.registry_ciks or bool(self.m_nocik.match(name or r["company"]))
        concentrated = (entries is not None and entries <= self.th["f13_unknown_max_entries"]
                        and (total or 0) >= self.th["f13_unknown_min_value_usd"])
        fetch_holdings = bool(info) and (is_reg or concentrated)
        reason = "registry fund" if is_reg else ("concentrated unknown fund" if concentrated else "")
        filer = self.base(r, urls)
        filer.update({"filer_cik": r["cik"], "filer_name": name, "period": period, "entries": entries,
                      "total_value_usd": total, "holdings_fetched": False, "fetch_reason": reason or "skipped (large diversified book)",
                      "is_registry_fund": is_reg, "entity_hits": "; ".join(self.m.match(name or ""))})
        holdings = []
        if fetch_holdings:
            f = max(info, key=lambda x: int(x.get("size") or 0))
            size = int(f.get("size") or 0)
            if size <= (30_000_000 if is_reg else 12_000_000):
                st, body, ct, err = L.fetch(base + f["name"], max_bytes=32_000_000, timeout=90)
                if body:
                    xml = L.parse_xml(body.decode("utf-8", "ignore"))
                    if xml is not None:
                        for it in L.find_all(xml, "infoTable"):
                            cusip = L.child_text(it, "cusip")
                            sh = L.find_first(it, "shrsOrPrnAmt")
                            holdings.append({
                                "accession": r["accession"], "filer_cik": r["cik"], "filer_name": name, "period": period,
                                "filing_date": filer["filing_date"], "issuer": L.child_text(it, "nameOfIssuer"),
                                "class": L.child_text(it, "titleOfClass"), "cusip": cusip,
                                "value_usd": L.to_float(L.child_text(it, "value")),
                                "shares": L.to_float(L.text_of(sh, "sshPrnamt")) if sh is not None else None,
                                "shares_type": L.text_of(sh, "sshPrnamtType") if sh is not None else "",
                                "put_call": L.child_text(it, "putCall"), "source_link": urls["index_html"]})
                        filer["holdings_fetched"] = True
        return filer, holdings

    def parse_8k(self, r, urls):
        hdr = L.sec_header(r["cik"], r["accession"])
        meta = L.parse_sec_header(hdr)
        codes = []
        for nm in meta.get("items", []):
            c = self.item_name_to_code.get(nm.strip().lower())
            if c is None:
                for k, v in self.item_name_to_code.items():
                    if nm.lower().startswith(k[:25]):
                        c = v
                        break
            if c:
                codes.append(c)
        info = self.u.lookup_cik(r["cik"])
        row = self.base(r, urls)
        row.update({"company": r["company"], "cik": r["cik"], "sic": meta.get("sic", ""), "sic_desc": meta.get("sic_desc", ""),
                    "items": "; ".join(meta.get("items", [])), "item_codes": "; ".join(codes)})
        gated = [c for c in codes if c in self.gate_items]
        row["gated"] = bool(gated) or (info is not None)
        if not row["gated"] and codes:
            return None
        # fetch primary document for gated filings (or when item info could not be read)
        st, ij, err = L.fetch_text(urls["index_json"])
        excerpt = []
        money = []
        hit_groups = {}
        if ij:
            try:
                files = json.loads(ij)["directory"]["item"]
                htm = [f for f in files if f["name"].lower().endswith((".htm", ".html"))
                       and not re.search(r"(^|[-_])ex|exhibit|index", f["name"].lower())]
                # the main 8-K document is the one whose opening says CURRENT REPORT / FORM 8-K
                main_doc, body = None, None
                for cand in htm[:4]:
                    st0, head, ct0, err0 = L.fetch(urls["folder"] + cand["name"], headers={"Range": "bytes=0-6000"}, max_bytes=8000)
                    if head and re.search(rb"(?i)current\s+report|form\s+8-?k", head):
                        main_doc = cand
                        break
                if main_doc is None and htm:
                    main_doc = htm[0]
                if main_doc:
                    st, body, ct, err = L.fetch(urls["folder"] + main_doc["name"], max_bytes=1_500_000)
                    if body:
                        text = L.strip_html(body.decode("utf-8", "ignore"))
                        # keep only the item bodies (skip cover page)
                        marks = [(m.start(), m.group(1)) for m in re.finditer(r"(?i)\bItem\s+(\d\.\d{2})\b", text)]
                        body_text = text
                        if marks:
                            pieces = []
                            for i, (pos, code) in enumerate(marks):
                                end = marks[i + 1][0] if i + 1 < len(marks) else len(text)
                                if code in self.gate_items or not gated:
                                    pieces.append(text[pos:end][:6000])
                            body_text = " ".join(pieces) if pieces else text[marks[0][0]:][:8000]
                        if not self.t.is_noise(body_text[:1500]):
                            excerpt = [x for x in L.first_sentences(body_text, self.all_phrases, 6, 280)
                                       if not re.search(r"(?i)representations? and warrant|table of contents|defined terms|forward-looking|safe harbor", x)][:3]
                        money = L.money_mentions(body_text, 5)
                        hit_groups = self.t.groups_hit(body_text)
                        row["body_chars"] = len(text)
                        row["mentions_tickers"] = "; ".join(self.u.match_names_in_text(body_text[:20000]))
                        row["entity_hits"] = "; ".join(sorted(set(self.m.match(r["company"] + " " + body_text[:20000]))))
                        row["sector_hits"] = "; ".join(s for s, _n in self.t.sector_hits(body_text[:20000])[:3])
            except Exception as e:
                row["parse_error"] = str(e)[:100]
        row["keyword_groups"] = "; ".join(sorted(hit_groups))
        row["keyword_hits"] = "; ".join(sorted({p for g in hit_groups.values() for p in g}))[:300]
        row["excerpt"] = " | ".join(excerpt)
        row["amounts"] = "; ".join(money)
        row = self.tag_company(row, r["cik"], r["company"])
        if not (row["in_universe"] or row.get("entity_hits") or hit_groups):
            return None
        return row

    # ---- one filing day
    def collect_day(self, d, health):
        day = d.isoformat()
        out_dir = os.path.join(DAILY, "sec", day)
        done = os.path.join(out_dir, "_done.json")
        if os.path.exists(done) and not self.force and not self.only_forms:
            health["skipped_done"].append(day)
            return
        st, rows = L.sec_daily_index(d)
        if rows is None:
            if d >= L.today_et():
                health["pending"].append(f"{day}: index not published yet")
                return
            L.ensure_dir(out_dir)
            L.save_json(done, {"date": day, "status": "no-index", "http": st})
            health["no_index"].append(f"{day} (HTTP {st}; weekend/holiday)")
            return
        all_counts = {}
        for r in rows:
            all_counts[r["form"]] = all_counts.get(r["form"], 0) + 1
        rel, seen = [], set()
        for r in rows:
            if r["form"] in self.forms and r["accession"] not in seen:
                seen.add(r["accession"])
                rel.append(r)
        L.ensure_dir(out_dir)
        L.write_csv(os.path.join(out_dir, "index.csv"), rel, ["form", "company", "cik", "date", "accession"])
        stats = {"index_rows_all": len(rows), "relevant_unique": len(rel), "forms": {}}
        buckets = {"form4": [], "ownership13dg": [], "f13_filers": [], "f13_holdings": [], "events8k": [],
                   "form_d": [], "form144": [], "registrations": [], "periodic": []}
        per_form_n = {}
        errors = []
        incomplete = False
        for r in rel:
            if self.over_time():
                incomplete = True
                break
            f = r["form"]
            if self.only_forms and f not in self.only_forms:
                continue
            per_form_n[f] = per_form_n.get(f, 0) + 1
            if self.limit and per_form_n[f] > self.limit:
                continue
            urls = L.sec_filing_urls(r["cik"], r["accession"])
            try:
                if f in ("4", "4/A"):
                    st, txt, err = L.fetch_text(urls["txt"], max_bytes=400_000)
                    if txt is None:
                        errors.append(f"{r['accession']} form4 {err}"); continue
                    buckets["form4"] += self.parse_form4(r, txt, urls)
                elif f.startswith("SCHEDULE 13") or f.startswith("SC 13"):
                    st, txt, err = L.fetch_text(urls["txt"], max_bytes=600_000)
                    if txt is None:
                        errors.append(f"{r['accession']} 13dg {err}"); continue
                    buckets["ownership13dg"] += self.parse_13dg(r, txt, urls)
                elif f in ("D", "D/A"):
                    st, txt, err = L.fetch_text(urls["txt"], max_bytes=300_000)
                    if txt is None:
                        errors.append(f"{r['accession']} formD {err}"); continue
                    buckets["form_d"] += self.parse_form_d(r, txt, urls)
                elif f == "144":
                    st, txt, err = L.fetch_text(urls["txt"], max_bytes=300_000)
                    if txt is None:
                        errors.append(f"{r['accession']} 144 {err}"); continue
                    buckets["form144"] += self.parse_144(r, txt, urls)
                elif f in ("13F-HR", "13F-HR/A"):
                    filer, hold = self.parse_13f(r, urls, None)
                    if "_error" in filer:
                        errors.append(f"{r['accession']} 13F {filer['_error']}"); continue
                    buckets["f13_filers"].append(filer)
                    buckets["f13_holdings"] += hold
                elif f in ("8-K", "8-K/A"):
                    row = self.parse_8k(r, urls)
                    if row:
                        buckets["events8k"].append(row)
                elif f in ("S-1", "S-1/A", "F-1", "F-1/A", "424B4"):
                    row = self.base(r, urls)
                    row.update({"company": r["company"], "cik": r["cik"]})
                    buckets["registrations"].append(self.tag_company(row, r["cik"], r["company"]))
                elif f in ("10-Q", "10-K", "6-K", "20-F", "40-F"):
                    if self.u.lookup_cik(r["cik"]) or r["cik"] in self.registry_ciks:
                        row = self.base(r, urls)
                        row.update({"company": r["company"], "cik": r["cik"]})
                        buckets["periodic"].append(self.tag_company(row, r["cik"], r["company"]))
            except Exception as e:  # keep going; record
                errors.append(f"{r['accession']} {f} {type(e).__name__}: {e}")
                health.setdefault("tracebacks", []).append(traceback.format_exc()[-400:])
        form_to_bucket = {"8-K": "events8k", "8-K/A": "events8k"}
        refresh_buckets = None
        if self.only_forms:
            refresh_buckets = {form_to_bucket[f] for f in self.only_forms if f in form_to_bucket}
        for name, rws in buckets.items():
            if refresh_buckets is not None and name not in refresh_buckets:
                continue
            L.write_jsonl(os.path.join(out_dir, name + ".jsonl"), rws)
            stats["forms"][name] = len(rws)
        if self.only_forms:
            health["sec_days"][day] = {"index_rows_all": len(rows), "relevant_unique": len(rel), "forms": stats["forms"],
                                       "fetch_errors": len(errors), "refreshed_forms": sorted(self.only_forms)}
            return
        stats.update({"form_counts_all": dict(sorted(all_counts.items(), key=lambda kv: -kv[1])[:25]),
                      "fetch_errors": len(errors), "incomplete_time_budget": incomplete,
                      "per_form_processed": per_form_n})
        L.save_json(os.path.join(out_dir, "_stats.json"), stats)
        if errors:
            L.save_json(os.path.join(out_dir, "_errors.json"), errors[:200])
        if not incomplete:
            L.save_json(done, {"date": day, "status": "done", "finished_et": L.now_et().isoformat(timespec="seconds"),
                               "limit_applied": self.limit})
        health["sec_days"][day] = stats


# =============================================================================== FEEDS

def run_feeds(universe, matcher, tagger, kw, deadline, google_window, health, date):
    path = os.path.join(L.CONFIG, "feeds.csv")
    if not os.path.exists(path):
        health["feeds_error"] = "config/feeds.csv missing - run tools/build_feeds.py"
        return
    feeds = [f for f in L.read_csv(path) if (f.get("enabled") or "yes").lower() == "yes"]
    seen_path = os.path.join(DAILY, "feeds", "_seen.json")
    seen = set(L.load_json(seen_path, []) or [])
    day_path = os.path.join(DAILY, "feeds", date.isoformat() + ".jsonl")
    day_items = L.read_jsonl(day_path)
    fp = kw["feed_params"]
    stats = {}
    g_fail = 0
    for fd in feeds:
        if time.time() > deadline:
            health["feeds_incomplete"] = True
            break
        url = fd["url"]
        ftype = (fd.get("type") or "rss").lower()
        if ftype == "fedregister":
            continue  # weekly job
        if ftype == "gnews":
            if g_fail >= 3:
                stats[fd["feed_id"]] = {"skipped": "google news blocking (3 consecutive failures)"}
                continue
            url = url.replace("when%3A1d", "when%3A" + google_window).replace("when:1d", "when:" + google_window)
            gap = random.uniform(fp["gap_seconds_min"], fp["gap_seconds_max"])
        else:
            gap = 0.8
        st, body, ct, err = L.fetch(url, gap=gap, timeout=40)
        if body is None and ftype != "gnews":
            time.sleep(5)  # one patient retry for direct feeds (transient 404/5xx seen)
            st, body, ct, err = L.fetch(url, gap=gap, timeout=40)
        if body is None:
            stats[fd["feed_id"]] = {"status": st, "error": err}
            if ftype == "gnews":
                g_fail += 1
            continue
        if ftype == "gnews":
            g_fail = 0
        items = L.parse_feed(body)
        if items is None:
            stats[fd["feed_id"]] = {"status": st, "error": "not parseable as RSS/Atom (interstitial?)"}
            continue
        new = 0
        stale_dropped = 0
        if ftype == "gnews":
            # Google silently ignores `when:` for some queries; verify the window and drop old items.
            hours = {"1d": 24, "2d": 48, "3d": 72, "7d": 168, "14d": 336, "30d": 720}.get(google_window, 24) + 6
            cutoff = L.now_et() - dt.timedelta(hours=hours)
            kept = []
            for it in items:
                if it["published_iso"]:
                    try:
                        if dt.datetime.fromisoformat(it["published_iso"]) < cutoff:
                            stale_dropped += 1
                            continue
                    except ValueError:
                        pass
                kept.append(it)
            items = kept
        for it in items:
            k = L.item_key(it["title"], it["link"])
            if k in seen:
                continue
            seen.add(k)
            text = it["title"] + " . " + it["summary"]
            if tagger.is_noise(text):
                continue
            groups = tagger.groups_hit(text)
            ents = matcher.match(text)
            tick = universe.match_names_in_text(text)
            secs = [s for s, _n in tagger.sector_hits(text)[:3]]
            row = {"key": k, "captured_et": L.now_et().isoformat(timespec="seconds"),
                   "feed_id": fd["feed_id"], "feed_name": fd["name"], "category": fd["category"],
                   "title": it["title"], "link": it["link"], "published_et": it["published_iso"],
                   "source": it["source"], "summary": it["summary"][:400],
                   "entity_hits": "; ".join(ents), "tickers": "; ".join(tick),
                   "keyword_groups": "; ".join(sorted(groups)),
                   "keyword_hits": "; ".join(sorted({p for g in groups.values() for p in g}))[:200],
                   "sector_hits": "; ".join(secs)}
            day_items.append(row)
            new += 1
        stats[fd["feed_id"]] = {"status": st, "items": len(items), "new": new}
        if stale_dropped:
            stats[fd["feed_id"]]["stale_dropped"] = stale_dropped
            if stale_dropped > len(items):
                stats[fd["feed_id"]]["warning"] = "window ignored by Google - most items were stale"
    L.write_jsonl(day_path, day_items)
    keep = list(seen)[-60000:]
    L.save_json(seen_path, keep)
    health["feeds"] = stats
    health["feed_items_today"] = len(day_items)


# =============================================================================== ETF

def _num(s):
    return L.to_float(s)


def parse_ark(body):
    txt = body.decode("utf-8", "ignore")
    rows = []
    for r in csv.DictReader(txt.splitlines()):
        t = (r.get("ticker") or "").strip()
        nm = (r.get("company") or "").strip()
        if not nm or not r.get("weight (%)"):
            continue
        rows.append({"ticker": t, "name": nm, "weight_pct": _num(r.get("weight (%)")),
                     "shares": _num(r.get("shares")), "cusip": (r.get("cusip") or "").strip()})
    dates = []
    for r in csv.DictReader(txt.splitlines()):
        if r.get("date") and re.match(r"\d{1,2}/\d{1,2}/\d{4}$", r["date"]):
            dates.append(dt.datetime.strptime(r["date"], "%m/%d/%Y").date())
    asof = max(dates).strftime("%m/%d/%Y") if dates else ""
    return rows, asof


def parse_globalx(body):
    lines = body.decode("utf-8-sig", "ignore").splitlines()
    asof = ""
    m = re.search(r"as of\s+(\d{1,2}/\d{1,2}/\d{4})", " ".join(lines[:4]), re.I)
    if m:
        asof = m.group(1)
    hdr = next((i for i, l in enumerate(lines) if "Ticker" in l and "Name" in l), None)
    if hdr is None:
        return [], asof
    rows = []
    for r in csv.DictReader(lines[hdr:]):
        nm = (r.get("Name") or "").strip()
        w = _num(r.get("% of Net Assets"))
        if not nm or w is None:
            continue
        rows.append({"ticker": (r.get("Ticker") or "").strip(), "name": nm, "weight_pct": w,
                     "shares": _num(r.get("Shares Held")), "cusip": ""})
    return rows, asof


def parse_ssga(body):
    import openpyxl
    wb = openpyxl.load_workbook(BytesIO(body), read_only=True, data_only=True)
    ws = wb.active
    rows, hdr, asof = [], None, ""
    for i, row in enumerate(ws.iter_rows(values_only=True)):
        cells = [("" if c is None else str(c).strip()) for c in row]
        joined = " ".join(cells)
        if not asof:
            m = re.search(r"As of\s+(\d{1,2}[-/ ][A-Za-z0-9]{1,9}[-/ ]\d{4})", joined, re.I)
            if m:
                asof = m.group(1)
        if hdr is None:
            if "Name" in cells and any(c.lower().startswith("weight") for c in cells):
                hdr = cells
            continue
        d = dict(zip(hdr, cells))
        nm = d.get("Name", "")
        w = _num(next((v for k, v in d.items() if k.lower().startswith("weight")), None))
        if not nm or w is None:
            continue
        rows.append({"ticker": d.get("Identifier", ""), "name": nm, "weight_pct": w,
                     "shares": _num(d.get("Shares Held")), "cusip": ""})
    return rows, asof


def _table_rows(text, need_cols):
    for t in L.html_tables(text):
        if not t:
            continue
        for hi, hr in enumerate(t[:3]):
            low = [c.lower() for c in hr]
            if all(any(n in c for c in low) for n in need_cols):
                out = []
                for r in t[hi + 1:]:
                    if len(r) == len(hr):
                        out.append(dict(zip(low, r)))
                return out
    return []


def parse_firsttrust(body):
    text = body.decode("utf-8", "ignore")
    rows = _table_rows(text, ["security name", "weighting"])
    out = []
    for r in rows:
        w = _num(r.get("weighting"))
        nm = r.get("security name", "")
        if w is None or not nm:
            continue
        out.append({"ticker": r.get("identifier", ""), "name": nm, "weight_pct": w,
                    "shares": _num(r.get("shares")), "cusip": r.get("cusip", "")})
    m = re.search(r"as of\s+(\d{1,2}/\d{1,2}/\d{4})", text, re.I)
    return out, (m.group(1) if m else "")


def parse_stockanalysis(body):
    text = body.decode("utf-8", "ignore")
    rows = _table_rows(text, ["symbol", "weight"])
    out = []
    for r in rows:
        w = _num(next((v for k, v in r.items() if "weight" in k), None))
        nm = r.get("name", "")
        if w is None or not nm:
            continue
        out.append({"ticker": r.get("symbol", "").replace("$", ""), "name": nm, "weight_pct": w,
                    "shares": _num(r.get("shares")), "cusip": ""})
    return out, ""


ETF_PARSERS = {"ark_csv": parse_ark, "globalx_csv": parse_globalx, "ssga_xlsx": parse_ssga,
               "firsttrust_html": parse_firsttrust, "stockanalysis_html": parse_stockanalysis}


def run_etf(kw, deadline, date, health, force=False):
    etfs = [e for e in L.read_csv(os.path.join(L.CONFIG, "etfs.csv")) if (e.get("enabled") or "yes").lower() == "yes"]
    snap_root = os.path.join(DAILY, "etf", "snapshots")
    diffs, stats = [], {}
    thr = kw["thresholds"]["etf_weight_change_pp"]
    for e in etfs:
        if time.time() > deadline:
            health["etf_incomplete"] = True
            break
        t = e["ticker"].upper()
        out = os.path.join(snap_root, t, date.isoformat() + ".csv")
        if os.path.exists(out) and not force:
            stats[t] = {"status": "already captured"}
            continue
        url = e["url"].replace("{t}", t.lower()).replace("{T}", t).replace("{d}", date.strftime("%Y%m%d"))
        method = e["method"]
        body, st, err = None, 0, None
        if method == "globalx_csv":
            # latest business day file (today's may not be posted yet)
            for back in range(0, 4):
                dd = date - dt.timedelta(days=back)
                if dd.weekday() >= 5:
                    continue
                u2 = e["url"].replace("{t}", t.lower()).replace("{d}", dd.strftime("%Y%m%d"))
                st, body, ct, err = L.fetch(u2, gap=0.8)
                if body:
                    break
        else:
            st, body, ct, err = L.fetch(url, gap=0.8, timeout=60)
        if not body:
            stats[t] = {"status": st, "error": err or "no body", "method": method}
            continue
        try:
            rows, asof = ETF_PARSERS[method](body)
        except Exception as ex:
            stats[t] = {"status": st, "error": f"parse {type(ex).__name__}: {ex}", "method": method}
            continue
        if not rows:
            stats[t] = {"status": st, "error": "parsed 0 holdings (page layout changed or interstitial)", "method": method}
            continue
        for r in rows:
            r["etf"] = t
        L.write_csv(out, rows, ["etf", "ticker", "name", "weight_pct", "shares", "cusip"])
        partial = method == "stockanalysis_html"
        stale = False
        if asof:
            for fmt in ("%m/%d/%Y", "%d-%b-%Y", "%Y-%m-%d"):
                try:
                    stale = (date - dt.datetime.strptime(asof, fmt).date()).days > 6
                    break
                except ValueError:
                    continue
        stats[t] = {"status": st, "holdings": len(rows), "as_of": asof, "method": method,
                    "coverage": "partial (top ~25 only)" if partial else "full", "stale": stale}
        # diff vs previous snapshot
        folder = os.path.join(snap_root, t)
        prev = sorted(f for f in os.listdir(folder) if f.endswith(".csv") and f[:-4] < date.isoformat())
        if prev:
            old = {(r["ticker"] or r["name"]): r for r in L.read_csv(os.path.join(folder, prev[-1]))}
            new = {(r["ticker"] or r["name"]): r for r in rows}
            for k, r in new.items():
                if k not in old:
                    diffs.append({"etf": t, "date": date.isoformat(), "change": "entered_top25" if partial else "added", "ticker": r["ticker"], "name": r["name"],
                                  "weight_old": None, "weight_new": r["weight_pct"], "prev_snapshot": prev[-1][:-4]})
                else:
                    dw = (r["weight_pct"] or 0) - (L.to_float(old[k]["weight_pct"]) or 0)
                    if abs(dw) >= thr:
                        diffs.append({"etf": t, "date": date.isoformat(), "change": "weight_up" if dw > 0 else "weight_down",
                                      "ticker": r["ticker"], "name": r["name"], "weight_old": L.to_float(old[k]["weight_pct"]),
                                      "weight_new": r["weight_pct"], "prev_snapshot": prev[-1][:-4]})
            for k, r in old.items():
                if k not in new:
                    diffs.append({"etf": t, "date": date.isoformat(), "change": "left_top25" if partial else "removed", "ticker": r["ticker"], "name": r["name"],
                                  "weight_old": L.to_float(r["weight_pct"]), "weight_new": None, "prev_snapshot": prev[-1][:-4]})
    if diffs or True:
        L.write_csv(os.path.join(DAILY, "etf", "diffs", date.isoformat() + ".csv"), diffs,
                    ["etf", "date", "change", "ticker", "name", "weight_old", "weight_new", "prev_snapshot"])
    health["etf"] = stats
    health["etf_diff_rows"] = len(diffs)


# =============================================================================== main

def main():
    ap = argparse.ArgumentParser(description="Layer 4 daily collector")
    ap.add_argument("--date", help="ET date for feeds/etf and the last SEC date (default today ET)")
    ap.add_argument("--lookback", type=int, default=5, help="business days of SEC to (re)check, default 5")
    ap.add_argument("--only", default="sec,feeds,etf")
    ap.add_argument("--force", action="store_true", help="re-collect days already marked done")
    ap.add_argument("--max-minutes", type=float, default=60)
    ap.add_argument("--google-window", default="1d", help="Google News window, e.g. 1d or 7d for a first backfill")
    ap.add_argument("--sec-limit", type=int, help="max filings per form per day (testing)")
    ap.add_argument("--only-forms", help="re-process only these SEC forms for days in the lookback, e.g. 8-K,8-K/A (rewrites only their output)")
    a = ap.parse_args()

    date = L.parse_date(a.date) if a.date else L.today_et()
    mods = {m.strip() for m in a.only.split(",")}
    started = time.time()
    deadline = started + a.max_minutes * 60
    health = {"run_date_et": date.isoformat(), "started_et": L.now_et().isoformat(timespec="seconds"),
              "modules": sorted(mods), "sec_days": {}, "skipped_done": [], "no_index": [], "pending": []}
    kw = L.load_keywords()
    universe = L.Universe()
    matcher = L.EntityMatcher()
    tagger = L.KeywordTagger(kw)
    health["universe_tickers"] = len(universe.sectors_by_ticker)
    health["universe_ciks"] = len(universe.ciks)

    if "sec" in mods:
        sc = SecCollector(universe, matcher, tagger, kw, deadline, limit=a.sec_limit, force=a.force,
                          only_forms=[x.strip() for x in a.only_forms.split(',')] if a.only_forms else None)
        for d in L.business_days_back(date, a.lookback):
            if time.time() > deadline:
                health["sec_time_budget_exhausted"] = True
                break
            sc.collect_day(d, health)
    if "feeds" in mods:
        run_feeds(universe, matcher, tagger, kw, deadline, a.google_window, health, date)
    if "etf" in mods:
        run_etf(kw, deadline, date, health, force=a.force)

    health["finished_et"] = L.now_et().isoformat(timespec="seconds")
    health["elapsed_min"] = round((time.time() - started) / 60, 1)
    L.save_json(os.path.join(DAILY, "health", date.isoformat() + ".json"), health)

    # run log
    log = os.path.join(L.DATA, "run-log.csv")
    L.ensure_dir(L.DATA)
    new = not os.path.exists(log)
    with open(log, "a", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        if new:
            w.writerow(["finished_et", "script", "date", "modules", "sec_days_done", "sec_filings_parsed",
                        "feed_items_today", "etf_ok", "etf_failed", "elapsed_min"])
        sec_days = len(health["sec_days"])
        parsed = sum(sum(s["forms"].values()) for s in health["sec_days"].values())
        etf = health.get("etf", {})
        ok = sum(1 for v in etf.values() if "holdings" in v or v.get("status") == "already captured")
        w.writerow([health["finished_et"], "daily", date.isoformat(), "+".join(sorted(mods)), sec_days, parsed,
                    health.get("feed_items_today", ""), ok, len(etf) - ok if etf else "", health["elapsed_min"]])
    print(json.dumps({k: v for k, v in health.items() if k not in ("sec_days", "feeds", "etf")}, indent=1))
    for day, s in health["sec_days"].items():
        print(day, s["index_rows_all"], "rows;", s["relevant_unique"], "relevant;", s["forms"], "errors:", s["fetch_errors"])
    if "etf" in health:
        bad = {k: v for k, v in health["etf"].items() if "error" in v}
        print("ETF ok:", sum(1 for v in health["etf"].values() if "holdings" in v), "failed:", bad)
    if "feeds" in health:
        bad = {k: v for k, v in health["feeds"].items() if "error" in v or "skipped" in v}
        print("feeds ok:", len(health["feeds"]) - len(bad), "failed:", bad)


if __name__ == "__main__":
    main()
