"""Decision replay.

`reconstruct` is the original half, `R(Lh(d), H(d))`: verify the preserved
logic artifact's identity, then re-evaluate it over the preserved historical
context and require the recorded result. The counterfactual half,
`R(Lc, H(d))`, is a separate computation with separate labels (INV-06) and
arrives in Phase 3.
"""
