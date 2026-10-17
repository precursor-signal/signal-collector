#!/usr/bin/env python3
"""Regenerate config/feeds.csv: static trade-press RSS feeds (tested in tools/test_sources.py)
plus one Google News query per sector built from sectors.csv's own keywords. Re-run this after
editing sectors.csv or the static feed list below.

Usage: python tools/build_feeds.py
"""
import csv
import os
import sys
import urllib.parse

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import l1lib as L  # noqa: E402

# Tested live 2026-10-17 (tools/test_sources.py). "enabled: yes" = confirmed working RSS/HTML.
# "enabled: no" = confirmed NOT working from a scripted client right now; kept here (not
# deleted) so routine 03 knows exactly which sources it must cover, and so a future re-test can
# flip it back on without re-discovering the URL.
STATIC_FEEDS = [
    # feed_id, name, category, type, url, enabled, notes
    ("semianalysis", "SemiAnalysis newsletter", "trade", "html-archive", "https://newsletter.semianalysis.com/archive", "yes", "not an RSS feed - the archive page lists posts; parsed as HTML, not XML"),
    ("dcd", "Data Center Dynamics", "trade", "rss", "https://www.datacenterdynamics.com/en/rss/", "yes", ""),
    ("ieee_spectrum", "IEEE Spectrum", "trade", "rss", "https://spectrum.ieee.org/feeds/feed.rss", "yes", ""),
    ("spacenews", "SpaceNews", "trade", "rss", "https://spacenews.com/feed/", "yes", ""),
    ("satellitetoday", "Via Satellite / SatelliteToday", "trade", "rss", "https://www.satellitetoday.com/feed/", "yes", ""),
    ("quantum_insider", "The Quantum Insider", "trade", "rss", "https://thequantuminsider.com/feed/", "yes", ""),
    ("blocks_and_files", "Blocks and Files", "trade", "rss", "https://blocksandfiles.com/feed/", "yes", ""),
    ("eetimes", "EE Times", "trade", "rss", "https://www.eetimes.com/feed/", "yes", ""),
    ("hpcwire", "HPCwire", "trade", "rss", "https://www.hpcwire.com/feed/", "yes", ""),
    ("digitimes", "Digitimes", "trade", "rss", "https://www.digitimes.com/rss/daily.xml", "yes", ""),
    ("toms_hardware", "Tom's Hardware", "trade", "rss", "https://www.tomshardware.com/feeds/all", "yes", ""),
    ("semiconductor_today", "Semiconductor Today", "trade", "rss", "https://www.semiconductor-today.com/rss/news_rss.xml", "no", "404 - homepage (semiconductor-today.com) works but no working RSS path found 2026-10-17; agent-covered until a real feed URL is found"),
    ("optics_org", "optics.org", "trade", "rss", "https://optics.org/rss/news", "no", "404 - homepage works but no working RSS path found 2026-10-17; agent-covered"),
    ("trendforce", "TrendForce", "trade", "rss", "https://www.trendforce.com/rss", "no", "404 - homepage works but no working RSS path found 2026-10-17; agent-covered"),
    ("linkedin_jobs", "LinkedIn job search", "jobs", "html", "https://www.linkedin.com/jobs/search", "no", "200 but login-walled for scripted clients (sign-in prompt) 2026-10-17; agent-covered, same as Layer 2/3's own blocked feeds"),
]

GNEWS_TMPL = "https://news.google.com/rss/search?q={q}+when:3d&hl=en-US&gl=US&ceid=US:en"


def build():
    sectors = L.load_sectors()
    rows = []
    for fid, name, cat, typ, url, enabled, notes in STATIC_FEEDS:
        rows.append({"feed_id": fid, "name": name, "category": cat, "type": typ, "url": url, "enabled": enabled, "notes": notes})
    for s in sectors:
        kws = s["keywords"][:3]
        if not kws:
            continue
        q = urllib.parse.quote(" OR ".join(f'"{k}"' for k in kws) + " bottleneck OR breakthrough OR patent OR arxiv")
        rows.append({
            "feed_id": "gnews_" + s["slug"], "name": f"Google News - {s['sector']}", "category": "gnews",
            "type": "gnews", "url": GNEWS_TMPL.format(q=q), "enabled": "yes",
            "notes": "rotated sector query; Google News is a lead source only (open the linked article, don't trust the aggregator summary)",
        })
    path = os.path.join(L.CONFIG, "feeds.csv")
    L.write_csv(path, rows, fields=["feed_id", "name", "category", "type", "url", "enabled", "notes"])
    print(f"wrote {len(rows)} feeds ({sum(1 for r in rows if r['enabled']=='yes')} enabled) -> {path}")


if __name__ == "__main__":
    build()
