"""Same-file queue admission, poll ownership recovery and atomic terminal release."""

import hashlib
from dataclasses import dataclass

from flight_recorder.adapters.crm.contracts import Ownership, require
from flight_recorder.adapters.crm.http import strict_json
from flight_recorder.collector.canonical import canonical_bytes, canonical_hash


@dataclass(frozen=True)
class MemberProgress:
    target_key: str
    state: str
    reason: str | None
    unit_id: str | None = None
    decision_event_id: str | None = None


@dataclass(frozen=True)
class PollReport:
    connection_id: str
    tick_id: str
    disposition: str
    members: tuple[MemberProgress, ...]


def claim_id(connection, tick, target, binding):
    return canonical_hash(
        dict(connection_id=connection, tick_id=tick, target_key=target, config_binding=binding)
    )


def _decode(body, digest):
    require(type(body) is bytes and hashlib.sha256(body).hexdigest() == digest, "poll_integrity")
    d = strict_json(body)
    require(canonical_bytes(d) == body, "poll_integrity")
    return d


class PollStore:
    def __init__(self, store, config, authority):
        require(store._format_version in (4, 5), "poll_store_uninitialized")
        self.store, self.config, self.authority = store, config, authority
        self.connection_id = config.connection.connection_id

    def _guard(self):
        self.authority.assert_held()

    def _tick(self, conn, tick):
        row = conn.execute(
            (
                "SELECT ordinal,body,body_sha256 FROM crm_poll_ticks WHERE co"
                "nnection_id=? AND tick_id=?"
            ),
            (self.connection_id, tick),
        ).fetchone()
        require(row is not None, "unknown_poll_tick")
        d = _decode(row[1], row[2])
        require(
            type(d) is dict
            and set(d)
            == {
                "format_version",
                "connection_id",
                "tick_id",
                "ordinal",
                "config_binding",
                "received_at",
                "disposition",
                "targets",
            }
            and type(d["format_version"]) is int
            and d["format_version"] == 1,
            "poll_integrity",
        )
        require(
            (d["connection_id"], d["tick_id"], d["ordinal"]) == (self.connection_id, tick, row[0])
            and type(d["ordinal"]) is int,
            "poll_integrity",
        )
        require(d["config_binding"] == self.config.binding, "poll_config_conflict")
        require(
            d["disposition"] in ("active", "waiting", "coalesced")
            and d["targets"]
            == ([] if d["disposition"] == "coalesced" else [t.key for t in self.config.targets]),
            "poll_integrity",
        )
        receipt = conn.execute(
            "SELECT body,body_sha256 FROM crm_receipts WHERE connection_id=? AND receipt_id=?",
            (self.connection_id, tick),
        ).fetchone()
        require(receipt == (row[1], row[2]), "poll_integrity")
        progress = conn.execute(
            "SELECT state FROM crm_poll_progress WHERE connection_id=? AND tick_id=?",
            (self.connection_id, tick),
        ).fetchone()
        require(
            progress is not None and progress[0] in {"active", "waiting", "coalesced", "complete"},
            "poll_integrity",
        )
        require(
            (d["disposition"] == "coalesced") == (progress[0] == "coalesced")
            and not (d["disposition"] == "active" and progress[0] == "waiting"),
            "poll_integrity",
        )
        return d, progress[0]

    def _member(self, conn, tick, target):
        d, state = self._tick(conn, tick)
        require(target in d["targets"], "target_not_allowlisted")
        row = conn.execute(
            (
                "SELECT claim_id,generation,state,reason,terminal_id FROM "
                "crm_poll_members WHERE connection_id=? AND tick_id=? AND "
                "target_key=?"
            ),
            (self.connection_id, tick, target),
        ).fetchone()
        require(
            row is not None
            and row[0] == claim_id(self.connection_id, tick, target, self.config.binding),
            "poll_integrity",
        )
        require(
            row[2] in ("queued", "claimed", "read_started", "blocked", "terminal")
            and (row[1] is None) == (row[2] == "queued")
            and (row[4] is not None) == (row[2] == "terminal"),
            "poll_integrity",
        )
        if row[1] is not None:
            require(type(row[1]) is int and row[1] > 0, "poll_integrity")
        return row

    def _terminal(self, conn, tick, target, member):
        row = conn.execute(
            (
                "SELECT body,body_sha256 FROM crm_poll_terminals WHERE "
                "terminal_id=? AND connection_id=? AND tick_id=? AND target_key=?"
            ),
            (member[4], self.connection_id, tick, target),
        ).fetchone()
        require(row is not None, "poll_integrity")
        d = _decode(*row)
        require(
            set(d)
            == {
                "format_version",
                "connection_id",
                "tick_id",
                "target_key",
                "claim_id",
                "generation",
                "config_binding",
                "state",
                "reason",
                "observation_fact_id",
                "unit_id",
                "decision_event_id",
                "submission_hash",
            }
            and d["format_version"] == 1
            and type(d["format_version"]) is int
            and canonical_hash(d) == member[4],
            "poll_integrity",
        )
        require(
            (
                d["connection_id"],
                d["tick_id"],
                d["target_key"],
                d["claim_id"],
                d["generation"],
                d["config_binding"],
            )
            == (self.connection_id, tick, target, member[0], member[1], self.config.binding),
            "poll_integrity",
        )
        fact = self.store._fact(conn, Ownership(target, member[0], member[1]))
        require(
            fact is not None
            and d["observation_fact_id"]
            == self.store._fact_key(Ownership(target, member[0], member[1]))
            and d["unit_id"] == fact["unit_id"],
            "poll_integrity",
        )
        require(
            (d["state"], d["reason"])
            in [
                ("complete", None),
                ("scoring_inputs_unchanged", "scoring_inputs_unchanged"),
                ("refused", "no_usable_inputs"),
                ("refused", "mixed_evidence_reuse_unsupported"),
            ],
            "poll_integrity",
        )
        frozen = self.store._load_submission(conn, Ownership(target, member[0], member[1]))
        if d["state"] == "complete":
            require(
                frozen is not None
                and d["submission_hash"] == frozen.submission_hash
                and d["decision_event_id"] == frozen.document["stages"][-1]["event_id"],
                "poll_integrity",
            )
            accepted, failure, _ = self.store.submission_history(conn, frozen)
            require(
                failure is None
                and accepted == {stage["stage"] for stage in frozen.document["stages"]},
                "poll_integrity",
            )
        else:
            require(
                frozen is None
                and d["decision_event_id"] is None
                and d["submission_hash"] is None
                and (
                    (d["state"] == "scoring_inputs_unchanged")
                    == (fact["state"] == "scoring_inputs_unchanged")
                ),
                "poll_integrity",
            )
        return d

    def _report(self, conn, tick):
        d, state = self._tick(conn, tick)
        result = []
        members = conn.execute(
            (
                "SELECT target_key FROM crm_poll_members WHERE connection_id="
                "? AND tick_id=? ORDER BY target_key"
            ),
            (self.connection_id, tick),
        ).fetchall()
        require([row[0] for row in members] == sorted(d["targets"]), "poll_integrity")
        for target in d["targets"]:
            row = self._member(conn, tick, target)
            if row[2] == "terminal":
                terminal = self._terminal(conn, tick, target, row)
                result.append(
                    MemberProgress(
                        target,
                        terminal["state"],
                        terminal["reason"],
                        terminal["unit_id"],
                        terminal["decision_event_id"],
                    )
                )
            else:
                result.append(MemberProgress(target, row[2], row[3]))
        require(
            (state == "complete")
            == (
                bool(result)
                and all(
                    x.state in ("complete", "refused", "scoring_inputs_unchanged") for x in result
                )
            ),
            "poll_integrity",
        )
        return PollReport(self.connection_id, tick, state, tuple(result))

    def _validate_queue(self, conn):
        ticks = conn.execute(
            "SELECT tick_id FROM crm_poll_ticks WHERE connection_id=? ORDER BY ordinal",
            (self.connection_id,),
        ).fetchall()
        pending = []
        for (tick,) in ticks:
            report = self._report(conn, tick)
            if report.disposition == "coalesced":
                continue
            if report.disposition == "complete":
                require(not pending, "poll_queue_order_integrity")
            else:
                pending.append((tick, report.disposition))
        require(
            len(pending) <= 2
            and (not pending or pending[0][1] == "active")
            and (len(pending) < 2 or pending[1][1] == "waiting"),
            "poll_queue_order_integrity",
        )
        return pending

    def report(self, tick):
        self._guard()
        with self.store._read() as conn:
            self._validate_queue(conn)
            return self._report(conn, tick)

    def admit(self, tick, received_at):
        self._guard()
        with self.store.transaction() as conn:
            self._validate_queue(conn)
            old = conn.execute(
                "SELECT 1 FROM crm_poll_ticks WHERE connection_id=? AND tick_id=?",
                (self.connection_id, tick),
            ).fetchone()
            if old is not None:
                self._tick(conn, tick)
            else:
                pending = conn.execute(
                    (
                        "SELECT state FROM crm_poll_progress WHERE connection_id=? AN"
                        "D state IN ('active','waiting')"
                    ),
                    (self.connection_id,),
                ).fetchall()
                require(
                    len(pending) <= 2
                    and sum(x[0] == "active" for x in pending) <= 1
                    and sum(x[0] == "waiting" for x in pending) <= 1,
                    "poll_integrity",
                )
                state = "coalesced" if len(pending) == 2 else "waiting" if pending else "active"
                ordinal = conn.execute(
                    "SELECT COALESCE(MAX(ordinal),0)+1 FROM crm_poll_ticks WHERE connection_id=?",
                    (self.connection_id,),
                ).fetchone()[0]
                d = dict(
                    format_version=1,
                    connection_id=self.connection_id,
                    tick_id=tick,
                    ordinal=ordinal,
                    config_binding=self.config.binding,
                    received_at=received_at,
                    disposition=state,
                    targets=[] if state == "coalesced" else [t.key for t in self.config.targets],
                )
                body = canonical_bytes(d)
                digest = hashlib.sha256(body).hexdigest()
                conn.execute(
                    "INSERT INTO crm_receipts VALUES (?,?,?,?)",
                    (self.connection_id, tick, body, digest),
                )
                conn.execute(
                    "INSERT INTO crm_poll_ticks VALUES (?,?,?,?,?)",
                    (self.connection_id, tick, ordinal, body, digest),
                )
                conn.execute(
                    "INSERT INTO crm_poll_progress VALUES (?,?,?)",
                    (self.connection_id, tick, state),
                )
                for target in d["targets"]:
                    conn.execute(
                        "INSERT INTO crm_poll_members VALUES (?,?,?,?,NULL,'queued',NULL,NULL)",
                        (
                            self.connection_id,
                            tick,
                            target,
                            claim_id(self.connection_id, tick, target, self.config.binding),
                        ),
                    )
        return self.report(tick)

    def has_tick(self, tick):
        self._guard()
        with self.store._read() as conn:
            return (
                conn.execute(
                    "SELECT 1 FROM crm_poll_ticks WHERE connection_id=? AND tick_id=?",
                    (self.connection_id, tick),
                ).fetchone()
                is not None
            )

    def active_tick(self):
        self._guard()
        with self.store._read() as conn:
            rows = self._validate_queue(conn)
            return rows[0][0] if rows else None

    def claim(self, tick, target):
        self._guard()
        with self.store.transaction() as conn:
            require(self._tick(conn, tick)[1] == "active", "poll_tick_not_active")
            member = self._member(conn, tick, target.key)
            require(member[2] != "terminal", "poll_member_terminal")
            if member[2] == "queued":
                prior = conn.execute(
                    "SELECT unit_id,generation,state FROM crm_target_ownership WHERE target_key=?",
                    (target.key,),
                ).fetchone()
                require(prior is None or prior[2] != "held", "target_busy")
                generation = prior[1] + 1 if prior else 1
                if prior:
                    conn.execute(
                        (
                            "UPDATE crm_target_ownership SET unit_id=?,generation=?,state"
                            "='held' WHERE target_key=?"
                        ),
                        (member[0], generation, target.key),
                    )
                else:
                    conn.execute(
                        "INSERT INTO crm_target_ownership VALUES (?,?,?,'held')",
                        (target.key, member[0], generation),
                    )
                conn.execute(
                    (
                        "UPDATE crm_poll_members SET generation=?,state='claimed',reason="
                        "NULL WHERE connection_id=? AND tick_id=? AND target_key=?"
                    ),
                    (generation, self.connection_id, tick, target.key),
                )
            else:
                generation = member[1]
                owner = Ownership(target.key, member[0], generation)
                self.store.assert_owner(conn, owner)
                if (
                    member[2] in ("read_started", "blocked")
                    and self.store._fact(conn, owner) is None
                ):
                    recovery = dict(
                        format_version=1,
                        target_key=target.key,
                        claim_id=member[0],
                        generation=generation,
                        connection_id=self.connection_id,
                        tick_id=tick,
                        config_binding=self.config.binding,
                        reason="read_lost_before_freeze",
                        next_generation=generation + 1,
                    )
                    body = canonical_bytes(recovery)
                    conn.execute(
                        "INSERT INTO crm_poll_recoveries VALUES (?,?,?,?,?,?)",
                        (
                            canonical_hash(recovery),
                            target.key,
                            member[0],
                            generation,
                            body,
                            hashlib.sha256(body).hexdigest(),
                        ),
                    )
                    generation += 1
                    conn.execute(
                        "UPDATE crm_target_ownership SET generation=? WHERE target_key=?",
                        (generation, target.key),
                    )
                    conn.execute(
                        (
                            "UPDATE crm_poll_members SET generation=?,state='claimed',rea"
                            "son=NULL WHERE connection_id=? AND tick_id=? AND "
                            "target_key=?"
                        ),
                        (generation, self.connection_id, tick, target.key),
                    )
            return Ownership(target.key, member[0], generation)

    def mark_read_started(self, tick, owner):
        self._guard()
        with self.store.transaction() as conn:
            self.store.assert_owner(conn, owner)
            row = self._member(conn, tick, owner.target_key)
            require(
                (row[0], row[1], row[2]) == (owner.unit_id, owner.generation, "claimed"),
                "poll_integrity",
            )
            conn.execute(
                (
                    "UPDATE crm_poll_members SET state='read_started' WHERE "
                    "connection_id=? AND tick_id=? AND target_key=?"
                ),
                (self.connection_id, tick, owner.target_key),
            )

    def blocked(self, tick, owner, reason):
        self._guard()
        with self.store.transaction() as conn:
            self.store.assert_owner(conn, owner)
            row = self._member(conn, tick, owner.target_key)
            require(
                (row[0], row[1]) == (owner.unit_id, owner.generation) and row[2] != "terminal",
                "poll_integrity",
            )
            conn.execute(
                (
                    "UPDATE crm_poll_members SET state='blocked',reason=? WHERE "
                    "connection_id=? AND tick_id=? AND target_key=?"
                ),
                (reason, self.connection_id, tick, owner.target_key),
            )

    def settle(self, tick, owner, collector, *, state, reason=None):
        from flight_recorder.adapters.crm.submit import _verify_domain

        self._guard()
        with self.store.transaction() as conn:
            self.store.assert_owner(conn, owner)
            member = self._member(conn, tick, owner.target_key)
            require(
                (member[0], member[1]) == (owner.unit_id, owner.generation)
                and member[2] != "terminal",
                "poll_integrity",
            )
            fact = self.store._fact(conn, owner)
            require(
                fact is not None
                and fact["config_binding"] == self.config.connection.binding
                and fact["selection_identity"] == self.config.document["selection_identity"]
                and fact["connection_id"] == self.connection_id,
                "poll_integrity",
            )
            frozen = self.store._load_submission(conn, owner)
            decision = submission = None
            if state == "complete":
                require(frozen is not None, "missing_frozen_submission")
                accepted, failure, _ = self.store.submission_history(conn, frozen)
                require(
                    failure is None and accepted == {s["stage"] for s in frozen.document["stages"]},
                    "submission_incomplete",
                )
                _verify_domain(collector, frozen, accepted)
                decision = frozen.document["stages"][-1]["event_id"]
                submission = frozen.submission_hash
                require(reason is None, "poll_integrity")
            elif state == "scoring_inputs_unchanged":
                require(
                    fact["state"] == "scoring_inputs_unchanged"
                    and reason == state
                    and frozen is None,
                    "poll_integrity",
                )
            else:
                require(
                    state == "refused"
                    and reason in ("no_usable_inputs", "mixed_evidence_reuse_unsupported")
                    and fact["state"] == "captured"
                    and frozen is None,
                    "poll_integrity",
                )
            terminal = dict(
                format_version=1,
                connection_id=self.connection_id,
                tick_id=tick,
                target_key=owner.target_key,
                claim_id=owner.unit_id,
                generation=owner.generation,
                config_binding=self.config.binding,
                state=state,
                reason=reason,
                observation_fact_id=self.store._fact_key(owner),
                unit_id=fact["unit_id"],
                decision_event_id=decision,
                submission_hash=submission,
            )
            body = canonical_bytes(terminal)
            identity = canonical_hash(terminal)
            conn.execute(
                "INSERT INTO crm_poll_terminals VALUES (?,?,?,?,?,?)",
                (
                    identity,
                    self.connection_id,
                    tick,
                    owner.target_key,
                    body,
                    hashlib.sha256(body).hexdigest(),
                ),
            )
            conn.execute(
                (
                    "UPDATE crm_poll_members SET state='terminal',reason=?,terminal_id"
                    "=? WHERE connection_id=? AND tick_id=? AND target_key=?"
                ),
                (reason, identity, self.connection_id, tick, owner.target_key),
            )
            conn.execute(
                "UPDATE crm_target_ownership SET state='released' WHERE target_key=?",
                (owner.target_key,),
            )
            pending = conn.execute(
                (
                    "SELECT 1 FROM crm_poll_members WHERE connection_id=? AND tic"
                    "k_id=? AND state!='terminal'"
                ),
                (self.connection_id, tick),
            ).fetchone()
            if pending is None:
                conn.execute(
                    (
                        "UPDATE crm_poll_progress SET state='complete' WHERE connecti"
                        "on_id=? AND tick_id=?"
                    ),
                    (self.connection_id, tick),
                )
                conn.execute(
                    (
                        "UPDATE crm_poll_progress SET state='active' WHERE connection"
                        "_id=? AND state='waiting'"
                    ),
                    (self.connection_id,),
                )
