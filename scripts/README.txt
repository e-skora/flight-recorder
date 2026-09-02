Seed and reset live behind the console entry point (D-010 "scripts/ holds seed and reset"):

  uv run flight-recorder reset
  uv run flight-recorder seed
  uv run flight-recorder serve

The seed submits fixtures/canonical/ through POST /api/v1/decision-events on the
in-process application; it never bypasses the collector.
