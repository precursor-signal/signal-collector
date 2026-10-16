#!/usr/bin/env python3
"""Build config/feeds.csv for the Layer 3 daily collector.

Rows come from three places:
  1. STATIC: direct RSS/Atom feeds (wires, trade press, buyer newsrooms, government, energy). Tested
     2026-10-15 from a Windows workstation; enabled=no rows are known to be blocked to scripted
     clients and are covered by the agent routines (source-gaps).
  2. SECTOR GOOGLE NEWS: for each of the 26 sectors, two Google News RSS searches built from the
     sector's own words plus Layer 3 vocabulary (deal feed + capacity/power/backlog feed). Query
     length is capped at 180 characters because Google silently ignores `when:1d` above ~200.
  3. CROSS-SECTOR GOOGLE NEWS: searches on the highest-yield phrase combinations.

Run: python tools/build_feeds.py
"""
import csv
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import l3lib as L  # noqa: E402

STATIC = [
    # feed_id, name, category, url, enabled, notes
    ("prn-all", "PR Newswire - all releases", "wire", "https://www.prnewswire.com/rss/news-releases-list.rss", "yes", "tested 2026-10-15 (follows 301)"),
    ("prn-technology", "PR Newswire - technology", "wire", "https://www.prnewswire.com/rss/technology-latest-news/technology-latest-news-list.rss", "yes", "tested 2026-10-15"),
    ("prn-energy", "PR Newswire - energy", "wire", "https://www.prnewswire.com/rss/energy-latest-news/energy-latest-news-list.rss", "yes", "tested 2026-10-15"),
    ("prn-aerospace-defense", "PR Newswire - aerospace and defense", "wire", "https://www.prnewswire.com/rss/aerospace-defense-latest-news/aerospace-defense-latest-news-list.rss", "yes", "tested 2026-10-15 if reachable"),
    ("prn-contracts", "PR Newswire - contracts", "wire", "https://www.prnewswire.com/rss/policy-public-interest-latest-news/policy-public-interest-latest-news-list.rss", "no", "not a contracts channel; left off"),
    ("gnw-public", "GlobeNewswire - public companies", "wire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies", "no", "403 from scripted client 2026-10-15; agents cover manually"),
    ("bw-all", "Business Wire - all", "wire", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWA==", "no", "empty channel / 403 2026-10-15; agents cover manually"),

    ("dcd", "DatacenterDynamics", "trade", "https://www.datacenterdynamics.com/en/rss/", "yes", "tested 2026-10-15"),
    ("hpcwire", "HPCwire", "trade", "https://www.hpcwire.com/feed/", "yes", "tested 2026-10-15"),
    ("siliconangle", "SiliconANGLE", "trade", "https://siliconangle.com/feed/", "yes", "tested 2026-10-15"),
    ("nextplatform", "The Next Platform", "trade", "https://www.nextplatform.com/feed/", "yes", "tested 2026-10-15"),
    ("eetimes", "EE Times", "trade", "https://www.eetimes.com/feed/", "yes", "tested 2026-10-15"),
    ("semiconductor-today", "Semiconductor Today", "trade", "https://www.semiconductor-today.com/rss/news.xml", "yes", "tested 2026-10-15 (large)"),
    ("tomshardware", "Tom's Hardware", "trade", "https://www.tomshardware.com/feeds/all", "yes", "tested 2026-10-15 (large)"),
    ("digitimes", "DigiTimes", "trade", "https://www.digitimes.com/rss/daily.xml", "yes", "tested 2026-10-15"),
    ("blocksandfiles", "Blocks and Files", "trade", "https://blocksandfiles.com/feed/", "yes", "tested 2026-10-15"),
    ("theregister", "The Register", "trade", "https://www.theregister.com/headlines.atom", "yes", "tested 2026-10-15"),
    ("techcrunch", "TechCrunch", "trade", "https://techcrunch.com/feed/", "yes", "tested 2026-10-15"),
    ("spacenews", "SpaceNews", "trade", "https://spacenews.com/feed/", "yes", "tested 2026-10-15"),
    ("satellitetoday", "Via Satellite / Satellite Today", "trade", "https://www.satellitetoday.com/feed/", "yes", "tested 2026-10-15"),
    ("payload", "Payload", "trade", "https://payloadspace.com/feed/", "yes", "tested 2026-10-15"),
    ("breakingdefense", "Breaking Defense", "trade", "https://breakingdefense.com/feed/", "yes", "tested 2026-10-15"),
    ("defensescoop", "DefenseScoop", "trade", "https://defensescoop.com/feed/", "yes", "tested 2026-10-15"),
    ("therobotreport", "The Robot Report", "trade", "https://www.therobotreport.com/feed/", "yes", "tested 2026-10-15"),
    ("securityweek", "SecurityWeek", "trade", "https://www.securityweek.com/feed/", "yes", "tested 2026-10-15"),
    ("utilitydive", "Utility Dive", "trade", "https://www.utilitydive.com/feeds/news/", "yes", "tested 2026-10-15"),
    ("powermag", "POWER Magazine", "trade", "https://www.powermag.com/feed/", "yes", "tested 2026-10-15"),
    ("world-nuclear-news", "World Nuclear News", "trade", "https://www.world-nuclear-news.org/rss", "yes", "tested 2026-10-15"),
    ("canarymedia", "Canary Media", "trade", "https://www.canarymedia.com/rss.rss", "yes", "tested 2026-10-15"),
    ("latitudemedia", "Latitude Media", "trade", "https://www.latitudemedia.com/feed", "yes", "tested 2026-10-15"),
    ("thequantuminsider", "The Quantum Insider", "trade", "https://thequantuminsider.com/feed/", "yes", "reachable 2026-10-15 from a workstation; Layer 4 saw a Cloudflare challenge from GitHub IPs - verify"),
    ("govconwire", "GovConWire", "trade", "https://www.govconwire.com/feed/", "no", "403 2026-10-15; agents cover manually"),
    ("mining-com", "Mining.com", "trade", "https://www.mining.com/feed/", "no", "403 2026-10-15; agents cover manually"),
    ("datacenterfrontier", "Data Center Frontier", "trade", "https://www.datacenterfrontier.com/rss.xml", "no", "404 2026-10-15; agents cover manually"),
    ("trendforce", "TrendForce", "trade", "https://www.trendforce.com/presscenter/news/rss", "no", "404 2026-10-15; agents cover manually"),

    ("nvidia-news", "NVIDIA newsroom", "buyer", "https://nvidianews.nvidia.com/releases.xml", "yes", "tested 2026-10-15"),
    ("nvidia-blog", "NVIDIA blog", "buyer", "https://blogs.nvidia.com/feed/", "yes", "tested 2026-10-15"),
    ("microsoft-source", "Microsoft Source", "buyer", "https://news.microsoft.com/source/feed/", "yes", "tested 2026-10-15"),
    ("aws-blog", "AWS blog", "buyer", "https://aws.amazon.com/blogs/aws/feed/", "yes", "tested 2026-10-15 (product-heavy, low yield)"),
    ("google-blog", "Google blog", "buyer", "https://blog.google/rss/", "yes", "tested 2026-10-15"),
    ("meta-news", "Meta newsroom", "buyer", "https://about.fb.com/news/feed/", "yes", "tested 2026-10-15"),
    ("openai-news", "OpenAI news", "buyer", "https://openai.com/news/rss.xml", "yes", "tested 2026-10-15"),
    ("intel-news", "Intel newsroom", "buyer", "https://newsroom.intel.com/feed/", "yes", "tested 2026-10-15 (large)"),
    ("oracle-news", "Oracle newsroom", "buyer", "https://www.oracle.com/news/rss/", "no", "403 2026-10-15; agents cover manually"),
    ("anthropic-news", "Anthropic news", "buyer", "https://www.anthropic.com/rss.xml", "no", "404 2026-10-15; agents cover manually"),
    ("tsmc-pr", "TSMC press", "buyer", "https://pr.tsmc.com/english/rss", "no", "404 2026-10-15; agents cover manually"),

    ("nasa-news", "NASA news releases", "gov", "https://www.nasa.gov/news-release/feed/", "yes", "tested 2026-10-15"),
    ("doe-news", "DOE news", "gov", "https://www.energy.gov/rss.xml", "yes", "tested 2026-10-15"),
    ("doe-nuclear", "DOE Office of Nuclear Energy", "gov", "https://www.energy.gov/ne/rss.xml", "yes", "tested 2026-10-15"),
    ("spaceforce-news", "Space Force news", "gov", "https://www.spaceforce.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=1&max=20", "yes", "tested 2026-10-15"),
    ("airforce-news", "Air Force news", "gov", "https://www.af.mil/DesktopModules/ArticleCS/RSS.ashx?ContentType=1&Site=1&max=20", "yes", "tested 2026-10-15"),
    ("eia-today", "EIA Today in Energy", "gov", "https://www.eia.gov/rss/todayinenergy.xml", "yes", "tested 2026-10-15"),
    ("dod-contracts", "DoD daily contract announcements", "gov", "https://www.war.gov/News/Contracts/", "no", "403 (Akamai) 2026-10-15; agents cover manually"),
    ("commerce-gov", "Commerce Department", "gov", "https://www.commerce.gov/rss.xml", "no", "403 2026-10-15; agents cover manually"),
    ("ferc-news", "FERC news", "gov", "https://www.ferc.gov/news-events/news", "no", "403 Cloudflare 2026-10-15; agents cover manually"),
    ("nrc-news", "NRC news", "gov", "https://www.nrc.gov/reading-rm/doc-collections/news/rss.xml", "no", "404/503 2026-10-15; agents cover manually"),
    ("darpa-news", "DARPA news", "gov", "https://www.darpa.mil/rss/news", "no", "404 2026-10-15; agents cover manually"),
]

CROSS = [
    ("x-take-or-pay", "take-or-pay / capacity reservation / prepayment", '("take-or-pay" OR "capacity reservation" OR "purchase commitment" OR prepayment) (supply OR capacity OR "data center")'),
    ("x-ppa-datacenter", "power purchase / offtake for data centers", '("power purchase agreement" OR offtake OR "interconnection") ("data center" OR hyperscaler) (MW OR GW OR megawatts)'),
    ("x-hyperscaler-supply", "hyperscaler / AI lab supply agreement", '(hyperscaler OR OpenAI OR Anthropic OR Microsoft OR Meta OR Oracle) ("supply agreement" OR "multi-year agreement" OR "capacity agreement")'),
    ("x-gov-awards", "government contract awards", '("awarded" OR "task order" OR IDIQ OR "other transaction") (Pentagon OR "Space Force" OR NASA OR DARPA OR "Department of Energy") ("$" OR million OR billion)'),
    ("x-fab-capacity", "fab / packaging / memory capacity", '("new fab" OR "capacity expansion" OR "wafer supply agreement" OR "HBM supply" OR CoWoS OR "advanced packaging") (agreement OR order OR expansion)'),
    ("x-backlog", "record backlog / design win", '("record backlog" OR "backlog of" OR "design win" OR "multi-year order" OR "book-to-bill") (semiconductor OR "data center" OR defense OR power)'),
    ("x-groundbreaking", "groundbreaking / construction start", '(groundbreaking OR "breaks ground" OR "construction begins" OR commissioning) ("data center" OR fab OR "manufacturing facility" OR "power plant" OR campus)'),
    ("x-lease", "data center lease / colocation", '("lease" OR "colocation" OR "capacity agreement") ("data center") (MW OR megawatts OR GW OR hyperscale)'),
]

SECTOR_TEMPLATES = [
    ("deal", '("signed" OR "awarded" OR "agreement" OR "contract" OR "purchase order" OR "supply agreement") ({kw})'),
    ("capacity", '(capacity OR expansion OR "MW" OR "GW" OR backlog OR groundbreaking OR "power purchase") ({kw})'),
]


def gnews_url(query):
    q = query + " when:1d"
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(q) + "&hl=en-US&gl=US&ceid=US:en"


def clip(q, n):
    return q if len(q) <= n else None


def main():
    kw = L.load_keywords()
    cap = kw["feed_params"]["google_news_query_max_chars"]
    rows = []
    for fid, name, cat, url, en, note in STATIC:
        rows.append({"feed_id": fid, "name": name, "category": cat, "type": "rss", "url": url, "enabled": en, "notes": note, "query": "", "sector": ""})
    for fid, name, q in CROSS:
        if len(q) > cap:
            print(f"WARN {fid}: query {len(q)} chars > {cap}; trimmed")
            q = q[:cap].rsplit(" ", 1)[0]
        rows.append({"feed_id": fid, "name": name, "category": "gnews", "type": "gnews", "url": gnews_url(q), "enabled": "yes",
                     "notes": "Google News RSS lead source (not primary evidence)", "query": q, "sector": ""})
    for s in L.load_sectors():
        kws = [k for k in s["keywords"] if len(k) >= 3][:6]
        for tag, tpl in SECTOR_TEMPLATES:
            use = list(kws)
            while True:
                kwq = " OR ".join('"%s"' % k for k in use)
                q = tpl.format(kw=kwq)
                if len(q) <= cap or len(use) <= 1:
                    break
                use = use[:-1]
            if len(q) > cap:
                print(f"WARN {s['slug']}-{tag}: still {len(q)} chars")
            rows.append({"feed_id": f"s-{s['slug']}-{tag}", "name": f"{s['sector']} - {tag}", "category": "gnews", "type": "gnews",
                         "url": gnews_url(q), "enabled": "yes", "notes": "Google News RSS lead source (not primary evidence)",
                         "query": q, "sector": s["sector"]})
    L.write_csv(os.path.join(L.CONFIG, "feeds.csv"), rows,
                ["feed_id", "name", "category", "type", "url", "enabled", "notes", "query", "sector"])
    n_en = sum(1 for r in rows if r["enabled"] == "yes")
    print(f"feeds.csv written: {len(rows)} rows, {n_en} enabled, {len(rows) - n_en} disabled (agent-covered)")


if __name__ == "__main__":
    main()
