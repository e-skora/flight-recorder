"""Logic artifacts and the deterministic evaluator.

`rules` is the closed grammar `evaluator-v1` parses; `evaluator` is `R(L, H)`
itself. Both are pure. Reading a recorded decision back out of the ledger and
verifying its artifact identity is `flight_recorder.replay`.
"""
