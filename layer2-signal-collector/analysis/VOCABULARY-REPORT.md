# Layer 2 vocabulary: how it was built and tested (2026-10-17)

## Source of the words

- 1,351 published Layer 2 reports of the old process (11 weekly runs, 2026-07-25 to 2026-09-30); 1,125 of them have a *Deal signal* section (2.9 million characters) where the talent fact was written up.
- Counts are in this folder: `word-counts-top100-words.csv`, `word-counts-top100-phrases.csv`, `word-counts-pedigree-orgs.csv` (pages that use each word, total occurrences, and pages of non-signal sections for comparison).
- Added to the counts: the old Layer 2 methodology terms (senior engineer departures, academics on boards, executives from Nvidia/TSMC/ASML/AMD/Broadcom, niche hiring surges, who a company lost), the job-keyword table (cooling, photonics, memory, custom silicon, quantum, space, power) and generated phrases for the 26 sectors (`tools/build_feeds.py`).

## Test 1: the old research text

Positive text = Deal signal sections (1125 pages). Negative text = price, market capitalization, entry price, funding and competitive sections (1118 pages), which carry no talent fact.

- Combinations score >= 3 (the keep line) on **937 of 1125** Deal signal sections (83%) and on **116 of 1118** non-signal sections (10%).

| Combination | Weight | Deal-signal pages | Non-signal pages | Precision |
|---|---|---|---|---|
| exec_appointment | 4 | 703 | 24 | 0.967 |
| pedigree_profile | 2 | 472 | 13 | 0.973 |
| pedigree_hire | 4 | 391 | 11 | 0.973 |
| board_appointment | 4 | 384 | 26 | 0.937 |
| senior_departure | 4 | 384 | 36 | 0.914 |
| technical_leader_hire | 3 | 217 | 3 | 0.986 |
| departure_to_found | 4 | 194 | 14 | 0.933 |
| founder_pedigree | 3 | 133 | 12 | 0.917 |
| newly_created_role | 3 | 90 | 1 | 0.989 |
| filing_officer_change | 4 | 85 | 0 | 1.0 |
| hiring_surge | 3 | 67 | 17 | 0.798 |
| academic_move | 3 | 65 | 4 | 0.942 |
| team_lift | 3 | 33 | 1 | 0.971 |
| facility_talent | 3 | 11 | 3 | 0.786 |
| headcount_growth | 3 | 1 | 2 | 0.333 |

Which phrases give the most talent signals (pages in Deal signal sections | pages in non-signal sections):

- **exec_titles**: president (519|10); ceo (511|95); vice president (379|3); cfo (186|36); chief financial officer (169|2); senior vice president (163|0); chairman (136|8); chief operating officer (136|6); executive vice president (118|0)
- **move_appoint**: named (437|219); joined (414|43); appointment (388|12); joining (218|77); hires (192|19); appointed (187|5); to join (187|10); hired (145|30); joins (82|0)
- **pedigree_markers**: spent (400|12); former (279|100); most recently (278|99); previously (267|22); regex:\b(?:over|more than|nearly|almost)\s+\d{1,2}\s+years (202|1); years at (192|2); veteran (160|6); brings more than (57|0); formerly (50|12)
- **board_terms**: board (517|127); the board (256|65); board of directors (184|5); board seat (104|7); audit committee (69|0); board member (65|9); independent director (49|1); advisory board (25|0); class i director (24|0)
- **move_depart**: departure (279|57); left (277|34); leaving (158|24); to leave (149|4); departed (36|3); resignation (33|1); resigned (26|0); stepped down (23|0); has left (17|0)
- **found_launch**: founder (251|68); co-founder (112|30); to found (91|0); founding team (77|26); co-founded (61|8); stealth (48|43); to start (39|4); co-found (33|1); founded by (20|35)
- **tech_titles**: engineering (395|69); engineer (129|4); architect (42|7); researcher (41|5); scientist (37|8); vice president of engineering (37|1); phd (36|2); fellow (33|20); ph.d (33|0)
- **hiring_terms**: talent (571|54); hire (558|33); hires (192|19); hiring (191|27); engineers (111|11); recruiting (62|8); headcount (50|28); researchers (40|14); workforce (27|12)
- **academic_terms**: phd (36|2); ph.d (33|0); professor (30|2); darpa (20|16); national laboratory (14|4); postdoctoral (9|0); research institute (8|1); nobel (2|1)
- **filing_person_terms**: effective immediately (34|0); item 5.02 (33|0); form 3 (28|0); form 4 (20|0); employment agreement (8|0); offer letter (5|0); commencing (1|0)

Phrases with a very low yield or a poor ratio were removed or narrowed after this test (`lands`, `comes from`, `to lead`, `arm`, `mit`, `ula`, `dr.`, `fellow`, `university`). Funding wording (`led by`, `series b`, `raises`, `valuation` ...) is used as an exclusion on the people combinations, because it showed up with investors who "joined" a round, which is Layer 4.

## Test 2: live data (run on 2026-10-17)

- **Feeds**: 106 of 106 enabled feeds read; 6,469 items captured; 19 kept as signals after the relevance and age gates. The kept items were real moves (for example XCENA hiring an ex-Qualcomm executive as CBO; the OpenAI CTO joining Amplitude's board; OpenAI naming a head of data centres and compute; Super Micro naming a CFO). Remaining noise: some headlines read as person names (headline title case) and a few non-business stories; the agents verify every kept item.
- **SEC**: 3 days of the daily index, 801 filings read, 107 carried Item 5.02, 53 kept; 8 Form 3 filings by officers or directors of tracked companies (one was the D-Wave director who is also in the old research). Tightening applied after the first look: the standard Item 5.02 heading and the cover page are removed before scoring; short organisation names that collide with common words were dropped.
- **Full-text search**: 10 of 10 queries worked; 2 hits opened.
- **Hiring**: 169 companies with a public Greenhouse / Lever / Ashby board (102 private, 67 public), 18,555 open roles, role families assigned (forward-deployed engineers: 336 roles; AI / ML engineer 530; AI / ML research 228; silicon / hardware 589; quantum 87; photonics 95).

## Known limits
- Person names in feed headlines are the weakest field; Form 3 and Item 5.02 filings give names reliably.
- The hiring script sees only companies on Greenhouse, Lever or Ashby (about 20% of the tracked list); everything else is read by agents.
- A vocabulary match is a lead, not a verdict: the agents check the primary source.
