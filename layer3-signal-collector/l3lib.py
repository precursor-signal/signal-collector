"""Shared helpers for the Layer 3 signal collectors (standard library + openpyxl only).

Layer 3 = supply-chain and procurement signal DISCOVERY. Nothing here scores, tiers or judges
a stock. It collects, tags and stores dated, sourced facts about physical and procurement
commitments: buyer-supplier deals, capacity and facility commitments, power procurement,
government awards, orders and backlog, and supplier disclosures.

Funding rounds, IPOs and pure M&A belong to Layer 4; items that only match those are stored
but not kept as Layer 3 signals (see keywords.json "layer4_events").
"""
import csv
import datetime as dt
import email.utils
import hashlib
import html
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config")
CACHE = os.path.join(CONFIG, "cache")
DATA = os.path.join(ROOT, "data")
ET_TZ = ZoneInfo("America/New_York")

SEC_UA = "PrecursorSignal Layer3 collector thilini.hettiarachchi@precursorsignal.com"
WEB_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) "
          "Chrome/124.0 Safari/537.36 research thilini.hettiarachchi@precursorsignal.com")

# ----------------------------------------------------------------------------- time


def now_et():
    return dt.datetime.now(ET_TZ)


def today_et():
    return now_et().date()


def parse_date(s):
    return dt.datetime.strptime(s, "%Y-%m-%d").date()


def business_days_back(end, n):
    """The n weekdays ending at `end` (inclusive if a weekday), oldest first."""
    out, d = [], end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(out))


# ----------------------------------------------------------------------------- files


def ensure_dir(p):
    os.makedirs(p, exist_ok=True)
    return p


def read_csv(path):
    with open(path, encoding="utf-8-sig", newline="") as f:
        return list(csv.DictReader(f))


def write_csv(path, rows, fields=None):
    ensure_dir(os.path.dirname(path))
    if fields is None:
        fields = []
        for r in rows:
            for k in r:
                if k not in fields:
                    fields.append(k)
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=fields, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: ("" if r.get(k) is None else r.get(k)) for k in fields})


def write_jsonl(path, rows):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        for r in rows:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")


def read_jsonl(path):
    if not os.path.exists(path):
        return []
    out = []
    with open(path, encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if line:
                try:
                    out.append(json.loads(line))
                except Exception:
                    pass
    return out


def load_json(path, default=None):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)


# ----------------------------------------------------------------------------- http

_last_hit = {}
_HOST_GAP = {"www.sec.gov": 0.17, "data.sec.gov": 0.17, "efts.sec.gov": 0.3, "api.usaspending.gov": 0.4}


def fetch(url, ua=None, timeout=40, headers=None, retries=2, data=None, gap=None, max_bytes=None, method=None):
    """Return (status, body_bytes_or_None, content_type, error_or_None). Never raises."""
    host = urllib.parse.urlparse(url).netloc.lower()
    if ua is None:
        ua = SEC_UA if host.endswith("sec.gov") else WEB_UA
    min_gap = gap if gap is not None else _HOST_GAP.get(host, 0.6)
    last_err = None
    for attempt in range(retries + 1):
        wait = min_gap - (time.time() - _last_hit.get(host, 0))
        if wait > 0:
            time.sleep(wait)
        _last_hit[host] = time.time()
        try:
            h = {"User-Agent": ua, "Accept-Encoding": "identity"}
            if headers:
                h.update(headers)
            req = urllib.request.Request(url, headers=h, data=data, method=method)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(max_bytes) if max_bytes else r.read()
                return r.status, body, r.headers.get("Content-Type", ""), None
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (404, 403, 401, 410, 400, 422):
                return e.code, None, "", last_err
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
    return 0, None, "", last_err


def fetch_text(url, **kw):
    st, body, ct, err = fetch(url, **kw)
    if body is None:
        return st, None, err
    return st, body.decode("utf-8", "ignore"), err


