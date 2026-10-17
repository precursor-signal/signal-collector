"""Test the Layer 2 vocabulary (config/keywords.json) against the old published Layer 2 research.

Positive text = the 'Deal signal' section of each report (where the talent fact was written up).
Negative text = sections that carry no talent fact (price and analyst targets, market capitalization,
entry price plan, funding / valuation, competitive landscape).

Reports, per combination and per phrase: pages covered in each set and a precision proxy
(positive pages / (positive pages + negative pages)). Writes analysis/vocabulary-test.json.

Usage: python tools/test_vocabulary.py --corpus <corpus.json>
corpus.json = list of {date, tier, file, secs:{section name: text}} built from the old research pages.
"""
import argparse
import json
import os
import sys
import collections

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
import l2lib  # noqa: E402

NEG_SECTIONS = ("price and analyst targets", "market capitalization", "entry price plan", "funding / valuation", "competitive landscape")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--corpus", required=True)
    ap.add_argument("--out", default=os.path.join(l2lib.ROOT, "analysis", "vocabulary-test.json"))
    a = ap.parse_args()
    docs = json.load(open(a.corpus, encoding="utf-8"))
    eng = l2lib.SignalEngine()
    pos_docs = [d for d in docs if d["secs"].get("deal signal")]
    neg_docs = [d for d in docs if any(d["secs"].get(k) for k in NEG_SECTIONS)]

    combo_pos, combo_neg = collections.Counter(), collections.Counter()
    phrase_pos, phrase_neg = collections.Counter(), collections.Counter()
    score_pos, score_neg = [], []
    fam_pos = collections.Counter()
    first_hit = collections.Counter()
    for d in pos_docs:
        r = eng.analyse(d["secs"]["deal signal"])
        score_pos.append(r["score"])
        for c in r["combos"]:
            combo_pos[c] += 1
        for f in r["families"]:
            fam_pos[f] += 1
        low = d["secs"]["deal signal"].lower()
        for g, pats in eng.groups.items():
            for p, rx in pats:
                if rx.search(low):
                    phrase_pos[(g, p)] += 1
    for d in neg_docs:
        t = "\n".join(d["secs"].get(k, "") for k in NEG_SECTIONS)
        r = eng.analyse(t)
        score_neg.append(r["score"])
        for c in r["combos"]:
            combo_neg[c] += 1
        low = t.lower()
        for g, pats in eng.groups.items():
            for p, rx in pats:
                if rx.search(low):
                    phrase_neg[(g, p)] += 1

    keep = eng.th.get("keep_min_score", 3)
    n_pos, n_neg = len(pos_docs), len(neg_docs)
    res = {
        "positive_pages": n_pos, "negative_pages": n_neg,
        "positive_pages_with_score_ge_keep": sum(1 for s in score_pos if s >= keep),
        "negative_pages_with_score_ge_keep": sum(1 for s in score_neg if s >= keep),
        "combos": {}, "phrases": [],
    }
    for c in eng.combos:
        cid = c["id"]
        p, n = combo_pos[cid], combo_neg[cid]
        res["combos"][cid] = {"weight": c["weight"], "pos_pages": p, "neg_pages": n,
                              "pos_rate": round(p / n_pos, 3), "neg_rate": round(n / max(1, n_neg), 3),
                              "precision": round(p / (p + n), 3) if p + n else None}
    for (g, p), v in phrase_pos.items():
        n = phrase_neg.get((g, p), 0)
        res["phrases"].append({"group": g, "phrase": p, "pos_pages": v, "neg_pages": n,
                               "pos_rate": round(v / n_pos, 3), "precision": round(v / (v + n), 3)})
    res["phrases"].sort(key=lambda r: -r["pos_pages"])
    json.dump(res, open(a.out, "w", encoding="utf-8"), indent=1)

    print(f"positive pages {n_pos}, scored >= {keep}: {res['positive_pages_with_score_ge_keep']} "
          f"({100 * res['positive_pages_with_score_ge_keep'] / n_pos:.0f}%)")
    print(f"negative pages {n_neg}, scored >= {keep}: {res['negative_pages_with_score_ge_keep']} "
          f"({100 * res['negative_pages_with_score_ge_keep'] / max(1, n_neg):.0f}%)")
    print("\ncombination                 weight  pos_pages  neg_pages  precision")
    for cid, v in sorted(res["combos"].items(), key=lambda x: -x[1]["pos_pages"]):
        print(f"{cid:27s} {v['weight']:>5}  {v['pos_pages']:>9}  {v['neg_pages']:>9}  {v['precision']}")
    print("\nfamilies on positive pages:", dict(fam_pos.most_common()))


if __name__ == "__main__":
    main()
