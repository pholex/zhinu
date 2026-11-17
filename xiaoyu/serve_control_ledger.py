"""Private durable effect receipts shared by distinct execution identity domains."""
from __future__ import annotations

import copy
import json
import time
from typing import Any

class DurableControlLedger:
    def __init__(self, *, journal, snapshot_event, payload_field, identity_fields,
                 warning, status_map=None, final=None):
        self.journal = journal
        self.snapshot_event = snapshot_event
        self.payload_field = payload_field
        self.identity_fields = identity_fields
        self.failure_warning = warning
        self.status_map = status_map or {}
        self.final = final or {"accepted", "not_consumed", "settled", "failed"}
        self.operations: dict[str, dict[str, Any]] = {}
        self.keys: dict[str, str] = {}
        self.integrity = True
        self.warning = ""

    def fail(self):
        self.integrity = False
        self.warning = self.failure_warning

    def save(self, operation, log):
        # Remember even an uncertain fsync: no side effect is retried in process.
        self.operations[operation["operation_id"]] = copy.deepcopy(operation)
        self.keys[operation["key"]] = operation["operation_id"]
        try:
            log.commit_event(self.journal, operation=operation)
        except (OSError, AttributeError):
            self.fail()
            self.operations[operation["operation_id"]].update(recorded_status=operation["status"], status="unknown", reason=self.warning)
            raise OSError(self.warning) from None

    def observe(self, controls):
        for value in controls:
            if not isinstance(value, dict):
                continue
            operation = self.operations.get(value.get("operation_id"))
            if not operation or any(value.get(field) != operation.get(field) for field in
                                    self.identity_fields):
                continue
            status = self.status_map.get(value.get("status"), value.get("status"))
            if status not in {"queued", "accepted", "not_consumed", "requested", "settled", "stop_requested", "stopped"}:
                continue
            # Older queued observations may arrive after an immediate accepted return.
            if operation["status"] in self.final and status not in self.final:
                continue
            operation.update(status=status, updated_at=value.get("updated_at", time.time()),
                             reason=value.get("reason", ""))

    def observe_snapshot(self, payload):
        self.observe(payload.get("controls", []))

    def restore(self, path):
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    try:
                        record = json.loads(line)
                        if not line.endswith("\n") or not isinstance(record, dict):
                            raise ValueError()
                        if record.get("event") == self.journal:
                            value = record["operation"]
                            if not isinstance(value, dict) or not all(isinstance(value.get(k), str) for k in
                                ("operation_id", "key", "digest", "status", *self.identity_fields)):
                                raise ValueError()
                            self.operations[value["operation_id"]] = value
                            self.keys[value["key"]] = value["operation_id"]
                        elif record.get("event") == self.snapshot_event:
                            self.observe_snapshot(record.get(self.payload_field, {}))
                    except (ValueError, KeyError, TypeError, AttributeError):
                        self.fail()
        except OSError:
            self.fail()
        for operation in self.operations.values():
            if operation["status"] not in self.final:
                operation.update(recorded_status=operation["status"], status="unknown",
                                 reason="服务已重启，无法确认旧操作是否被消费；不会自动重放。")


def public(operation):
    return {k: copy.deepcopy(v) for k, v in operation.items() if k not in {"key", "digest"}}
