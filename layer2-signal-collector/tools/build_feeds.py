#!/usr/bin/env python3
"""Build config/feeds.csv for the Layer 2 daily collector (talent signals).

Rows come from three places:
  1. STATIC: direct RSS/Atom feeds (wires, trade press, people-move columns, company newsrooms).
     `tools/test_sources.py` tests each one and writes config/feed-overrides.json; a feed that
     failed the test is written with enabled=no and is covered by the agent routines (source gaps).
  2. SECTOR GOOGLE NEWS: for each of the 26 sectors, two Google News RSS searches built from the
     sector's own words plus Layer 2 vocabulary (people-move feed + hiring / departure feed).
     Query length is capped at 180 characters because Google silently ignores `when:1d` above ~200.
  3. CROSS-SECTOR GOOGLE NEWS: searches on the highest-yield phrase combinations (pedigree moves,
     departures to found, academics to boards, chief-scientist hires).

Run: python tools/build_feeds.py
"""
import csv
import json
import os
import sys
import urllib.parse

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)
import l2lib as L  # noqa: E402

STATIC = [
    # feed_id, name, category, url, notes
    ("prn-all", "PR Newswire - all releases", "wire", "https://www.prnewswire.com/rss/news-releases-list.rss", "Layer 3/4 tested: works (follows 301)"),
    ("prn-technology", "PR Newswire - technology", "wire", "https://www.prnewswire.com/rss/technology-latest-news/technology-latest-news-list.rss", ""),
    ("prn-personnel", "PR Newswire - personnel announcements", "wire", "https://www.prnewswire.com/rss/business-technology/personnel-announcements-latest-news/personnel-announcements-latest-news-list.rss", "people-move channel, tested by test_sources"),
    ("prn-energy", "PR Newswire - energy", "wire", "https://www.prnewswire.com/rss/energy-latest-news/energy-latest-news-list.rss", ""),
    ("prn-aerospace-defense", "PR Newswire - aerospace and defense", "wire", "https://www.prnewswire.com/rss/aerospace-defense-latest-news/aerospace-defense-latest-news-list.rss", ""),
    ("gnw-personnel", "GlobeNewswire - personnel announcements", "wire", "https://www.globenewswire.com/RssFeed/subjectcode/19-Personnel%20Announcements/feedTitle/GlobeNewswire%20-%20Personnel%20Announcements", "403 from scripted clients in Layer 3 test; retested"),
    ("gnw-public", "GlobeNewswire - public companies", "wire", "https://www.globenewswire.com/RssFeed/orgclass/1/feedTitle/GlobeNewswire%20-%20News%20about%20Public%20Companies", "403 from scripted clients in Layer 3 test; retested"),
    ("bw-all", "Business Wire - all", "wire", "https://feed.businesswire.com/rss/home/?rss=G1QFDERJXkJeEFpRWA==", "empty / 403 in Layer 3 test; retested"),
    ("accesswire", "ACCESS Newswire", "wire", "https://www.accessnewswire.com/newsroom/rss", "small-cap wire, retested"),

    ("techmeme", "Techmeme", "aggregator", "https://www.techmeme.com/feed.xml", "same-day executive departures cluster here"),
    ("geekwire-moves", "GeekWire - Tech Moves", "people-moves", "https://www.geekwire.com/tag/tech-moves/feed/", "named in the old Layer 2 run log as productive"),
    ("geekwire", "GeekWire", "trade", "https://www.geekwire.com/feed/", ""),
    ("datacenterknowledge", "Data Center Knowledge", "people-moves", "https://www.datacenterknowledge.com/rss.xml", "quarterly 'Key Data Center & Cloud Appointments' roundup"),
    ("dcd", "DatacenterDynamics", "trade", "https://www.datacenterdynamics.com/en/rss/", "Layer 3/4 tested: works"),
    ("datacenterfrontier", "Data Center Frontier", "trade", "https://www.datacenterfrontier.com/rss.xml", "404 in Layer 3 test; retested"),
    ("executivebiz", "ExecutiveBiz", "people-moves", "https://www.executivebiz.com/feed/", "defense / gov-con executive moves"),
    ("govconwire", "GovConWire", "people-moves", "https://www.govconwire.com/feed/", "403 in Layer 3 test; retested"),
    ("washingtontechnology", "Washington Technology", "people-moves", "https://www.washingtontechnology.com/rss", "retested"),
    ("techcrunch", "TechCrunch", "trade", "https://techcrunch.com/feed/", "Layer 3/4 tested: works"),
    ("siliconangle", "SiliconANGLE", "trade", "https://siliconangle.com/feed/", "works"),
    ("hpcwire", "HPCwire", "trade", "https://www.hpcwire.com/feed/", "works"),
    ("theregister", "The Register", "trade", "https://www.theregister.com/headlines.atom", "works"),
    ("semiengineering", "Semiconductor Engineering", "trade", "https://semiengineering.com/feed/", "has a Startup / people column"),
    ("eetimes", "EE Times", "trade", "https://www.eetimes.com/feed/", "works"),
    ("semiconductor-today", "Semiconductor Today", "trade", "https://www.semiconductor-today.com/rss/news.xml", "large feed"),
    ("tomshardware", "Tom's Hardware", "trade", "https://www.tomshardware.com/feeds/all", "large feed"),
    ("digitimes", "DigiTimes", "trade", "https://www.digitimes.com/rss/daily.xml", "works"),
    ("thelec", "The Elec", "trade", "https://www.thelec.net/rss/allArticle.xml", "Korean semiconductor press, retested"),
    ("kedglobal", "KED Global", "trade", "https://www.kedglobal.com/rss/allArticle.xml", "Korean chaebol / semiconductor, retested"),
    ("blocksandfiles", "Blocks and Files", "trade", "https://blocksandfiles.com/feed/", "works"),
    ("thequantuminsider", "The Quantum Insider", "trade", "https://thequantuminsider.com/feed/", "Layer 4 saw Cloudflare challenges from GitHub IPs; verify in Actions"),
    ("quantumcomputingreport", "Quantum Computing Report", "trade", "https://quantumcomputingreport.com/feed/", "has a 'Who's News' column"),
    ("quantumzeitgeist", "Quantum Zeitgeist", "trade", "https://quantumzeitgeist.com/feed/", ""),
    ("spacenews", "SpaceNews", "trade", "https://spacenews.com/feed/", "works"),
    ("payload", "Payload", "trade", "https://payloadspace.com/feed/", "works"),
    ("satellitetoday", "Via Satellite / Satellite Today", "trade", "https://www.satellitetoday.com/feed/", "works"),
    ("breakingdefense", "Breaking Defense", "trade", "https://breakingdefense.com/feed/", "works"),
    ("defensescoop", "DefenseScoop", "trade", "https://defensescoop.com/feed/", "works"),
    ("therobotreport", "The Robot Report", "trade", "https://www.therobotreport.com/feed/", "works"),
    ("securityweek", "SecurityWeek", "trade", "https://www.securityweek.com/feed/", "works"),
    ("darkreading", "Dark Reading", "trade", "https://www.darkreading.com/rss.xml", "retested"),
    ("utilitydive", "Utility Dive", "trade", "https://www.utilitydive.com/feeds/news/", "works"),
    ("world-nuclear-news", "World Nuclear News", "trade", "https://www.world-nuclear-news.org/rss", "works"),
    ("latitudemedia", "Latitude Media", "trade", "https://www.latitudemedia.com/feed", "works"),
    ("theminermag", "TheMinerMag", "trade", "https://theminermag.com/feed", "bitcoin miners, retested"),
    ("optics-org", "optics.org", "trade", "https://optics.org/rss/news", "photonics, retested"),
    ("mining-com", "Mining.com", "trade", "https://www.mining.com/feed/", "403 in Layer 3 test; retested"),
    ("cnbc-tech", "CNBC Technology", "news", "https://search.cnbc.com/rs/search/combinedcms/view.xml?partnerId=wrss01&id=19854910", "works"),
    ("axios", "Axios", "news", "https://api.axios.com/feed/", "retested"),
    ("wired", "Wired", "news", "https://www.wired.com/feed/rss", "broke the Discovery Loop / Jeff Dean story"),
    ("fortune-tech", "Fortune - tech", "news", "https://fortune.com/section/tech/feed/", "retested"),
    ("forbes-innovation", "Forbes - innovation", "news", "https://www.forbes.com/innovation/feed2/", "retested"),

    ("nvidia-news", "NVIDIA newsroom", "newsroom", "https://nvidianews.nvidia.com/releases.xml", "works"),
    ("microsoft-source", "Microsoft Source", "newsroom", "https://news.microsoft.com/source/feed/", "works"),
    ("google-blog", "Google blog", "newsroom", "https://blog.google/rss/", "works"),
    ("meta-news", "Meta newsroom", "newsroom", "https://about.fb.com/news/feed/", "works"),
    ("openai-news", "OpenAI news", "newsroom", "https://openai.com/news/rss.xml", "works"),
    ("intel-news", "Intel newsroom", "newsroom", "https://newsroom.intel.com/feed/", "works"),
    ("anthropic-news", "Anthropic news", "newsroom", "https://www.anthropic.com/rss.xml", "404 in Layer 3 test; retested"),
]

