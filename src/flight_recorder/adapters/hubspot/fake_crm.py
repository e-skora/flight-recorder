"""Controllable, in-memory SYNTHETIC request ledger. No network client exists."""

from threading import RLock

from flight_recorder.adapters.hubspot.writer_contracts import FakeObservation, require
from flight_recorder.collector.canonical import canonical_bytes
from flight_recorder.collector.schema import WriteProperties


class FakeRejected(Exception):
    """Positive fake response; operation-specific no-effect proof is still required."""


class FakeCRM:
    def __init__(self):
        self.mode = "apply"
        self.send_count = 0
        self.read_count = 0
        self._values = {}
        self._requests = {}
        self._lock = RLock()

    def set_properties(self, company_key, properties):
        with self._lock:
            self._values[company_key] = canonical_bytes(properties)

    def queue_pending(self, company_key, operation_id, properties):
        with self._lock:
            require(operation_id not in self._requests, "fake_operation_already_received")
            self._requests[operation_id] = {
                "company": company_key,
                "properties": canonical_bytes(properties),
                "state": "pending",
            }

    def apply_pending(self, operation_id):
        from flight_recorder.adapters.hubspot.contracts import strict_json_loads

        with self._lock:
            request = self._requests[operation_id]
            require(request["state"] == "pending", "fake_request_already_settled")
            values = strict_json_loads(self._values.get(request["company"], b"{}"))
            values.update(strict_json_loads(request["properties"]))
            self._values[request["company"]] = canonical_bytes(values)
            request["state"] = "applied"

    def reject_pending(self, operation_id):
        with self._lock:
            request = self._requests[operation_id]
            require(request["state"] == "pending", "fake_request_already_settled")
            request["state"] = "not_applied"

    def send(self, company_key, operation_id, properties):
        with self._lock:
            self.send_count += 1
            checked = WriteProperties.model_validate_json(canonical_bytes(properties), strict=True)
            require(checked.fr_op_id == operation_id, "fake_operation_tuple_mismatch")
            self.queue_pending(company_key, operation_id, properties)
            if self.mode in {"reject", "lost_rejection"}:
                self.reject_pending(operation_id)
                if self.mode == "reject":
                    raise FakeRejected("definite fake rejection, no effect")
                raise TimeoutError("fake rejection response lost")
            if self.mode == "timeout":
                raise TimeoutError("fake request may apply later")
            if self.mode == "raise":
                raise RuntimeError("ambiguous fake transport failure")
            require(self.mode in {"apply", "lost_response"}, "unknown_fake_mode")
            self.apply_pending(operation_id)
            if self.mode == "lost_response":
                raise TimeoutError("fake applied response lost")

    def observe(self, company_key):
        with self._lock:
            self.read_count += 1
            owned = {
                key: value
                for key, value in self._requests.items()
                if value["company"] == company_key
            }
            return FakeObservation(
                self._values.get(company_key, b"{}"),
                tuple(sorted(key for key, value in owned.items() if value["state"] != "pending")),
                tuple(sorted(key for key, value in owned.items() if value["state"] == "pending")),
                tuple(
                    sorted(key for key, value in owned.items() if value["state"] == "not_applied")
                ),
            )
