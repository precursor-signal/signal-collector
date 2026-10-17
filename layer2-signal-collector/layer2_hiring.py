#!/usr/bin/env python3
"""Layer 2 WEEKLY hiring snapshot - which companies, sectors and roles are hiring more or less.

Polls the public job boards (Greenhouse, Lever, Ashby) of every company in config/ats-companies.csv,
stores each company's open roles with first-seen / last-seen dates, classifies every title into a
role family and a seniority level (config/role-families.json), and compares with the previous
snapshots.

Outputs (data/hiring/weekly/<run-date>/):
  company-hiring.csv   one row per company: open roles, change vs last snapshot and 4-snapshot average,
                       new / removed roles, role-family counts, surge and fade flags
  sector-hiring.csv    open roles by sector, change, how many companies are growing / shrinking
  family-hiring.csv    open roles by role family across all companies, change
  family-by-sector.csv role family x sector matrix (open roles)
  title-trends.csv     emerging and fading job titles (normalised), plus the most common titles
  surges.csv           company-level and company-family surges / fades that crossed the thresholds
  new-roles.csv        roles first seen this week (capped)   removed-roles.csv  roles that disappeared
  market-context.json  free market-level series (Indeed Hiring Lab) when reachable
  coverage.json / SUMMARY.md
State: data/hiring/state/<slug>.json.gz (open roles per company), data/hiring/history/<date>.csv.

The first snapshot is a BASELINE: change figures are blank until a second snapshot exists
(Lever / Ashby give posting dates, so "new this week" is already real on day one).

Usage
  python layer2_hiring.py                 # all mapped companies
  python layer2_hiring.py --limit 20      # testing
  python layer2_hiring.py --date 2026-10-21
"""
import argparse
import collections
import concurrent.futures as cf
import csv
import datetime as dt
import gzip
import json
import os
import re
import time

import l2lib as L

HIRING = os.path.join(L.DATA, "hiring")
STATE = os.path.join(HIRING, "state")
HIST = os.path.join(HIRING, "history")
WEEKLY = os.path.join(HIRING, "weekly")


# ----------------------------------------------------------------------------- taxonomy


class Taxonomy:
    def __init__(self):
        cfg = L.load_json(os.path.join(L.CONFIG, "role-families.json"), {})
        self.cfg = cfg
        self.families = [(f["id"], f["label"], re.compile(f["pattern"], re.I)) for f in cfg["families"]]
        self.labels = {f["id"]: f["label"] for f in cfg["families"]}
        self.labels[cfg.get("default_family", "other")] = "Other"
        self.default = cfg.get("default_family", "other")
        self.seniority = [(s["id"], re.compile(s["pattern"], re.I)) for s in cfg["seniority"]]
        self.default_sen = cfg.get("default_seniority", "mid")
        self.surge = cfg["surge"]
        tn = cfg.get("title_normalize", {})
        self.drop = set(w.lower() for w in tn.get("drop_words", []))
        self.seps = tn.get("strip_after_separators", [])

    def family(self, title, dept=""):
        t = title or ""
        for fid, _lbl, rx in self.families:
            if rx.search(t):
                return fid
        # the department can rescue a vague title ("Member of Technical Staff" in "Research")
        for fid, _lbl, rx in self.families[:7]:
            if dept and rx.search(dept):
                return fid
        return self.default

    def level(self, title):
        for sid, rx in self.seniority:
            if rx.search(title or ""):
                return sid
        return self.default_sen

    def norm_title(self, title):
        t = (title or "").lower()
        t = re.sub(r"\([^)]*\)", " ", t)
        for s in self.seps:
            if s in t:
                t = t.split(s)[0]
        words = [w for w in re.findall(r"[a-z0-9&+#\./]+", t) if w not in self.drop]
        return " ".join(words).strip()


# ----------------------------------------------------------------------------- ATS fetchers


