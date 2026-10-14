#!/usr/bin/env python3
"""Regenerate config/feeds.csv from entities.csv + sectors.csv (+ the fixed feed list below).

Run after editing the entity registry or sector keywords:  python tools/build_feeds.py
Google News queries are kept <= 180 characters (a longer query makes Google ignore `when:`).
"""
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.dirname(HERE))
import l4lib as L  # noqa: E402

MAXQ = 180

# (feed_id, name, category, url, enabled, notes)
STATIC = [
    ("prn-all", "PR Newswire - all releases", "wire", "https://www.prnewswire.com/rss/news-releases-list.rss", "yes", "tested 2026-10-14"),
    ("prn-technology", "PR Newswire - technology", "wire", "https://www.prnewswire.com/rss/technology-latest-news/technology-latest-news-list.rss", "yes", "tested"),
    ("prn-energy", "PR Newswire - energy", "wire", "https://www.prnewswire.com/rss/energy-latest-news/energy-latest-news-list.rss", "yes", "tested"),
    ("prn-finserv", "PR Newswire - financial services", "wire", "https://www.prnewswire.com/rss/financial-services-latest-news/financial-services-latest-news-list.rss", "yes", "tested"),
    ("prn-acquisitions", "PR Newswire - acquisitions, mergers", "wire", "https://www.prnewswire.com/rss/financial-services-latest-news/acquisitions-mergers-and-takeovers-list.rss", "yes", "tested"),
    ("gnw-public", "GlobeNewswire - public companies", "wire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies", "no", "403 from scripted client 2026-10-14; agents cover manually"),
    ("bw-all", "Business Wire", "wire", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWQ==", "no", "feed id returned empty channel; needs valid ids; agents cover manually"),
    ("nvidia-newsroom", "NVIDIA newsroom", "newsroom", "https://nvidianews.nvidia.com/releases.xml", "yes", "tested"),
    ("techcrunch", "TechCrunch", "trade-press", "https://techcrunch.com/feed/", "yes", "tested"),
    ("techcrunch-venture", "TechCrunch - venture", "trade-press", "https://techcrunch.com/category/venture/feed/", "yes", "tested"),
    ("spacenews", "SpaceNews", "trade-press", "https://spacenews.com/feed/", "yes", "tested"),
    ("dcd", "Data Center Dynamics", "trade-press", "https://www.datacenterdynamics.com/en/rss/", "yes", "tested"),
    ("hpcwire", "HPCwire", "trade-press", "https://www.hpcwire.com/feed/", "yes", "tested"),
    ("siliconangle", "SiliconANGLE", "trade-press", "https://siliconangle.com/feed/", "yes", "tested"),
    ("wnn", "World Nuclear News", "trade-press", "https://world-nuclear-news.org/rss", "yes", "tested"),
    ("utilitydive", "Utility Dive", "trade-press", "https://www.utilitydive.com/feeds/news/", "yes", "tested"),
    ("breakingdefense", "Breaking Defense", "trade-press", "https://breakingdefense.com/feed/", "yes", "tested"),
    ("quantum-insider", "The Quantum Insider", "trade-press", "https://thequantuminsider.com/feed/", "no", "Cloudflare challenge 2026-10-14; agents cover manually"),
    ("govconwire", "GovConWire", "trade-press", "https://www.govconwire.com/feed/", "no", "Cloudflare challenge 2026-10-14; agents cover manually"),
    ("cnbc-top", "CNBC top news", "news", "https://www.cnbc.com/id/100003114/device/rss/rss.html", "yes", "tested"),
    ("cnbc-tech", "CNBC technology", "news", "https://www.cnbc.com/id/19854910/device/rss/rss.html", "yes", "tested"),
    ("techmeme", "Techmeme", "news", "https://www.techmeme.com/feed.xml", "yes", "tested"),
    ("sec-press", "SEC press releases", "regulator", "https://www.sec.gov/news/pressreleases.rss", "yes", "tested"),
    ("war-gov", "Department of War - releases", "gov", "https://www.war.gov/DesktopModules/ArticleCS/RSS.ashx?ContentType=9&Site=945&max=10", "yes", "tested"),
    ("commerce-gov", "Department of Commerce - press", "gov", "https://www.commerce.gov/news/press-releases/rss.xml", "no", "Cloudflare challenge 2026-10-14; agents cover manually"),
]


def gnews(q):
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote(q, safe="") + "&hl=en-US&gl=US&ceid=US:en"


def group_names(names, tail, window="when:1d"):
    """Greedy-pack quoted names into queries that stay under MAXQ characters."""
    groups, cur = [], []
    for n in names:
        trial = cur + [n]
        q = "(" + " OR ".join(f'"{x}"' for x in trial) + ") " + tail + " " + window
        if len(q) > MAXQ and cur:
            groups.append(cur)
            cur = [n]
        else:
            cur = trial
    if cur:
        groups.append(cur)
    return [("(" + " OR ".join(f'"{x}"' for x in g) + ") " + tail + " " + window) for g in groups]


def main():
    ents = L.load_entities()
    sectors = L.load_sectors()
    rows = []
    for fid, name, cat, url, en, note in STATIC:
        rows.append({"feed_id": fid, "name": name, "category": cat, "type": "rss", "url": url,
                     "enabled": en, "notes": note, "query": ""})

    inv = [e["name"] for e in ents if e["type"] in ("fund", "vc", "sovereign") and e["status"] not in ("deregistered",)
           and e["tier"] in ("tier1", "tier2", "history", "candidate")]
    # short, distinctive search names
    alias = {"Coatue Management": "Coatue", "Altimeter Capital Management": "Altimeter Capital",
             "Tiger Global Management": "Tiger Global", "Light Street Capital Management": "Light Street Capital",
             "Whale Rock Capital Management": "Whale Rock", "Lone Pine Capital": "Lone Pine Capital",
             "Dragoneer Investment Group": "Dragoneer", "Pershing Square Capital Management": "Pershing Square",
             "Third Point LLC": "Third Point", "Viking Global Investors": "Viking Global",
             "Elliott Investment Management": "Elliott Management", "ARK Investment Management": "ARK Invest",
             "Andreessen Horowitz": "a16z", "Sequoia Capital": "Sequoia Capital", "Founders Fund": "Founders Fund",
             "Bessemer Venture Partners": "Bessemer Venture Partners", "Lightspeed Venture Partners": "Lightspeed",
             "Qatar Investment Authority": "Qatar Investment Authority", "Mubadala / MGX": "Mubadala",
             "SoftBank Group": "SoftBank", "Public Investment Fund (Saudi)": "Public Investment Fund"}
    names = [alias.get(n, n) for n in inv if "Situational" not in n]
    names.append("Situational Awareness")
    q_inv = group_names(names, '(led OR invests OR "invested in" OR stake OR "backed by")')
    for i, q in enumerate(q_inv, 1):
        rows.append({"feed_id": f"gn-investors-{i:02d}", "name": f"Investor activity group {i}", "category": "news-search",
                     "type": "gnews", "url": gnews(q), "enabled": "yes", "notes": "entity-driven", "query": q})

    corp_tail = '("strategic investment" OR "equity stake" OR "invests in" OR warrant)'
    q = f'(NVIDIA OR NVentures) {corp_tail} when:1d'
    rows.append({"feed_id": "gn-corp-nvidia", "name": "NVIDIA strategic investments", "category": "news-search",
                 "type": "gnews", "url": gnews(q), "enabled": "yes", "notes": "stream 5", "query": q})
    q = f'(Microsoft OR Alphabet OR Amazon OR Intel OR Qualcomm OR Cisco OR Samsung) {corp_tail} when:1d'
    rows.append({"feed_id": "gn-corp-others", "name": "Corporate strategic investments", "category": "news-search",
                 "type": "gnews", "url": gnews(q), "enabled": "yes", "notes": "stream 5", "query": q})

    gov = [
        ('("equity stake" OR "equity investment") ("Department of Commerce" OR "CHIPS Act") when:1d', "gn-gov-commerce", "Government equity: Commerce / CHIPS"),
        ('("equity stake" OR "letter of intent") ("Department of War" OR "Department of Defense" OR "Office of Strategic Capital") when:1d', "gn-gov-war", "Government equity: War / Defense"),
        ('("equity stake" OR "loan" OR "award") ("Department of Energy") (nuclear OR uranium OR "critical minerals") when:1d', "gn-gov-doe", "Government: Energy awards"),
    ]
    for q, fid, name in gov:
        rows.append({"feed_id": fid, "name": name, "category": "news-search", "type": "gnews", "url": gnews(q),
                     "enabled": "yes", "notes": "government stakes", "query": q})

    rows.append({"feed_id": "gn-filings", "name": "13F / 13D / 13G news", "category": "news-search", "type": "gnews",
                 "url": gnews('("13F" OR "13D" OR "13G") ("new position" OR stake OR "disclosed") when:1d'),
                 "enabled": "yes", "notes": "fund filing coverage", "query": '("13F" OR "13D" OR "13G") ("new position" OR stake OR "disclosed") when:1d'})
    rows.append({"feed_id": "gn-politicians", "name": "Politician trade coverage", "category": "news-search", "type": "gnews",
                 "url": gnews('(Pelosi OR Khanna OR Cisneros OR Gottheimer OR Mullin) (stock OR bought OR sold OR disclosed) when:1d'),
                 "enabled": "yes", "notes": "STOCK Act coverage",
                 "query": '(Pelosi OR Khanna OR Cisneros OR Gottheimer OR Mullin) (stock OR bought OR sold OR disclosed) when:1d'})

    for s in sectors:
        kws = s["keywords"][:5]
        q = "(" + " OR ".join(f'"{k}"' if " " in k else k for k in kws) + ') (raises OR "Series A" OR "Series B" OR "Series C" OR funding OR "led by") when:1d'
        while len(q) > MAXQ and len(kws) > 2:
            kws = kws[:-1]
            q = "(" + " OR ".join(f'"{k}"' if " " in k else k for k in kws) + ') (raises OR "Series A" OR "Series B" OR "Series C" OR funding OR "led by") when:1d'
        rows.append({"feed_id": f"gn-rounds-{s['slug']}", "name": f"Private rounds - {s['sector']}", "category": "news-search",
                     "type": "gnews", "url": gnews(q), "enabled": "yes", "notes": "sector-driven private rounds", "query": q})

    L.write_csv(os.path.join(L.CONFIG, "feeds.csv"), rows,
                ["feed_id", "name", "category", "type", "url", "enabled", "notes", "query"])
    over = [r["feed_id"] for r in rows if r["query"] and len(r["query"]) > MAXQ]
    print(f"wrote {len(rows)} feeds ({sum(1 for r in rows if r['type']=='gnews')} Google News queries); over-length: {over}")


if __name__ == "__main__":
    main()