def post_json(url, payload, **kw):
    body = json.dumps(payload).encode()
    st, resp, ct, err = fetch(url, data=body, headers={"Content-Type": "application/json"}, **kw)
    if resp is None:
        return st, None, err
    try:
        return st, json.loads(resp.decode("utf-8", "ignore")), None
    except Exception as e:
        return st, None, f"bad json: {e}"


# ----------------------------------------------------------------------------- text

_TAG = re.compile(r"<[^>]+>")
_WS = re.compile(r"\s+")


def strip_html(s):
    if not s:
        return ""
    s = re.sub(r"(?is)<(script|style).*?</\1>", " ", s)
    s = _TAG.sub(" ", s)
    s = html.unescape(s)
    return _WS.sub(" ", s).strip()


def norm_name(s):
    s = (s or "").upper()
    s = re.sub(r"[^A-Z0-9 ]", " ", s)
    s = re.sub(r"\b(THE|INC|INCORPORATED|CORP|CORPORATION|CO|COMPANY|LTD|LIMITED|LLC|LP|L P|PLC|HOLDINGS?|HLDGS?|GROUP|NV|SA|AG|CLASS [A-Z]|COM|COMMON STOCK|ORD|ADR)\b", " ", s)
    return _WS.sub(" ", s).strip()


def split_sentences(text):
    return [s.strip() for s in re.split(r"(?<=[.!?])\s+(?=[A-Z0-9\"'(])", text) if s.strip()]


# ----------------------------------------------------------------------------- xml / feeds


def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def to_iso(s):
    if not s:
        return ""
    try:
        t = email.utils.parsedate_to_datetime(s)
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(ET_TZ).isoformat(timespec="seconds")
    except Exception:
        pass
    try:
        t = dt.datetime.fromisoformat(s.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=dt.timezone.utc)
        return t.astimezone(ET_TZ).isoformat(timespec="seconds")
    except Exception:
        return ""


def parse_feed(xml_bytes):
    """RSS 2.0 / Atom -> list of dict(title, link, published, summary, source). None if not a feed."""
    try:
        txt = xml_bytes.decode("utf-8", "ignore").lstrip("﻿")
        root = ET.fromstring(txt)
    except Exception:
        return None
    if _local(root.tag).lower() not in ("rss", "feed", "rdf", "rss1", "channel"):
        return None  # an HTML interstitial or error page that happens to be well-formed
    items = []
    for it in root.iter():
        if _local(it.tag).lower() not in ("item", "entry"):
            continue
        d = {"title": "", "link": "", "published": "", "summary": "", "source": ""}
        for c in it:
            cn = _local(c.tag).lower()
            if cn == "title":
                d["title"] = strip_html("".join(c.itertext()))
            elif cn == "link":
                d["link"] = (c.attrib.get("href") or "".join(c.itertext())).strip()
            elif cn in ("pubdate", "published", "updated", "date"):
                if not d["published"]:
                    d["published"] = "".join(c.itertext()).strip()
            elif cn in ("description", "summary", "content", "encoded"):
                if len(d["summary"]) < 5:
                    d["summary"] = strip_html("".join(c.itertext()))[:900]
            elif cn == "source":
                d["source"] = strip_html("".join(c.itertext()))
        d["published_iso"] = to_iso(d["published"])
        items.append(d)
    return items


def item_key(title, link):
    host = urllib.parse.urlparse(link).netloc.lower()
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return hashlib.sha1((t + "|" + host).encode()).hexdigest()[:16]


def deal_key(*parts):
    t = "|".join(re.sub(r"[^a-z0-9]+", " ", str(p).lower()).strip() for p in parts)
    return hashlib.sha1(t.encode()).hexdigest()[:14]


# ----------------------------------------------------------------------------- SEC


def sec_index_url(d):
    q = (d.month - 1) // 3 + 1
    return f"https://www.sec.gov/Archives/edgar/daily-index/{d.year}/QTR{q}/form.{d.strftime('%Y%m%d')}.idx"