def _json(url, **kw):
    st, body, ct, err = L.fetch(url, timeout=40, retries=1, gap=0.25, **kw)
    if body is None:
        return None, f"HTTP {st} {err or ''}".strip()
    try:
        return json.loads(body.decode("utf-8", "ignore")), None
    except Exception as e:
        return None, f"bad json: {e}"


def fetch_greenhouse(token):
    js, err = _json(f"https://boards-api.greenhouse.io/v1/boards/{token}/jobs")
    if js is None:
        return None, err
    dept_of = {}
    djs, _e = _json(f"https://boards-api.greenhouse.io/v1/boards/{token}/departments")
    if isinstance(djs, dict):
        for d in djs.get("departments", []):
            for j in d.get("jobs", []) or []:
                dept_of[j.get("id")] = d.get("name", "")
    out = []
    for j in js.get("jobs", []):
        out.append({"id": str(j.get("id")), "title": j.get("title", ""), "dept": dept_of.get(j.get("id"), ""),
                    "location": (j.get("location") or {}).get("name", ""), "posted": "", "updated": (j.get("updated_at") or "")[:10],
                    "url": j.get("absolute_url", "")})
    return out, None


def fetch_lever(token):
    js, err = _json(f"https://api.lever.co/v0/postings/{token}?mode=json")
    if js is None:
        return None, err
    out = []
    for j in js if isinstance(js, list) else []:
        cat = j.get("categories") or {}
        ms = j.get("createdAt")
        posted = dt.datetime.fromtimestamp(ms / 1000, dt.timezone.utc).date().isoformat() if isinstance(ms, (int, float)) else ""
        out.append({"id": str(j.get("id")), "title": j.get("text", ""), "dept": cat.get("department") or cat.get("team") or "",
                    "location": cat.get("location", ""), "posted": posted, "updated": "", "url": j.get("hostedUrl", "")})
    return out, None


def fetch_ashby(token):
    js, err = _json(f"https://api.ashbyhq.com/posting-api/job-board/{token}")
    if js is None:
        return None, err
    out = []
    for j in js.get("jobs", []):
        if j.get("isListed") is False:
            continue
        out.append({"id": str(j.get("id")), "title": j.get("title", ""), "dept": j.get("department") or j.get("team") or "",
                    "location": j.get("location", ""), "posted": (j.get("publishedAt") or "")[:10], "updated": "",
                    "url": j.get("jobUrl", "")})
    return out, None


FETCH = {"greenhouse": fetch_greenhouse, "lever": fetch_lever, "ashby": fetch_ashby}


# ----------------------------------------------------------------------------- state


def slug_of(company):
    return re.sub(r"[^a-z0-9]+", "-", company.lower()).strip("-")[:80]


