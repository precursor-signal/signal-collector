"""Shared helpers for the Layer 1 signal collectors (standard library + openpyxl only).

Layer 1 = physics & engineering reality signal DISCOVERY - the earliest layer (24-36 months
ahead of the market). Before any financial signal exists, the underlying physics and engineering
bottleneck tells you what's coming: a cluster of papers on the same hardware problem, engineers
independently complaining about the same bottleneck, a conference track, a patent filed by a
small entrant in an area incumbents dominate, GitHub/Hugging Face adoption velocity, a government
R&D grant, a manufacturing-enablement partnership that is not yet a disclosed commercial order.
Nothing here scores, tiers or judges a stock. Funding rounds, IPOs and fund positions are Layer 4;
contracts, capacity and procurement are Layer 3; a named person hired/appointed/departing is
Layer 2 (Layer 1 only keeps a `spinout_signal` row when the SAME event also carries a research
signal, and cross-references Layer 2's own row rather than re-describing the person move).
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

SEC_UA = "PrecursorSignal Layer1 collector thilini.hettiarachchi@precursorsignal.com"
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
_HOST_GAP = {
    "export.arxiv.org": 3.0,         # arXiv asks for >=3s between calls from one client
    "hn.algolia.com": 0.3,
    "api.github.com": 1.0,           # unauthenticated: ~60 req/hr, so space these out
    "huggingface.co": 0.4,
    "patents.google.com": 1.2,
    "efts.sec.gov": 0.3,
    "www.sec.gov": 0.17,
}


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


def fetch_json(url, **kw):
    st, txt, err = fetch_text(url, **kw)
    if txt is None:
        return st, None, err
    try:
        return st, json.loads(txt), None
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


def item_key(title, link):
    host = urllib.parse.urlparse(link or "").netloc.lower()
    t = re.sub(r"[^a-z0-9]+", " ", (title or "").lower()).strip()
    return hashlib.sha1((t + "|" + host).encode()).hexdigest()[:16]


# ----------------------------------------------------------------------------- feeds (RSS/Atom)


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
        return None
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


# ----------------------------------------------------------------------------- arXiv


def arxiv_search(query, categories=None, max_results=50, start=0, sort_by="submittedDate"):
    """export.arxiv.org Atom API. `query` is a free-text phrase; `categories` ORs cat: terms in.
    Returns (status, list_of_entries, error). Each entry: id, title, summary, authors, categories,
    published, updated, link."""
    parts = []
    if query:
        parts.append(f"abs:{query}" if " " not in query else f'abs:"{query}"')
    if categories:
        cat_q = " OR ".join(f"cat:{c}" for c in categories)
        parts.append(f"({cat_q})")
    search_query = " AND ".join(parts) if parts else (categories[0] if categories else "")
    url = ("https://export.arxiv.org/api/query?" + urllib.parse.urlencode({
        "search_query": search_query, "start": start, "max_results": max_results,
        "sortBy": sort_by, "sortOrder": "descending"}))
    st, txt, err = fetch_text(url, retries=1)
    if txt is None:
        return st, None, err
    try:
        ns = {"a": "http://www.w3.org/2005/Atom", "arxiv": "http://arxiv.org/schemas/atom"}
        root = ET.fromstring(txt)
        out = []
        for e in root.findall("a:entry", ns):
            out.append({
                "id": (e.findtext("a:id", default="", namespaces=ns) or "").strip(),
                "title": _WS.sub(" ", (e.findtext("a:title", default="", namespaces=ns) or "").strip()),
                "summary": _WS.sub(" ", (e.findtext("a:summary", default="", namespaces=ns) or "").strip())[:1500],
                "published": (e.findtext("a:published", default="", namespaces=ns) or "").strip(),
                "updated": (e.findtext("a:updated", default="", namespaces=ns) or "").strip(),
                "authors": [a.findtext("a:name", default="", namespaces=ns) for a in e.findall("a:author", ns)],
                "categories": [c.attrib.get("term", "") for c in e.findall("a:category", ns)],
                "link": next((l.attrib.get("href") for l in e.findall("a:link", ns)
                              if l.attrib.get("type") == "text/html"), ""),
            })
        return st, out, None
    except Exception as ex:
        return st, None, f"parse: {ex}"


# ----------------------------------------------------------------------------- HackerNews (Algolia)


def hn_search(query, min_points=0, tags="story", hits_per_page=50, numeric_filters=None):
    """hn.algolia.com search_by_date. Returns (status, list_of_hits, error)."""
    params = {"query": query, "tags": tags, "hitsPerPage": hits_per_page}
    nf = [f"points>={min_points}"] if min_points else []
    if numeric_filters:
        nf += numeric_filters
    if nf:
        params["numericFilters"] = ",".join(nf)
    url = "https://hn.algolia.com/api/v1/search_by_date?" + urllib.parse.urlencode(params)
    st, j, err = fetch_json(url, retries=1)
    if j is None:
        return st, None, err
    return st, j.get("hits", []), None


# ----------------------------------------------------------------------------- GitHub


def github_search_repos(query, sort="stars", order="desc", per_page=20, token=None):
    """api.github.com/search/repositories. Unauthenticated: ~60 req/hr - use sparingly/cached."""
    url = "https://api.github.com/search/repositories?" + urllib.parse.urlencode(
        {"q": query, "sort": sort, "order": order, "per_page": per_page})
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    st, j, err = fetch_json(url, headers=headers, retries=0)
    if j is None:
        return st, None, err
    return st, j.get("items", []), None


# ----------------------------------------------------------------------------- Hugging Face


def hf_search_models(query, sort="downloads", limit=20):
    url = "https://huggingface.co/api/models?" + urllib.parse.urlencode(
        {"search": query, "sort": sort, "direction": "-1", "limit": limit})
    st, j, err = fetch_json(url, retries=0)
    if j is None:
        return st, None, err
    return st, j, None


# ----------------------------------------------------------------------------- Google Patents
# patents.google.com/xhr/query is the (undocumented) JSON endpoint the Google Patents search page
# itself calls. Tested live 2026-10-17: returns structured results (title, snippet, assignee,
# filing_date, publication_date, publication_number) for a free-text query, sortable by filing
# date (`sort=new`), with NO API key and no login. This contradicts the old framework's
# "JS-rendered shell only" finding - re-tested here, not assumed. Treat as "reachable but
# undocumented": it may change without notice, same caveat as SEC's efts.sec.gov full-text search.


def google_patents_search(query, sort="new", num_page=0):
    q = query if "sort%3D" in query else query
    url_q = query + ("&sort=" + sort if sort else "")
    url = "https://patents.google.com/xhr/query?" + urllib.parse.urlencode({"url": url_q, "exp": ""})
    if num_page:
        url += f"&page={num_page}"
    st, j, err = fetch_json(url, retries=1)
    if j is None:
        return st, None, err
    try:
        cluster = j["results"]["cluster"][0]["result"]
        out = [c["patent"] for c in cluster if "patent" in c]
        return st, out, None
    except Exception as ex:
        return st, None, f"parse: {ex}"


def uspto_odp_search(query, api_key=None, limit=10):
    """USPTO Open Data Portal patent-search API (api.uspto.gov). Needs a free registered key
    (env var USPTO_ODP_API_KEY, registered 2026-10-17) - primary patent source in run_patents()
    when the key is set; google_patents_search() is the fallback when it is not. Returns
    (status, json-dict-with-patentFileWrapperDataBag, err); no sort param - the API 500s on
    "sort" (tested 2026-10-17), so this relies on the API's own default ordering, which already
    skews toward recent filings."""
    api_key = api_key or os.environ.get("USPTO_ODP_API_KEY")
    if not api_key:
        return 0, None, "no USPTO_ODP_API_KEY set"
    url = "https://api.uspto.gov/api/v1/patent/applications/search?" + urllib.parse.urlencode(
        {"q": query, "limit": limit})
    st, j, err = fetch_json(url, headers={"X-API-KEY": api_key}, retries=1)
    return st, j, err


# ----------------------------------------------------------------------------- SEC (secondary use:
# gov-R&D-grant / patent-portfolio mentions inside an 8-K/10-K exhibit; full-text search only)


def sec_fts_url(query, start, end, forms="8-K,6-K,10-Q,10-K,20-F,40-F"):
    return ("https://efts.sec.gov/LATEST/search-index?q=" + urllib.parse.quote(query) +
            "&dateRange=custom&startdt=" + start + "&enddt=" + end + "&forms=" + forms)


# ----------------------------------------------------------------------------- registry


def load_keywords():
    return load_json(os.path.join(CONFIG, "keywords.json"), {})


def load_sectors():
    out = []
    for r in read_csv(os.path.join(CONFIG, "sectors.csv")):
        kws = [k.strip().lower() for k in (r.get("keywords") or "").split(";") if k.strip()]
        out.append({"sector": r["sector"], "slug": r["slug"], "keywords": kws})
    return out


def load_arxiv_categories():
    p = os.path.join(CONFIG, "arxiv-categories.csv")
    return read_csv(p) if os.path.exists(p) else []


def load_conference_calendar():
    p = os.path.join(CONFIG, "conference-calendar.csv")
    return read_csv(p) if os.path.exists(p) else []


class PrivateMatcher:
    """Private watchlist names (company + aliases) found in a text."""

    def __init__(self):
        self.items = []
        p = os.path.join(CONFIG, "private-watchlist.csv")
        for r in (read_csv(p) if os.path.exists(p) else []):
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


class Universe:
    """Tracked ticker universe (ticker -> sectors), for sector/ticker tagging of feed and API text.
    Lighter than Layer 2/3/4's Universe: Layer 1 does not open SEC filings by CIK as its primary
    source, so this skips the SEC company-ticker fetch unless asked."""

    def __init__(self, fetch_tickers=False):
        self.sectors_by_ticker = {}
        p = os.path.join(CONFIG, "ticker-sectors.csv")
        for r in (read_csv(p) if os.path.exists(p) else []):
            t = (r.get("ticker") or "").strip().upper()
            if t:
                self.sectors_by_ticker[t] = [s.strip() for s in (r.get("sectors") or "").split(";") if s.strip()]
        self.tickers_meta = {}
        if fetch_tickers:
            self._load_sec_tickers()
        self.name_to_ticker = {}

    def _load_sec_tickers(self):
        path = os.path.join(CACHE, "company_tickers_exchange.json")
        ensure_dir(CACHE)
        fresh = os.path.exists(path) and (time.time() - os.path.getmtime(path) < 20 * 3600)
        if not fresh:
            st, txt, err = fetch_text("https://www.sec.gov/files/company_tickers_exchange.json")
            if txt:
                with open(path, "w", encoding="utf-8") as f:
                    f.write(txt)
        d = load_json(path, {}) or {}
        fields = d.get("fields") or []
        names = {}
        for row in d.get("data") or []:
            r = dict(zip(fields, row))
            nm = norm_name(r.get("name", ""))
            if nm and len(nm) >= 3:
                names[nm] = r.get("ticker", "")
        self.name_to_ticker = names

    def match_tickers_in_text(self, text):
        found, up = set(), (text or "").upper()
        for m in re.finditer(r"\b(?:NASDAQ|NYSE|NYSE AMERICAN|AMEX|NYSEARCA|CBOE|TSX|TSXV)\s*:\s*([A-Z]{1,5})\b", up):
            if m.group(1) in self.sectors_by_ticker:
                found.add(m.group(1))
        for m in re.finditer(r"\$([A-Z]{1,5})\b", text or ""):
            if m.group(1) in self.sectors_by_ticker:
                found.add(m.group(1))
        n = " " + norm_name(text or "") + " "
        for nm, t in self.name_to_ticker.items():
            if len(nm) >= 5 and t in self.sectors_by_ticker and (" " + nm + " ") in n:
                found.add(t)
        return sorted(found)


# ----------------------------------------------------------------------------- the signal engine
# (generic: driven entirely by config/keywords.json; identical mechanism to Layer 2/3's engine)


def _junk(s):
    if len(s) < 25:
        return False
    letters = sum(1 for c in s if c.isalpha() or c == " ")
    if letters / len(s) < 0.72:
        return True
    return bool(re.search(r"\S{45,}", s))


def _phrase_rx(p):
    if p.startswith("regex:"):
        return re.compile(p[6:], re.I)
    return re.compile(r"(?<![a-z0-9])" + re.escape(p.lower()) + r"(?![a-z0-9])")


class SignalEngine:
    """Vocabulary groups + combination rules for PHYSICS/ENGINEERING signals (Layer 1).

    A combination fires when every class it needs is present in ONE sentence (or a sentence plus
    the one that follows it). Classes are phrase groups from keywords.json and the pseudo-classes
    @person (a named researcher/engineer), @stars, @downloads, @points, @money (quantity_regex).
    """

    def __init__(self, kw=None, sectors=None):
        self.kw = kw or load_keywords()
        self.groups = {g: [(p.lower(), _phrase_rx(p)) for p in ph] for g, ph in self.kw["groups"].items()}
        self.q = {k: re.compile(v, re.I) for k, v in self.kw.get("quantity_regex", {}).items()}
        self.combos = self.kw["combos"]
        self.veto = [p.lower() for p in self.kw.get("sentence_veto", [])]
        self.noise = [p.lower() for p in self.kw.get("exclude_noise", [])]
        self.person_rx = re.compile(self.kw.get("person_regex", r"(?!)"))
        self.not_names = set(self.kw.get("person_not_names", []))
        self.sectors = sectors or load_sectors()
        self._sector_rx = [(s["sector"], [_phrase_rx(k) for k in s["keywords"]]) for s in self.sectors]
        self.th = self.kw.get("thresholds", {})

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

    def persons(self, text):
        cache = self.__dict__.setdefault("_pcache", {})
        if text in cache:
            return cache[text]
        if len(cache) > 20000:
            cache.clear()
        out = cache[text] = []
        for m in self.person_rx.finditer(text or ""):
            name = m.group(0).strip()
            toks = re.findall(r"[A-Za-z][A-Za-z'\-]*", name)
            toks = [t for t in toks if t not in ("Dr", "Mr", "Ms", "Mrs", "Prof")]
            if len(toks) < 2 or any(t in self.not_names for t in toks):
                continue
            if name not in out:
                out.append(name)
        return out

    def quantities(self, text):
        return {k: [m.group(0).strip() for m in rx.finditer(text or "")][:5] for k, rx in self.q.items()}

    def _has(self, cls, low, text, ctx):
        if isinstance(cls, (list, tuple)):
            return any(self._has(c, low, text, ctx) for c in cls)
        if cls == "@person":
            return bool(self.persons(text))
        if cls.startswith("@"):
            rx = self.q.get(cls[1:])
            return bool(rx and rx.search(text))
        return any(rx.search(low) for _p, rx in self.groups.get(cls, []))

    def _trim(self, sent, need_groups, limit):
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

    def analyse(self, text, ctx=None):
        ctx = ctx or {}
        text = text or ""
        sents = split_sentences(text) or [text]
        lim = self.th.get("excerpt_max_chars", 360)
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
                for c in self.combos:
                    cur = fired.get(c["id"])
                    if cur and (cur["weight"] >= c["weight"] or not scope_is_sentence):
                        continue
                    if all(self._has(cl, low, tx, ctx) for cl in c["need"]) and not any(
                            self._has(cl, low, tx, ctx) for cl in c.get("not", [])):
                        w = c["weight"] if scope_is_sentence else max(1, c["weight"] - 1)
                        if not cur or cur["weight"] < w:
                            fired[c["id"]] = {"weight": w, "family": c["family"], "sentence": self._trim(s, c["need"], lim)}
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
        joined = " ".join(sents_out)
        return {
            "combos": sorted(fired),
            "families": families,
            "family_scores": fam_score,
            "score": score,
            "evidence": sents_out[:3],
            "persons": self.persons(joined) if joined else [],
            "quantities": self.quantities(joined if joined else text),
            "group_hits": self.groups_hit(joined) if joined else {},
        }


def money_mentions(text, n=5):
    rx = re.compile(r"\$\s?\d[\d,]*(?:\.\d+)?\s?(?:billion|million|thousand|bn|mm|m|b|k)?\b", re.I)
    return [m.group(0).strip() for m in rx.finditer(text or "")][:n]
