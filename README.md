# signal-collector

Data collectors for Precursor Signal.

- `layer4-signal-collector/` - Layer 4 daily and weekly collectors (scripts and config).
- `layer5-signal-collector/` - Layer 5 market movers and sector headlines. Run by the GitHub Actions
  workflows in `.github/workflows/`, started by the claude.ai routine "Layer 5 - collector kickoff"
  pushing `layer5-signal-collector/triggers/*.txt`. Results are committed back to `main`.
