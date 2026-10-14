#!/usr/bin/env python3
"""Resolve Google News RSS article links (news.google.com/rss/articles/<id>) to the real publisher URL.

Older ids are plain base64 of the URL and decode offline. Newer ids (start "AU_yqL") need one page fetch (for a
signature and timestamp) plus one batchexecute call. Returns "" when it cannot be resolved; never raises.
Shared by news/layer5_market_signals.py and movers/layer5_market_movers.py.
"""
import base64, json, re, time, urllib.error, urllib.parse, urllib.request

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
      "Chrome/124.0 Safari/537.36")
_BATCH = "https://news.google.com/_/DotsSplashUi/data/batchexecute"


def article_id(link):
    m = re.search(r"news\.google\.com/(?:rss/)?(?:read|articles)/([^/?#]+)", link or "")
    return m.group(1) if m else ""


def _offline(aid):
    """Old-style ids carry the URL inside the base64 payload."""
    try:
        raw = base64.urlsafe_b64decode(aid + "=" * (-len(aid) % 4))
    except Exception:
        return ""
    if raw[:3] == b"\x08\x13\x22":
        raw = raw[3:]
    if raw[-3:] == b"\xd2\x01\x00":
        raw = raw[:-3]
    if not raw:
        return ""
    n = raw[0]
    if n >= 0x80:
        n = (n & 0x7F) | (raw[1] << 7)
        s = raw[2:2 + n]
    else:
        s = raw[1:1 + n]
    s = s.decode("utf-8", "ignore")
    return s if s.startswith("http") and not s.startswith("AU_yqL") else ""


def _get(url, timeout=25):
    req = urllib.request.Request(url, headers={"User-Agent": UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode("utf-8", "ignore")


def _online(aid, timeout=25):
    page = _get(f"https://news.google.com/rss/articles/{aid}?hl=en-US&gl=US&ceid=US:en", timeout)
    sg = re.search(r'data-n-a-sg="([^"]+)"', page)
    ts = re.search(r'data-n-a-ts="([^"]+)"', page)
    if not (sg and ts):
        return "", "no signature on page"
    inner = json.dumps(["garturlreq", [["X", "X", ["X", "X"], None, None, 1, 1, "US:en", None, 1, None, None, None,
                                        None, None, 0, 1], "X", "X", 1, [1, 1, 1], 1, 1, None, 0, 0, None, 0],
                        aid, int(ts.group(1)), sg.group(1)])
    body = "f.req=" + urllib.parse.quote(json.dumps([[["Fbv4je", inner, None, "generic"]]]))
    req = urllib.request.Request(_BATCH, data=body.encode(), headers={
        "User-Agent": UA, "Content-Type": "application/x-www-form-urlencoded;charset=UTF-8"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        txt = r.read().decode("utf-8", "ignore")
    try:
        parsed = json.loads(txt.split("\n\n", 1)[1])
        url = json.loads(parsed[0][2])[1]
    except Exception:
        return "", "unparsable response"
    return (url, "") if isinstance(url, str) and url.startswith("http") else ("", "no url in response")


def resolve(link, retries=2, timeout=25):
    """Returns (url, status): status 'offline', 'online', or an error text with url ''."""
    aid = article_id(link)
    if not aid:
        return "", "not a google news article link"
    u = _offline(aid)
    if u:
        return u, "offline"
    err = ""
    for k in range(retries + 1):
        try:
            u, err = _online(aid, timeout)
            if u:
                return u, "online"
            if err:
                break
        except urllib.error.HTTPError as e:
            err = f"http {e.code}"
            if e.code not in (429, 503):
                break
            time.sleep(4 * (k + 1))
        except Exception as e:
            err = str(e)[:60]
            time.sleep(2)
    return "", err or "failed"


if __name__ == "__main__":
    import sys
    for l in sys.argv[1:]:
        print(resolve(l))
