#!/usr/bin/env python3
"""Test every source Layer 1's collector wants to use, live, and write analysis/source-reachability.json.

Run this before trusting any scheduled collector run, and again after the first scheduled run
from GitHub Actions - L2/L3's own experience is that reachability differs between a workstation
and an Actions runner (Cloudflare challenges hit some trade-press feeds only from GitHub's IP
ranges). Nothing here is assumed working; everything is actually requested.

Usage
  python tools/test_sources.py                 # test everything
  python tools/test_sources.py --only apis      # apis | feeds | patents | agent_covered
"""
import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import l1lib as L  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

API_TESTS = [
    ("arXiv API", lambda: L.arxiv_search("silicon photonics", categories=["cs.AR"], max_results=3)),
    ("HackerNews Algolia API", lambda: L.hn_search("bandwidth bottleneck", min_points=0, hits_per_page=3)),
    ("GitHub search API", lambda: L.github_search_repos("silicon photonics", per_page=3)),
    ("Hugging Face API", lambda: L.hf_search_models("photonic", limit=3)),
    ("Google Patents XHR (undocumented)", lambda: L.google_patents_search("q=silicon+photonics", sort="new")),
    ("SEC full-text search (efts.sec.gov)", lambda: L.fetch_json(
        L.sec_fts_url('"research grant" "DARPA"', "2026-10-01", "2026-10-17"))),
]

USPTO_ODP_TEST = ("USPTO Open Data Portal API (needs free USPTO_ODP_API_KEY)",
                   lambda: L.uspto_odp_search("silicon photonics"))

FEED_TESTS = [
    ("SemiAnalysis newsletter", "https://newsletter.semianalysis.com/archive", "html"),
    ("Datacenter Dynamics RSS", "https://www.datacenterdynamics.com/en/rss/", "rss"),
    ("IEEE Spectrum RSS", "https://spectrum.ieee.org/feeds/feed.rss", "rss"),
    ("SpaceNews RSS", "https://spacenews.com/feed/", "rss"),
    ("SatelliteToday RSS", "https://www.satellitetoday.com/feed/", "rss"),
    ("The Quantum Insider RSS", "https://thequantuminsider.com/feed/", "rss"),
    ("Blocks and Files RSS", "https://blocksandfiles.com/feed/", "rss"),
    ("EE Times RSS", "https://www.eetimes.com/feed/", "rss"),
    ("HPCwire RSS", "https://www.hpcwire.com/feed/", "rss"),
    ("Digitimes RSS", "https://www.digitimes.com/rss/daily.xml", "rss"),
    ("Tom's Hardware RSS", "https://www.tomshardware.com/feeds/all", "rss"),
    ("Google News RSS (sector query)", "https://news.google.com/rss/search?q=silicon+photonics+bottleneck", "rss"),
    ("Semiconductor Today (no working RSS path found)", "https://www.semiconductor-today.com/rss/news_rss.xml", "rss"),
    ("optics.org (no working RSS path found)", "https://optics.org/rss/news", "rss"),
    ("TrendForce (no working RSS path found)", "https://www.trendforce.com/rss", "rss"),
]

AGENT_COVERED_TESTS = [
    ("LinkedIn job search (login-walled for scripted clients)", "https://www.linkedin.com/jobs/search?keywords=silicon+photonics+engineer"),
    ("G2 search (403 to scripted clients)", "https://www.g2.com/search?query=test"),
    ("SimilarWeb (202/JS-rendered)", "https://www.similarweb.com/website/openai.com/"),
    ("BuiltWith (202/JS-rendered)", "https://builtwith.com/openai.com"),
    ("Reddit JSON API (403 to scripted clients)", "https://www.reddit.com/r/hardware/.json"),
]


def test_api(name, fn):
    t0 = time.time()
    try:
        st, items, err = fn()
    except Exception as e:
        return {"source": name, "ok": False, "error": f"{type(e).__name__}: {e}", "elapsed_s": round(time.time() - t0, 2)}
    ok = items is not None and (not isinstance(items, list) or len(items) >= 0)
    return {"source": name, "ok": bool(ok and st in (200, 0, None) or (ok and st)), "http": st,
            "sample_count": (len(items) if isinstance(items, list) else (1 if items else 0)),
            "error": err, "elapsed_s": round(time.time() - t0, 2)}


def test_feed(name, url, kind):
    t0 = time.time()
    st, body, ct, err = L.fetch(url, retries=1, timeout=20)
    if body is None:
        return {"source": name, "url": url, "ok": False, "http": st, "error": err, "elapsed_s": round(time.time() - t0, 2)}
    if kind == "rss":
        items = L.parse_feed(body)
        ok = items is not None and len(items) > 0
        return {"source": name, "url": url, "ok": ok, "http": st, "items_parsed": (len(items) if items else 0),
                "error": None if ok else "not parseable as RSS/Atom (interstitial or wrong path)",
                "elapsed_s": round(time.time() - t0, 2)}
    return {"source": name, "url": url, "ok": st == 200, "http": st, "bytes": len(body), "elapsed_s": round(time.time() - t0, 2)}


def test_agent_covered(name, url):
    t0 = time.time()
    st, body, ct, err = L.fetch(url, retries=0, timeout=15)
    note = ""
    if body:
        low = body.decode("utf-8", "ignore").lower()
        if "sign in" in low and "linkedin" in url.lower():
            note = "200 but login-walled (sign-in prompt detected) - treat as agent-covered, not scriptable"
    return {"source": name, "url": url, "http": st, "error": err, "note": note or "expected blocked/agent-covered",
            "elapsed_s": round(time.time() - t0, 2)}


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="apis,feeds,patents,agent_covered")
    a = ap.parse_args()
    which = set(a.only.split(","))
    out = {"tested_et": L.now_et().isoformat(timespec="seconds"), "apis": [], "feeds": [], "uspto_odp": None, "agent_covered": []}

    if "apis" in which or "patents" in which:
        for name, fn in API_TESTS:
            print("testing:", name)
            out["apis"].append(test_api(name, fn))
        print("testing:", USPTO_ODP_TEST[0])
        out["uspto_odp"] = test_api(*USPTO_ODP_TEST)

    if "feeds" in which:
        for name, url, kind in FEED_TESTS:
            print("testing:", name)
            out["feeds"].append(test_feed(name, url, kind))

    if "agent_covered" in which:
        for name, url in AGENT_COVERED_TESTS:
            print("testing:", name)
            out["agent_covered"].append(test_agent_covered(name, url))

    n_ok_api = sum(1 for r in out["apis"] if r.get("ok"))
    n_ok_feed = sum(1 for r in out["feeds"] if r.get("ok"))
    out["summary"] = {
        "apis_ok": f"{n_ok_api}/{len(out['apis'])}",
        "feeds_ok": f"{n_ok_feed}/{len(out['feeds'])}",
        "uspto_odp_needs_key": out["uspto_odp"] and not out["uspto_odp"].get("ok"),
    }
    path = os.path.join(ROOT, "analysis", "source-reachability.json")
    L.save_json(path, out)
    print(json.dumps(out["summary"], indent=1))
    print("written:", path)


if __name__ == "__main__":
    main()