def load_state(slug):
    p = os.path.join(STATE, slug + ".json.gz")
    if not os.path.exists(p):
        return None
    try:
        with gzip.open(p, "rt", encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return None


def save_state(slug, obj):
    L.ensure_dir(STATE)
    with gzip.open(os.path.join(STATE, slug + ".json.gz"), "wt", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, separators=(",", ":"))


def history_dates(before):
    if not os.path.isdir(HIST):
        return []
    return sorted(f[:-4] for f in os.listdir(HIST) if re.fullmatch(r"\d{4}-\d{2}-\d{2}\.csv", f) and f[:-4] < before)


def load_history_rows(date):
    p = os.path.join(HIST, date + ".csv")
    return {r["company"]: r for r in L.read_csv(p)} if os.path.exists(p) else {}


# ----------------------------------------------------------------------------- per company


def process_company(row, tax, today, prev_date):
    ats, token, company = row["ats"], row["token"], row["company"]
    fn = FETCH.get(ats)
    if not fn:
        return {"company": company, "error": f"unsupported ats {ats}"}
    jobs, err = fn(token)
    if jobs is None:
        return {"company": company, "error": err}
    slug = slug_of(company)
    prev = load_state(slug)
    baseline = prev is None
    state = prev or {"company": company, "jobs": {}}
    cur_ids = set()
    new_roles, removed_roles = [], []
    week_ago = (today - dt.timedelta(days=7)).isoformat()
    for j in jobs:
        jid = j["id"]
        cur_ids.add(jid)
        fam = tax.family(j["title"], j["dept"])
        lev = tax.level(j["title"])
        rec = state["jobs"].get(jid)
        if rec is None or rec.get("closed"):
            rec = {"t": j["title"], "d": j["dept"], "l": j["location"], "f": fam, "s": lev,
                   "first": today.isoformat(), "posted": j["posted"], "last": today.isoformat(), "u": j["url"]}
            state["jobs"][jid] = rec
            is_new = (not baseline) or (j["posted"] and j["posted"] >= week_ago)
            if is_new:
                new_roles.append({"company": company, "title": j["title"], "department": j["dept"], "family": fam, "seniority": lev,
                                  "location": j["location"], "posted": j["posted"], "url": j["url"]})
        else:
            rec.update({"t": j["title"], "d": j["dept"], "l": j["location"], "f": fam, "s": lev, "last": today.isoformat(), "u": j["url"]})
            if j["posted"] and not rec.get("posted"):
                rec["posted"] = j["posted"]
    for jid, rec in state["jobs"].items():
        if jid not in cur_ids and not rec.get("closed"):
            rec["closed"] = today.isoformat()
            if not baseline:
                removed_roles.append({"company": company, "title": rec["t"], "department": rec["d"], "family": rec["f"],
                                      "seniority": rec["s"], "first_seen": rec["first"], "closed": rec["closed"]})
    # purge roles closed more than 90 days ago
    cutoff = (today - dt.timedelta(days=90)).isoformat()
    state["jobs"] = {k: v for k, v in state["jobs"].items() if not v.get("closed") or v["closed"] >= cutoff}
    save_state(slug, state)
    open_jobs = [v for k, v in state["jobs"].items() if k in cur_ids]
    fam_counts = collections.Counter(v["f"] for v in open_jobs)
    sen_counts = collections.Counter(v["s"] for v in open_jobs)
    titles = collections.Counter(tax.norm_title(v["t"]) for v in open_jobs)
    return {"company": company, "ticker": row.get("ticker", ""), "private": row.get("private", ""), "sectors": row.get("sectors", ""),
            "ats": ats, "token": token, "total_open": len(open_jobs), "baseline": baseline,
            "new_roles": new_roles, "removed_roles": removed_roles,
            "fam": dict(fam_counts), "sen": dict(sen_counts), "titles": titles}


# ----------------------------------------------------------------------------- market context


def market_context(health, outdir):
    """Free market-level series: Indeed Hiring Lab AI tracker and postings index (CC BY 4.0). Best effort."""
    ctx = {}
    for name, api in (("indeed_ai_tracker", "https://api.github.com/repos/hiring-lab/ai-tracker/contents"),
                      ("indeed_job_postings_index", "https://api.github.com/repos/hiring-lab/job_postings_tracker/contents")):
        js, err = _json(api)
        if not isinstance(js, list):
            ctx[name] = {"collected": False, "reason": err or "not a listing"}
            continue
        csvs = [f for f in js if f.get("name", "").lower().endswith(".csv")]
        out = {"collected": True, "files": [f["name"] for f in csvs][:12], "latest": {}}
        for f in csvs[:12]:
            st, txt, e = L.fetch_text(f["download_url"], timeout=60)
            if not txt:
                continue
            rows = list(csv.reader(txt.splitlines()))
            if len(rows) < 2:
                continue
            head, last = rows[0], rows[-1]
            out["latest"][f["name"]] = {"columns": head[:12], "last_row": last[:12], "rows": len(rows) - 1}
        ctx[name] = out
    ctx["not_collected_by_script"] = ["Revelio Public Labor Statistics (monthly page, no stable data file found) -> agents",
                                      "QED-C Quantum Workforce Monitor (blog report, text only) -> agents",
                                      "LinkedIn Economic Graph reports (PDF) -> agents"]
    L.save_json(os.path.join(outdir, "market-context.json"), ctx)
    health["market_context"] = {k: (v.get("collected") if isinstance(v, dict) else None) for k, v in ctx.items() if isinstance(v, dict)}


# ----------------------------------------------------------------------------- main


_CANON = None


def canon_sectors(raw):
    """Map free-text sector strings (private watchlist) to the 26 canonical sector names; unknown -> Unassigned."""
    global _CANON
    if _CANON is None:
        secs = L.load_sectors()
        _CANON = [(x["sector"], x["sector"].lower(), [k.lower() for k in x["keywords"]]) for x in secs]
    out = []
    for tok in [t.strip() for t in (raw or "").split(";") if t.strip()]:
        low = tok.lower()
        hit = None
        for name, nl, kws in _CANON:
            if low == nl:
                hit = name
                break
        if not hit:
            for name, nl, kws in _CANON:
                if nl in low or low in nl or any(re.search(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])", low) for k in kws if len(k) >= 4):
                    hit = name
                    break
        if hit and hit not in out:
            out.append(hit)
    return out or ["Unassigned"]


def pct(now, prev):
    if prev in (None, "", 0):
        return ""
    return round(100.0 * (now - prev) / prev, 1)


def main():
    ap = argparse.ArgumentParser(description="Layer 2 weekly hiring snapshot")
    ap.add_argument("--date", help="snapshot date (default today ET)")
    ap.add_argument("--limit", type=int)
    ap.add_argument("--workers", type=int, default=4)
    a = ap.parse_args()
    today = L.parse_date(a.date) if a.date else L.today_et()
    started = time.time()
    tax = Taxonomy()
    sur = tax.surge
    comp_path = os.path.join(L.CONFIG, "ats-companies.csv")
    companies = [r for r in L.read_csv(comp_path) if (r.get("status") or "active") == "active"] if os.path.exists(comp_path) else []
    if a.limit:
        companies = companies[: a.limit]
    health = {"run_date_et": today.isoformat(), "started_et": L.now_et().isoformat(timespec="seconds"), "companies_mapped": len(companies)}
    pdates = history_dates(today.isoformat())
    prev_date = pdates[-1] if pdates else None
    prev_rows = load_history_rows(prev_date) if prev_date else {}
    trailing = [load_history_rows(d) for d in pdates[-4:]]
    health["prior_snapshot"] = prev_date
    outdir = L.ensure_dir(os.path.join(WEEKLY, today.isoformat()))

    results, errors = [], {}
    with cf.ThreadPoolExecutor(max_workers=a.workers) as ex:
        futs = {ex.submit(process_company, r, tax, today, prev_date): r for r in companies}
        for fu in cf.as_completed(futs):
            try:
                res = fu.result()
            except Exception as e:  # one broken board never stops the run
                res = {"company": futs[fu]["company"], "error": f"{type(e).__name__}: {e}"}
            if res.get("error"):
                errors[res["company"]] = res["error"]
            else:
                results.append(res)
    results.sort(key=lambda r: r["company"].lower())
    health["boards_ok"], health["boards_failed"] = len(results), len(errors)
    health["board_errors"] = dict(list(errors.items())[:30])

    # ---- company rows
    comp_rows, surge_rows, hist_rows = [], [], []
    new_all, removed_all = [], []
    for r in results:
        co = r["company"]
        p = prev_rows.get(co)
        prev_total = int(p["total_open"]) if p and p.get("total_open", "") != "" else None
        traj = [int(t[co]["total_open"]) for t in trailing if co in t and t[co].get("total_open", "") != ""]
        avg4 = round(sum(traj) / len(traj), 1) if traj else ""
        change = (r["total_open"] - prev_total) if prev_total is not None else ""
        wow = pct(r["total_open"], prev_total) if prev_total is not None else ""
        flag = ""
        if prev_total is not None:
            if wow != "" and wow >= sur["company_total_pct"] and change >= sur["company_total_min_roles"]:
                flag = "SURGE"
            elif wow != "" and wow <= sur["fade_pct"] and -change >= sur["fade_min_roles"]:
                flag = "FADE"
        elif r["baseline"]:
            flag = "baseline"
        prev_fam = json.loads(p["families_json"]) if p and p.get("families_json") else {}
        for fid, n in r["fam"].items():
            pf = prev_fam.get(fid)
            if pf is not None and pf > 0:
                fw = pct(n, pf)
                if fw != "" and fw >= sur["company_family_pct"] and n - pf >= sur["company_family_min_roles"]:
                    surge_rows.append({"company": co, "ticker": r["ticker"], "sectors": r["sectors"], "kind": "family-surge",
                                       "family": tax.labels.get(fid, fid), "open_now": n, "open_prior": pf, "change_pct": fw})
                elif fw != "" and fw <= sur["fade_pct"] and pf - n >= sur["fade_min_roles"]:
                    surge_rows.append({"company": co, "ticker": r["ticker"], "sectors": r["sectors"], "kind": "family-fade",
                                       "family": tax.labels.get(fid, fid), "open_now": n, "open_prior": pf, "change_pct": fw})
        if flag in ("SURGE", "FADE"):
            surge_rows.append({"company": co, "ticker": r["ticker"], "sectors": r["sectors"], "kind": "company-" + flag.lower(),
                               "family": "(all roles)", "open_now": r["total_open"], "open_prior": prev_total, "change_pct": wow})
        top_fams = sorted(r["fam"].items(), key=lambda x: -x[1])[:4]
        comp_rows.append({"company": co, "ticker": r["ticker"], "private": r["private"], "sectors": r["sectors"], "ats": r["ats"],
                          "total_open": r["total_open"], "prior_total": prev_total if prev_total is not None else "",
                          "change": change, "change_pct": wow, "avg_prior_4": avg4,
                          "new_roles": len(r["new_roles"]), "removed_roles": len(r["removed_roles"]),
                          "top_families": "; ".join(f"{tax.labels.get(k, k)} {v}" for k, v in top_fams),
                          "senior_plus_share_pct": round(100.0 * (r["sen"].get("staff_plus", 0) + r["sen"].get("leadership", 0)) / max(1, r["total_open"]), 1),
                          "flag": flag, "families_json": json.dumps(r["fam"], sort_keys=True)})
        hist_rows.append({"company": co, "ticker": r["ticker"], "private": r["private"], "sectors": r["sectors"], "ats": r["ats"],
                          "total_open": r["total_open"], "families_json": json.dumps(r["fam"], sort_keys=True),
                          "new_roles": len(r["new_roles"]), "removed_roles": len(r["removed_roles"])})
        new_all += r["new_roles"]
        removed_all += r["removed_roles"]
    # companies that failed today keep their last known history row out of the totals (reported in coverage)

    # ---- sector and family roll-ups
    def sector_list(s):
        return canon_sectors(s)
    sec_now, sec_prev = collections.defaultdict(lambda: collections.Counter()), collections.defaultdict(lambda: collections.Counter())
    sec_breadth = collections.defaultdict(lambda: {"up": 0, "down": 0, "flat": 0, "companies": 0})
    fam_now, fam_prev, fam_matched = collections.Counter(), collections.Counter(), collections.Counter()
    fam_sec = collections.defaultdict(collections.Counter)
    for r, cr in zip(results, comp_rows):
        prevj = json.loads(prev_rows[r["company"]]["families_json"]) if r["company"] in prev_rows and prev_rows[r["company"]].get("families_json") else None
        for sct in sector_list(r["sectors"]):
            sec_now[sct]["total"] += r["total_open"]
            sec_breadth[sct]["companies"] += 1
            if prevj is not None:
                sec_prev[sct]["total"] += sum(prevj.values())
                sec_now[sct]["matched"] += r["total_open"]
                sec_breadth[sct]["compared"] = sec_breadth[sct].get("compared", 0) + 1
            if cr["change_pct"] != "":
                if cr["change_pct"] >= 5:
                    sec_breadth[sct]["up"] += 1
                elif cr["change_pct"] <= -5:
                    sec_breadth[sct]["down"] += 1
                else:
                    sec_breadth[sct]["flat"] += 1
            for fid, n in r["fam"].items():
                fam_sec[fid][sct] += n
        for fid, n in r["fam"].items():
            fam_now[fid] += n
            if prevj is not None:
                fam_matched[fid] += n
        if prevj is not None:
            for fid, n in prevj.items():
                fam_prev[fid] += n
    have_prev = bool(prev_rows)
    sector_rows = []
    for sct, c in sorted(sec_now.items(), key=lambda x: -x[1]["total"]):
        pt = sec_prev[sct]["total"] if have_prev and sec_breadth[sct].get("compared") else None
        mt = c["matched"]
        sector_rows.append({"sector": sct, "companies_tracked": sec_breadth[sct]["companies"], "companies_compared": sec_breadth[sct].get("compared", 0),
                            "total_open": c["total"],
                            "prior_total": pt if pt is not None else "", "change": (mt - pt) if pt is not None else "",
                            "change_pct": pct(mt, pt) if pt else "", "companies_up_5pct": sec_breadth[sct]["up"],
                            "companies_down_5pct": sec_breadth[sct]["down"], "companies_flat": sec_breadth[sct]["flat"]})
    family_rows = []
    for fid, n in sorted(fam_now.items(), key=lambda x: -x[1]):
        pn = fam_prev[fid] if have_prev else None
        family_rows.append({"family": tax.labels.get(fid, fid), "family_id": fid, "total_open": n, "prior_total": pn if pn is not None else "",
                            "change": (fam_matched[fid] - pn) if pn is not None else "", "change_pct": pct(fam_matched[fid], pn) if pn else ""})
    all_secs = sorted(sec_now)
    fam_sec_rows = []
    for fid, c in sorted(fam_sec.items(), key=lambda x: -sum(x[1].values())):
        row = {"family": tax.labels.get(fid, fid)}
        for sct in all_secs:
            row[sct] = c.get(sct, 0)
        fam_sec_rows.append(row)

    # ---- titles
    titles_now = collections.Counter()
    for r in results:
        titles_now.update(r["titles"])
    prior_title_sets = []
    for d in pdates[-4:]:
        p = os.path.join(HIST, f"titles-{d}.json")
        prior_title_sets.append(L.load_json(p, {}) or {})
    title_rows = []
    for t, n in titles_now.most_common(4000):
        if not t:
            continue
        pri = [ps.get(t, 0) for ps in prior_title_sets]
        avg = round(sum(pri) / len(pri), 1) if pri else ""
        kind = ""
        if pri:
            if n >= sur["emerging_title_min_now"] and (sum(pri) / len(pri)) <= sur["emerging_title_max_prior_avg"]:
                kind = "emerging"
        title_rows.append({"title": t, "family": tax.family(t), "open_now": n, "prior_avg": avg, "kind": kind})
    if prior_title_sets:
        seen_now = set(titles_now)
        for t in set().union(*[set(ps) for ps in prior_title_sets]):
            pri = [ps.get(t, 0) for ps in prior_title_sets]
            if max(pri) >= sur["fading_title_min_prior"] and titles_now.get(t, 0) <= max(pri) / 2:
                title_rows.append({"title": t, "family": tax.family(t), "open_now": titles_now.get(t, 0),
                                   "prior_avg": round(sum(pri) / len(pri), 1), "kind": "fading"})
    title_rows.sort(key=lambda r: ({"emerging": 0, "fading": 1}.get(r["kind"], 2), -r["open_now"]))
    L.ensure_dir(HIST)
    L.save_json(os.path.join(HIST, f"titles-{today.isoformat()}.json"), dict(titles_now.most_common(4000)))
    L.write_csv(os.path.join(HIST, today.isoformat() + ".csv"), hist_rows,
                ["company", "ticker", "private", "sectors", "ats", "total_open", "families_json", "new_roles", "removed_roles"])

    # ---- write
    market_context(health, outdir)
    surge_rows.sort(key=lambda r: (r["kind"], -abs(r["change_pct"] if r["change_pct"] != "" else 0)))
    outs = {"company-hiring": comp_rows, "sector-hiring": sector_rows, "family-hiring": family_rows, "family-by-sector": fam_sec_rows,
            "title-trends": title_rows, "surges": surge_rows, "new-roles": new_all[:6000], "removed-roles": removed_all[:6000]}
    for name, rows in outs.items():
        L.write_csv(os.path.join(outdir, name + ".csv"), rows)
    comp_ok = len(results)
    health["rows"] = {k: len(v) for k, v in outs.items()}
    health["baseline_snapshot"] = prev_date is None
    health["total_open_roles"] = sum(r["total_open"] for r in results)
    health["by_ats"] = dict(collections.Counter(r["ats"] for r in results))
    health["not_collected_by_script"] = [
        "Companies on Workday, Oracle, iCIMS, SuccessFactors, Taleo, SmartRecruiters or their own career sites (no public JSON board) -> agents (routine 03) read the career page and LinkedIn / Indeed / Glassdoor counts",
        "Public mega-caps (Nvidia, Google, Microsoft, Amazon, Intel, AMD, Broadcom, Qualcomm, Micron ...) -> agents, aggregate counts by role family",
        "LinkedIn, Indeed and Glassdoor job counts (scripted access is blocked and against their terms) -> agents, manual spot-checks only",
        "Posting dates for Greenhouse boards (list endpoint gives last-updated only) -> first-seen date is used",
        "Removed roles cannot be told apart as filled / cancelled / moved to another board",
    ]
    if errors:
        health["not_collected_by_script"].append(f"{len(errors)} mapped boards failed today ({', '.join(list(errors)[:8])}...) -> agents or next run")
    health["finished_et"] = L.now_et().isoformat(timespec="seconds")
    health["elapsed_min"] = round((time.time() - started) / 60, 1)
    L.save_json(os.path.join(outdir, "coverage.json"), health)
    lines = [f"# Layer 2 hiring snapshot - {today}", "",
             f"Boards read: {comp_ok} of {len(companies)} mapped ({len(errors)} failed). Open roles: {health['total_open_roles']:,}. "
             f"Prior snapshot: {prev_date or 'none - this is the BASELINE'}.", "", "## Sectors (open roles)", ""]
    for s in sector_rows[:30]:
        lines.append(f"- {s['sector']}: {s['total_open']:,} across {s['companies_tracked']} companies"
                     + (f" ({s['change_pct']:+}% vs prior; {s['companies_up_5pct']} up / {s['companies_down_5pct']} down)" if s["change_pct"] != "" else ""))
    lines += ["", "## Role families (open roles)", ""]
    for f in family_rows:
        lines.append(f"- {f['family']}: {f['total_open']:,}" + (f" ({f['change_pct']:+}%)" if f["change_pct"] != "" else ""))
    lines += ["", f"## Surges / fades this snapshot: {len(surge_rows)}", ""]
    for s in surge_rows[:25]:
        lines.append(f"- {s['company']} [{s['kind']}] {s['family']}: {s['open_prior']} -> {s['open_now']} ({s['change_pct']:+}%)")
    lines += ["", "## Not collected by script (agents must cover)", ""] + [f"- {x}" for x in health["not_collected_by_script"]]
    with open(os.path.join(outdir, "SUMMARY.md"), "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    print(json.dumps({k: health[k] for k in ("run_date_et", "companies_mapped", "boards_ok", "boards_failed", "total_open_roles", "by_ats",
                                              "baseline_snapshot", "rows", "elapsed_min")}, indent=1))
    if errors:
        print("board errors:", dict(list(errors.items())[:10]))


if __name__ == "__main__":
    main()
