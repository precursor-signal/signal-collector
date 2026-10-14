"""Shared helpers for the Layer 4 signal collectors (standard library + openpyxl + pypdf only).

Layer 4 = capital-flow ("smart money") signal DISCOVERY. Nothing here scores, tiers or
judges a stock. It collects, parses, tags and stores facts with a source link.
"""
import csv
import datetime as dt
import email.utils
import hashlib
import html
import io
import json
import os
import random
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import xml.etree.ElementTree as ET
from html.parser import HTMLParser
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.abspath(__file__))
CONFIG = os.path.join(ROOT, "config")
CACHE = os.path.join(CONFIG, "cache")
DATA = os.path.join(ROOT, "data")
ET_TZ = ZoneInfo("America/New_York")

SEC_UA = "PrecursorSignal Layer4 collector thilini.hettiarachchi@precursorsignal.com"
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
    out = []
    d = end
    while len(out) < n:
        if d.weekday() < 5:
            out.append(d)
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def weekdays_between(start, end):
    d = start
    out = []
    while d <= end:
        if d.weekday() < 5:
            out.append(d)
        d += dt.timedelta(days=1)
    return out

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
        with open(path, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return default


def save_json(path, obj):
    ensure_dir(os.path.dirname(path))
    with open(path, "w", encoding="utf-8") as f:
        json.dump(obj, f, ensure_ascii=False, indent=1)

# ----------------------------------------------------------------------------- http

_last_hit = {}
_HOST_GAP = {"www.sec.gov": 0.17, "data.sec.gov": 0.17, "efts.sec.gov": 0.25}


def fetch(url, ua=None, timeout=40, headers=None, retries=2, data=None, gap=None, max_bytes=None):
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
            req = urllib.request.Request(url, headers=h, data=data)
            with urllib.request.urlopen(req, timeout=timeout) as r:
                body = r.read(max_bytes) if max_bytes else r.read()
                return r.status, body, r.headers.get("Content-Type", ""), None
        except urllib.error.HTTPError as e:
            last_err = f"HTTP {e.code}"
            if e.code in (404, 403, 401, 410):
                return e.code, None, "", last_err
            time.sleep(1.5 * (attempt + 1))
        except Exception as e:  # network / timeout
            last_err = f"{type(e).__name__}: {e}"
            time.sleep(1.5 * (attempt + 1))
    return 0, None, "", last_err


def fetch_text(url, **kw):
    st, body, ct, err = fetch(url, **kw)
    if body is None:
        return st, None, err
    return st, body.decode("utf-8", "ignore"), err

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


def first_sentences(text, patterns, max_n=3, max_len=260):
    """Sentences of `text` containing any phrase in `patterns` (lowercase substrings)."""
    out = []
    for s in re.split(r"(?<=[.!?])\s+", text):
        sl = s.lower()
        if any(p in sl for p in patterns):
            out.append(s.strip()[:max_len])
            if len(out) >= max_n:
                break
    return out


MONEY = re.compile(r"\$\s?[\d][\d,]*(?:\.\d+)?\s?(?:billion|million|thousand|bn|mm|m|b|k)?", re.I)


def money_mentions(text, n=5):
    return [m.group(0).strip() for m in MONEY.finditer(text)][:n]

# ----------------------------------------------------------------------------- xml

def _local(tag):
    return tag.rsplit("}", 1)[-1] if "}" in tag else tag


def parse_xml(text):
    try:
        return ET.fromstring(text)
    except Exception:
        try:
            return ET.fromstring(re.sub(r"<\?xml[^>]*\?>", "", text, count=1))
        except Exception:
            return None


def find_all(node, *names):
    names = {n.lower() for n in names}
    return [e for e in node.iter() if _local(e.tag).lower() in names]


def find_first(node, *names):
    r = find_all(node, *names)
    return r[0] if r else None


def text_of(node, *names, default=""):
    e = find_first(node, *names) if node is not None else None
    if e is None:
        return default
    t = "".join(e.itertext()).strip()
    return t if t else default


def child_text(node, name, default=""):
    for c in node:
        if _local(c.tag).lower() == name.lower():
            return "".join(c.itertext()).strip() or default
    return default


def xml_blocks(txt, root_tag):
    """All <root_tag>...</root_tag> blocks inside an EDGAR complete-submission .txt."""
    return re.findall(r"(<%s[\s>].*?</%s>)" % (root_tag, root_tag), txt, flags=re.S)


def to_float(s):
    try:
        return float(str(s).replace(",", "").replace("$", "").replace("%", "").strip())
    except Exception:
        return None

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
            rows.append({"form": m.group(1).strip(), "company": m.group(2).strip(),
                         "cik": int(m.group(3)), "date": m.group(4), "accession": m.group(7),
                         "path": m.group(5)})
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
    """Fetch only the first bytes of the complete submission (SGML header)."""
    url = sec_filing_urls(cik, accession)["txt"]
    st, body, ct, err = fetch(url, headers={"Range": f"bytes=0-{nbytes}"}, max_bytes=nbytes + 2000)
    if body is None:
        return None
    return body.decode("utf-8", "ignore")


def parse_sec_header(h):
    out = {}
    if not h:
        return out
    m = re.search(r"STANDARD INDUSTRIAL CLASSIFICATION:\s*(.*?)\s*\[(\d{4})\]", h)
    if m:
        out["sic"] = m.group(2)
        out["sic_desc"] = m.group(1).strip()
    m = re.search(r"STATE OF INCORPORATION:\s*([A-Z0-9]{2})", h)
    if m:
        out["state_inc"] = m.group(1)
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
    out = {}
    fields = d.get("fields") or []
    for row in d.get("data") or []:
        r = dict(zip(fields, row))
        try:
            cik = int(r["cik"])
        except Exception:
            continue
        # keep the first (primary) listing per CIK
        out.setdefault(cik, {"ticker": r.get("ticker", ""), "title": r.get("name", ""), "exchange": r.get("exchange", "")})
    return out

# ----------------------------------------------------------------------------- registry

def load_entities():
    p = os.path.join(CONFIG, "entities.csv")
    return read_csv(p) if os.path.exists(p) else []


def load_sectors():
    rows = read_csv(os.path.join(CONFIG, "sectors.csv"))
    out = []
    for r in rows:
        kws = [k.strip().lower() for k in r["keywords"].split(";") if k.strip()]
        out.append({"sector": r["sector"], "slug": r["slug"], "keywords": kws})
    return out


def load_keywords():
    return load_json(os.path.join(CONFIG, "keywords.json"), {})


class Universe:
    """Tracked ticker universe (ticker -> sectors) joined to SEC CIKs."""

    def __init__(self):
        self.sectors_by_ticker = {}
        p = os.path.join(CONFIG, "ticker-sectors.csv")
        for r in read_csv(p):
            t = (r.get("ticker") or "").strip().upper()
            if t:
                self.sectors_by_ticker[t] = [s.strip() for s in (r.get("sectors") or "").split(";") if s.strip()]
        self.tickers_meta = sec_company_tickers()
        self.cik_by_ticker = {}
        for cik, m in self.tickers_meta.items():
            self.cik_by_ticker.setdefault(m["ticker"].upper(), cik)
        self.ciks = {}
        for t, secs in self.sectors_by_ticker.items():
            c = self.cik_by_ticker.get(t)
            if c:
                self.ciks[c] = {"ticker": t, "sectors": secs}
        # registry corporates with a ticker
        for e in load_entities():
            t = (e.get("ticker") or "").strip().upper()
            if t and t in self.cik_by_ticker:
                c = self.cik_by_ticker[t]
                self.ciks.setdefault(c, {"ticker": t, "sectors": []})
        names = {}
        for cik, info in self.ciks.items():
            nm = norm_name(self.tickers_meta.get(cik, {}).get("title", ""))
            if nm and len(nm) >= 4:
                names[nm] = info["ticker"]
        self.name_to_ticker = names
        self._name_rx = None

    def lookup_cik(self, cik):
        return self.ciks.get(int(cik))

    def match_names_in_text(self, text):
        """Tickers of universe companies named in free text (name or exchange:ticker)."""
        found = set()
        up = text.upper()
        for m in re.finditer(r"\b(?:NASDAQ|NYSE|NYSE AMERICAN|AMEX|NYSEARCA|CBOE)\s*:\s*([A-Z]{1,5})\b", up):
            t = m.group(1)
            if t in self.sectors_by_ticker:
                found.add(t)
        for m in re.finditer(r"\$([A-Z]{1,5})\b", text):
            if m.group(1) in self.sectors_by_ticker:
                found.add(m.group(1))
        n = " " + norm_name(text) + " "
        for nm, t in self.name_to_ticker.items():
            if len(nm) >= 5 and (" " + nm + " ") in n:
                found.add(t)
        return sorted(found)


class EntityMatcher:
    def __init__(self, entities=None):
        self.items = []
        for e in (entities if entities is not None else load_entities()):
            if (e.get("status") or "").lower() in ("inactive",):
                pass
            names = [e["name"]] + [a for a in (e.get("aliases") or "").split(";") if a.strip()]
            pats = []
            for n in names:
                n = n.strip()
                n_clean = re.sub(r"^(Hon\.|Sen\.|Rep\.)\s+", "", n)
                if len(n_clean) < 3:
                    continue
                short = len(n_clean) <= 4
                if short and not (n_clean.isupper() or any(c.isdigit() for c in n_clean)):
                    continue
                flags = 0 if short else re.I
                pats.append(re.compile(r"(?<![A-Za-z0-9])" + re.escape(n_clean) + r"(?![A-Za-z0-9])", flags))
            if pats:
                self.items.append((e["entity_id"], e["type"], pats))

    def match(self, text):
        hits = []
        for eid, _typ, pats in self.items:
            if any(p.search(text) for p in pats):
                hits.append(eid)
        return hits


class KeywordTagger:
    """Phrase tagging with word boundaries (so 'mou' does not match 'amount')."""

    GROUPS = ("investment_verbs", "round_vocabulary", "government_vocabulary",
              "status_change_vocabulary", "commitment_vocabulary")

    def __init__(self, kw=None, sectors=None):
        self.kw = kw or load_keywords()
        self.sectors = sectors or load_sectors()
        self.groups = {}
        for g in self.GROUPS:
            self.groups[g] = [(p.lower(), re.compile(r"(?<![a-z0-9])" + re.escape(p.lower()) + r"(?![a-z0-9])"))
                              for p in self.kw.get(g, [])]
        self.noise = [p.lower() for p in self.kw.get("exclude_noise", [])]
        self._sector_rx = []
        for s in self.sectors:
            self._sector_rx.append((s["sector"], [re.compile(r"(?<![a-z0-9])" + re.escape(k) + r"(?![a-z0-9])") for k in s["keywords"]]))

    def groups_hit(self, text):
        low = text.lower()
        res = {}
        for g, pats in self.groups.items():
            hit = [p for p, rx in pats if rx.search(low)]
            if hit:
                res[g] = hit
        return res

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

# ----------------------------------------------------------------------------- feeds

def parse_feed(xml_bytes):
    """RSS 2.0 / Atom -> list of dict(title, link, published, summary, source)."""
    try:
        txt = xml_bytes.decode("utf-8", "ignore")
        txt = re.sub(r"^﻿", "", txt)
        root = ET.fromstring(txt)
    except Exception:
        return None
    items = []
    for it in root.iter():
        ln = _local(it.tag).lower()
        if ln not in ("item", "entry"):
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
                    d["summary"] = strip_html("".join(c.itertext()))[:600]
            elif cn == "source":
                d["source"] = strip_html("".join(c.itertext()))
        d["published_iso"] = to_iso(d["published"])
        items.append(d)
    return items


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


def item_key(title, link):
    host = urllib.parse.urlparse(link).netloc.lower()
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return hashlib.sha1((t + "|" + host).encode()).hexdigest()[:16]

# ----------------------------------------------------------------------------- html tables

class _TableParser(HTMLParser):
    def __init__(self):
        super().__init__()
        self.tables = []
        self._t = None
        self._row = None
        self._cell = None

    def handle_starttag(self, tag, attrs):
        if tag == "table":
            self._t = []
        elif tag == "tr" and self._t is not None:
            self._row = []
        elif tag in ("td", "th") and self._row is not None:
            self._cell = []

    def handle_endtag(self, tag):
        if tag in ("td", "th") and self._cell is not None and self._row is not None:
            self._row.append(_WS.sub(" ", "".join(self._cell)).strip())
            self._cell = None
        elif tag == "tr" and self._row is not None and self._t is not None:
            if self._row:
                self._t.append(self._row)
            self._row = None
        elif tag == "table" and self._t is not None:
            self.tables.append(self._t)
            self._t = None

    def handle_data(self, data):
        if self._cell is not None:
            self._cell.append(data)


def html_tables(text):
    p = _TableParser()
    try:
        p.feed(text)
    except Exception:
        pass
    return p.tables