CROSS = [
    ("x-pedigree-hire", "pedigree hire: chief / VP joins from a big name", '("joins" OR "appoints" OR "names" OR "hires") ("former Nvidia" OR "former Google" OR "former Intel" OR "former TSMC" OR "former Broadcom" OR "ex-Nvidia")'),
    ("x-pedigree-ai-labs", "pedigree hire from AI labs", '("joins" OR "appoints" OR "hires" OR "leaves") ("OpenAI" OR "DeepMind" OR "Anthropic" OR "Meta Superintelligence") (researcher OR scientist OR "chief")'),
    ("x-leaves-to-found", "leaves a big company to found / launch", '("leaves" OR "departs" OR "steps down") ("to found" OR "to launch" OR "to start" OR "co-found") (Nvidia OR Google OR Meta OR OpenAI OR Intel OR Apple OR Microsoft)'),
    ("x-chief-scientist", "chief scientist / chief architect hires", '("chief scientist" OR "chief architect" OR "chief technology officer" OR "chief AI officer") ("appoints" OR "joins" OR "names") (semiconductor OR quantum OR "AI" OR space)'),
    ("x-professor-board", "academics joining boards / advisory boards", '(professor OR "Ph.D." OR "Nobel") ("joins board" OR "advisory board" OR "board of directors" OR "chief scientist") (quantum OR semiconductor OR photonics OR robotics)'),
    ("x-board-semis", "board seats for semiconductor / datacenter veterans", '("appointed to board" OR "joins board" OR "independent director") (semiconductor OR "data center" OR "AI infrastructure") (veteran OR former OR executive)'),
    ("x-team-lift", "team poaching / lift-outs", '(poaches OR poached OR "hires team" OR "lured away" OR "talent war") (engineers OR researchers) (AI OR chip OR quantum OR robotics)'),
    ("x-design-center", "new design centres and engineering hubs", '("design center" OR "engineering hub" OR "R&D center" OR "opens office") (hiring OR engineers OR "jobs") (semiconductor OR AI OR quantum OR robotics)'),
    ("x-stealth", "founders emerging from stealth with big-company pedigree", '("emerges from stealth" OR "out of stealth" OR "founded by former") (Nvidia OR Google OR OpenAI OR Intel OR Tesla OR SpaceX OR DeepMind)'),
    ("x-exec-departs", "senior technical departures at public companies", '("chief technology officer" OR "chief scientist" OR "head of engineering" OR "chief architect") ("steps down" OR "resigns" OR "departs" OR "leaves")'),
]

