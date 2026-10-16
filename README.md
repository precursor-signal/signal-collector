# signal-collector

Data collectors for Precursor Signal.

- `layer4-signal-collector/` - Layer 4 daily and weekly collectors (scripts and config).
- `layer3-signal-collector/` - Layer 3 (supply chain and procurement) daily and weekly collectors
  (scripts and config). Started by the GitHub Actions workflows `layer3-daily.yml` and
  `layer3-weekly.yml`, kicked off by claude.ai routines pushing `layer3-signal-collector/triggers/*.txt`.
- `layer5-signal-collector/` - Layer 5 market movers and sector headlines. Run by the GitHub Actions
  workflows in `.github/workflows/`, started by the claude.ai routine "Layer 5 - collector kickoff"
  pushing `layer5-signal-collector/triggers/*.txt`. Results are committed back to `main`.