_IDX_ROW = re.compile(r"^(\S.*?)\s{2,}(.+?)\s+(\d{1,10})\s+(\d{8})\s+(edgar/data/(\d+)/(\d{10}-\d{2}-\d{6})\.txt)\s*$")


def sec_daily_index(d):
    """Return (status, rows). rows: dicts form, company, cik, date, accession, path."""
    st, txt, err = fetch_text(sec_index_url(d))
    if txt is None:
        return st, None
    rows = []
    for line in txt.split("\n"):
        m = _IDX_ROW.match(line.rstrip("\r"))
        if m:
            rows.append({"form": m.group(1).strip(), "company": m.group(2).strip(), "cik": int(m.group(3)),
                         "date": m.group(4), "accession": m.group(7), "path": m.group(5)})
    return st, rows


def sec_filing_urls(cik, accession):
    nodash = accession.replace("-", "")
    return {
        "txt": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{accession}.txt",
        "folder": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/",
        "index_html": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/{accession}-index.htm",
        "index_json": f"https://www.sec.gov/Archives/edgar/data/{int(cik)}/{nodash}/index.json",
    }


def sec_header(cik, accession, nbytes=9000):
    url = sec_filing_urls(cik, accession)["txt"]
    st, body, ct, err = fetch(url, headers={"Range": f"bytes=0-{nbytes}"}, max_bytes=nbytes + 2000)
    return body.decode("utf-8", "ignore") if body else None


def parse_sec_header(h):
    out = {}
    if not h:
        return out
    m = re.search(r"STANDARD INDUSTRIAL CLASSIFICATION:\s*(.*?)\s*\[(\d{4})\]", h)
    if m:
        out["sic"], out["sic_desc"] = m.group(2), m.group(1).strip()
    out["items"] = [x.strip() for x in re.findall(r"ITEM INFORMATION:\s*(.+)", h)]
    m = re.search(r"CONFORMED PERIOD OF REPORT:\s*(\d{8})", h)
    if m:
        out["period"] = m.group(1)
    return out


def sec_company_tickers():
    """cik -> {ticker, title, exchange}; cached for the day."""
    path = os.path.join(CACHE, "company_tickers_exchange.json")
    ensure_dir(CACHE)
    fresh = os.path.exists(path) and (time.time() - os.path.getmtime(path) < 20 * 3600)
    if not fresh:
        st, txt, err = fetch_text("https://www.sec.gov/files/company_tickers_exchange.json")
        if txt:
            with open(path, "w", encoding="utf-8") as f:
                f.write(txt)
    d = load_json(path, {}) or {}
    out, fields = {}, d.get("fields") or []
    for row in d.get("data") or []:
        r = dict(zip(fields, row))
        try:
            cik = int(r["cik"])
        except Exception:
            continue
        out.setdefault(cik, {"ticker": r.get("ticker", ""), "title": r.get("name", ""), "exchange": r.get("exchange", "")})
    return out


# ----------------------------------------------------------------------------- registry


def load_keywords():
    return load_json(os.path.join(CONFIG, "keywords.json"), {})


def load_sectors():
    out = []
    for r in read_csv(os.path.join(CONFIG, "sectors.csv")):
        kws = [k.strip().lower() for k in (r.get("keywords") or "").split(";") if k.strip()]
        out.append({"sector": r["sector"], "slug": r["slug"], "keywords": kws})
    return out


def load_buyers():
    p = os.path.join(CONFIG, "buyers.csv")
    return read_csv(p) if os.path.exists(p) else []