SECTOR_TEMPLATES = [
    ("moves", '("appoints" OR "names" OR "joins as" OR "hires" OR "appointed") ("chief" OR "vice president" OR "board" OR "head of") ({kw})'),
    ("departures-hiring", '("leaves" OR "departs" OR "steps down" OR "poached" OR "hiring" OR "design center" OR "new engineers") ({kw})'),
]


def gnews_url(query):
    q = query + " when:1d"
    return "https://news.google.com/rss/search?q=" + urllib.parse.quote_plus(q) + "&hl=en-US&gl=US&ceid=US:en"


def main():
    kw = L.load_keywords()
    cap = kw["feed_params"]["google_news_query_max_chars"]
    ov_path = os.path.join(L.CONFIG, "feed-overrides.json")
    overrides = json.load(open(ov_path, encoding="utf-8")) if os.path.exists(ov_path) else {}
    rows = []
    for fid, name, cat, url, note in STATIC:
        ov = overrides.get(fid, {})
        en = "no" if ov.get("enabled") == "no" else "yes"
        n = note
        if ov.get("note"):
            n = (note + "; " if note else "") + ov["note"]
        rows.append({"feed_id": fid, "name": name, "category": cat, "type": "rss", "url": url, "enabled": en, "notes": n, "query": "", "sector": ""})
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
