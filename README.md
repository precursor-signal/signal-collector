# signal-collector

Data collectors for Precursor Signal.

- `layer1-signal-collector/` - Layer 1 (physics & engineering reality) daily and weekly
  collectors (scripts and config). arXiv/HackerNews/GitHub/Hugging Face are first-class direct-API
  collectors here (not generic feed items) since Layer 1's premise is catching a signal before
  any filing or press release exists. Patents are best-effort only (Google's patent search is
  bot-detection-gated at any real request rate - see that folder's README). Runs on Friday so
  the four weekly-cadence layers don't collide (L4=Tue, L3=Wed, L2=Thu, L1=Fri).
- `layer4-signal-collector/` - Layer 4 daily and weekly collectors (scripts and config).
- `layer3-signal-collector/` - Layer 3 (supply chain and procurement) daily and weekly collectors
  (scripts and config). Started by the GitHub Actions workflows `layer3-daily.yml` and
  `layer3-weekly.yml`, kicked off by claude.ai routines pushing `layer3-signal-collector/triggers/*.txt`.
- `layer5-signal-collector/` - Layer 5 market movers and sector headlines. Run by the GitHub Actions
  workflows in `.github/workflows/`, started by the claude.ai routine "Layer 5 - collector kickoff"
  pushing `layer5-signal-collector/triggers/*.txt`. Results are committed back to `main`.