class Universe:
    """Tracked ticker universe (ticker -> sectors) joined to SEC CIKs."""

    def __init__(self, fetch_tickers=True):
        self.sectors_by_ticker = {}
        for r in read_csv(os.path.join(CONFIG, "ticker-sectors.csv")):
            t = (r.get("ticker") or "").strip().upper()
            if t:
                self.sectors_by_ticker[t] = [s.strip() for s in (r.get("sectors") or "").split(";") if s.strip()]
        self.tickers_meta = sec_company_tickers() if fetch_tickers else {}
        self.cik_by_ticker = {}
        for cik, m in self.tickers_meta.items():
            self.cik_by_ticker.setdefault(m["ticker"].upper(), cik)
        self.ciks = {}
        for t, secs in self.sectors_by_ticker.items():
            c = self.cik_by_ticker.get(t)
            if c:
                self.ciks[c] = {"ticker": t, "sectors": secs}
        for b in load_buyers():
            t = (b.get("ticker") or "").strip().upper()
            if t and t in self.cik_by_ticker:
                self.ciks.setdefault(self.cik_by_ticker[t], {"ticker": t, "sectors": []})
        names = {}
        for cik, info in self.ciks.items():
            nm = norm_name(self.tickers_meta.get(cik, {}).get("title", ""))
            if nm and len(nm) >= 3:
                names[nm] = info["ticker"]
        self.name_to_ticker = names

    def lookup_cik(self, cik):
        return self.ciks.get(int(cik))

    def match_names_in_text(self, text):
        found, up = set(), text.upper()
        for m in re.finditer(r"\b(?:NASDAQ|NYSE|NYSE AMERICAN|AMEX|NYSEARCA|CBOE|TSX|TSXV)\s*:\s*([A-Z]{1,5})\b", up):
            if m.group(1) in self.sectors_by_ticker:
                found.add(m.group(1))
        for m in re.finditer(r"\$([A-Z]{1,5})\b", text):
            if m.group(1) in self.sectors_by_ticker:
                found.add(m.group(1))
        n = " " + norm_name(text) + " "
        for nm, t in self.name_to_ticker.items():
            if len(nm) >= 5 and (" " + nm + " ") in n:
                found.add(t)
        # short company names (ASML, AMAT, IREN ...) only when written as an upper-case word in the text
        for nm, t in self.name_to_ticker.items():
            if 3 <= len(nm) <= 4 and nm.isalpha() and re.search(r"(?<![A-Za-z0-9])" + nm + r"(?![A-Za-z0-9])", text):
                found.add(t)
        return sorted(found)


class BuyerMatcher:
    def __init__(self, buyers=None):
        self.items = []
        for b in (buyers if buyers is not None else load_buyers()):
            names = [b["name"]] + [a for a in (b.get("aliases") or "").split(";") if a.strip()]
            pats = []
            for n in names:
                n = n.strip()
                if len(n) < 3:
                    continue
                short = len(n) <= 4
                if short and not (n.isupper() or any(c.isdigit() for c in n)):
                    continue
                pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(n) + r"(?![A-Za-z0-9])", 0 if short else re.I))
            if pats:
                self.items.append((b["buyer_id"], pats))

    def match(self, text):
        return [bid for bid, pats in self.items if any(p.search(text) for p in pats)]


# ----------------------------------------------------------------------------- the signal engine


def _junk(s):
    """Encoded blobs, tables and legal fragments: too few letters, or very long unbroken tokens."""
    if len(s) < 25:
        return False
    letters = sum(1 for c in s if c.isalpha() or c == " ")
    if letters / len(s) < 0.72:
        return True
    return bool(re.search(r"\S{45,}", s))


def _phrase_rx(p):
    return re.compile(r"(?<![a-z0-9])" + re.escape(p.lower()) + r"(?![a-z0-9])")


