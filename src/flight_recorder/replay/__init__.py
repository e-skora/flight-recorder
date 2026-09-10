"""Decision replay.

`reconstruct` is the original half, `R(Lh(d), H(d))`: verify the preserved
logic artifact's identity, then re-evaluate it over the preserved historical
context and require the recorded result. `counterfactual.replay` and
`counterfactual.compare` are the counterfactual half, `R(Lc, H(d))`: the same
sealed context evaluated under an explicitly selected and verified current
artifact, returned as a separate type with its own label and compared with the
original. The two sides keep separate labels, and no counterfactual is ever
persisted (INV-06).
"""
