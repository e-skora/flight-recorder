Seed and reset live behind the console entry point (D-010 "scripts/ holds seed and reset"):

  uv run flight-recorder reset
  uv run flight-recorder seed
  uv run flight-recorder serve

The seed submits fixtures/canonical/ through POST /api/v1/decision-events on the
in-process application; it never bypasses the collector.

The synthetic dataset (at least 200 accounts, config and planted-effects manifest in
fixtures/dataset/) enters the same way, as a bounded operation schedule:

  uv run flight-recorder reset
  uv run flight-recorder seed-dataset

seed-dataset submits the canonical nine, the generated events, one attribution per
stage-1 outcome version at the scheduled cutoff, and two stage-2 outcome events that
stay awaiting attribution. Running it again is a no-op. Running `attribute` after
seed-dataset attributes the stage-2 outcomes and changes the demo's initial standing;
`reset` followed by seed-dataset is the only way back to a fresh seed.