class SignalEngine:
    """Vocabulary groups + combination rules.

    A combination fires when every class it needs is present in ONE sentence (or in a sentence
    plus the sentence that follows it). Classes are phrase groups from keywords.json, the
    pseudo-classes @quantity (money or capacity unit), @money, @capacity_units, @percent,
    @percent_or_money, and @buyer (a registry buyer named in the window).
    """

    def __init__(self, kw=None, buyer_matcher=None, sectors=None):
        self.kw = kw or load_keywords()
        self.groups = {g: [(p.lower(), _phrase_rx(p)) for p in ph] for g, ph in self.kw["groups"].items()}
        q = self.kw["quantity_regex"]
        self.q = {k: re.compile(v, re.I) for k, v in q.items()}
        self.combos = self.kw["combos"]
        self.veto = [p.lower() for p in self.kw.get("sentence_veto", [])]
        self.noise = [p.lower() for p in self.kw.get("exclude_noise", [])]
        self.l4 = [(p, _phrase_rx(p)) for p in self.kw.get("layer4_events", {}).get("phrases", [])]
        self.buyers = buyer_matcher or BuyerMatcher()
        self.sectors = sectors or load_sectors()
        self._sector_rx = [(s["sector"], [_phrase_rx(k) for k in s["keywords"]]) for s in self.sectors]
        self.th = self.kw.get("thresholds", {})

    # ---- basic tagging
    def is_noise(self, text):
        low = text.lower()
        return any(p in low for p in self.noise)

    def sector_hits(self, text):
        low = text.lower()
        res = []
        for name, rxs in self._sector_rx:
            n = sum(1 for rx in rxs if rx.search(low))
            if n:
                res.append((name, n))
        res.sort(key=lambda x: -x[1])
        return res

    def groups_hit(self, text):
        low = text.lower()
        res = {}
        for g, pats in self.groups.items():
            hit = [p for p, rx in pats if rx.search(low)]
            if hit:
                res[g] = hit
        return res

    def layer4_hits(self, text):
        low = text.lower()
        return [p for p, rx in self.l4 if rx.search(low)]

    # ---- quantity helpers
    def quantities(self, text):
        return {k: [m.group(0).strip() for m in rx.finditer(text)][:5] for k, rx in self.q.items()}

    def _has(self, cls, low, text, buyers_in):
        if isinstance(cls, (list, tuple)):  # any-of
            return any(self._has(c, low, text, buyers_in) for c in cls)
        if cls == "@quantity":
            return bool(self.q["money"].search(text) or self.q["capacity_units"].search(text))
        if cls == "@money":
            return bool(self.q["money"].search(text))
        if cls == "@capacity_units":
            return bool(self.q["capacity_units"].search(low))
        if cls == "@percent":
            return bool(self.q["percent"].search(text))
        if cls == "@percent_or_money":
            return bool(self.q["percent"].search(text) or self.q["money"].search(text))
        if cls == "@buyer":
            return bool(buyers_in)
        return any(rx.search(low) for _p, rx in self.groups.get(cls, []))

    # ---- the combination engine
    def _trim(self, sent, need_groups, limit):
        """Cut a long sentence down to the part around the first matched vocabulary phrase."""
        if len(sent) <= limit:
            return sent
        low = sent.lower()
        pos = None
        for g in need_groups:
            for gg in (g if isinstance(g, (list, tuple)) else [g]):
                for _p, rx in self.groups.get(gg, []):
                    m = rx.search(low)
                    if m and (pos is None or m.start() < pos):
                        pos = m.start()
        start = max(0, (pos or 0) - 100)
        return ("..." if start else "") + sent[start:start + limit]

    def analyse(self, text, strict=False):
        """Return dict: combos (ids), families (ordered by score), score, evidence sentences,
        binding_status, quantities, buyers, group_hits.

        score = weight of the strongest combination + 1 for each further distinct combination
        (max +3), so a long filing cannot pile up points just by being long. Sentences that carry
        boilerplate or financing wording (config sentence_veto) are skipped.
        """
        text = text or ""
        sents = split_sentences(text) or [text]
        lim = self.th.get("excerpt_max_chars", 320)
        fired = {}
        for i, s in enumerate(sents):
            low_s = s.lower()
            if any(v in low_s for v in self.veto):
                continue
            if _junk(s):
                continue
            nxt = sents[i + 1] if i + 1 < len(sents) else ""
            if nxt and any(v in nxt.lower() for v in self.veto):
                nxt = ""
            for scope_is_sentence, tx in ((True, s), (False, s + " " + nxt)):
                if not scope_is_sentence and not nxt:
                    continue
                low = tx.lower()
                buyers_in = self.buyers.match(tx)
                for c in self.combos:
                    cur = fired.get(c["id"])
                    if cur and (cur["weight"] >= c["weight"] or not scope_is_sentence):
                        continue
                    if all(self._has(cl, low, tx, buyers_in) for cl in c["need"]):
                        w = c["weight"] if scope_is_sentence else max(1, c["weight"] - 1)
                        if not cur or cur["weight"] < w:
                            fired[c["id"]] = {"weight": w, "family": c["family"], "sentence": self._trim(s, c["need"], lim)}
        if strict:
            # periodic reports: only firm figures count (weight >= 4), plus backlog and supplier disclosures
            fired = {cid: v for cid, v in fired.items()
                     if v["weight"] >= 4 or v["family"] in ("order_backlog", "supplier_disclosure")}
        ws = sorted(((v["weight"], cid) for cid, v in fired.items()), reverse=True)
        fam_best, fam_n = {}, {}
        for cid, v in fired.items():
            f = v["family"]
            fam_best[f] = max(fam_best.get(f, 0), v["weight"])
            fam_n[f] = fam_n.get(f, 0) + 1
        fam_score = {f: fam_best[f] + min(3, fam_n[f] - 1) for f in fam_best}
        families = [f for f, _ in sorted(fam_score.items(), key=lambda x: -x[1])]
        score = (ws[0][0] + min(3, len(ws) - 1)) if ws else 0
        sents_out = []
        for cid, v in sorted(fired.items(), key=lambda x: -x[1]["weight"]):
            if v["sentence"] not in sents_out:
                sents_out.append(v["sentence"])
        # binding status is judged on the sentences that fired, not on the whole document
        basis = " ".join(sents_out).lower() if sents_out else text.lower()
        executed = bool(sents_out) and any(rx.search(basis) for _p, rx in self.groups.get("executed_markers", []))
        nonbind = bool(sents_out) and any(rx.search(basis) for _p, rx in self.groups.get("nonbinding_markers", []))
        if executed and not nonbind:
            binding = "executed"
        elif nonbind and not executed:
            binding = "non-binding-or-prospective"
        elif executed and nonbind:
            binding = "mixed-check"
        else:
            binding = "unstated"
        joined = " ".join(sents_out)
        return {
            "combos": sorted(fired),
            "families": families,
            "family_scores": fam_score,
            "score": score,
            "evidence": sents_out[:3],
            "binding_status": binding,
            "quantities": self.quantities(joined if joined else text),
            "buyers": self.buyers.match(joined if joined else text),
            "group_hits": self.groups_hit(joined) if joined else {},
        }


# ----------------------------------------------------------------------------- misc


def money_mentions(text, n=5):
    rx = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:billion|million|thousand|bn|mm|m|b|k)?\b", re.I)
    return [m.group(0).strip() for m in rx.finditer(text)][:n]


def parse_money(s):
    """'$1.5 billion' -> 1.5e9 ; None if unparsable."""
    m = re.match(r"\$\s?([\d,]+(?:\.\d+)?)\s?(billion|million|thousand|bn|mm|m|b|k)?", s or "", re.I)
    if not m:
        return None
    v = float(m.group(1).replace(",", ""))
    u = (m.group(2) or "").lower()
    mult = {"billion": 1e9, "bn": 1e9, "b": 1e9, "million": 1e6, "mm": 1e6, "m": 1e6, "thousand": 1e3, "k": 1e3}.get(u, 1)
    return v * mult
