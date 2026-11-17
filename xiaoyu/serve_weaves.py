"""Read-only batch views, independent of the bounded runtime event feed."""
from __future__ import annotations

import copy
import json
from pathlib import Path
from typing import Any

JOURNAL_EVENT = "serve.qixiang.updated"
MAX_BATCHES = 64
EXECUTION_EVENT = "serve.qixiang.execution.updated"


class WeaveView:
    def __init__(self) -> None:
        self.batches: dict[str, dict[str, Any]] = {}
        self.warning = ""
        self.integrity = True
        self.truncated = False
        self.executions: dict[str, dict[str, Any]] = {}
        self.current: dict[tuple[str, str], str] = {}

    def accept(self, batch: dict[str, Any]) -> bool:
        identity, revision, items = batch.get("batch_id"), batch.get("revision"), batch.get("items")
        if (not isinstance(identity, str) or not identity
                or type(revision) is not int or revision < 1
                or not isinstance(items, list) or len(items) > 64
                or not all(isinstance(item, dict) and isinstance(item.get("id"), str) and item["id"] for item in items)
                or len({item["id"] for item in items}) != len(items)):
            self.integrity = False
            self.warning = "部分协作记录无法读取。"
            return False
        previous = self.batches.get(identity)
        if previous and previous["revision"] >= revision:
            return False
        self.batches[identity] = copy.deepcopy(batch)
        while len(self.batches) > MAX_BATCHES:
            evicted = next(iter(self.batches))
            del self.batches[evicted]
            self.executions = {key: value for key, value in self.executions.items() if value["batch_id"] != evicted}
            self.current = {key: value for key, value in self.current.items() if key[0] != evicted}
            self.truncated = True
            if self.integrity:
                self.warning = "仅显示最近 64 批协作记录。"
        return True

    def record(self, event: dict[str, Any], log: Any, run_id: str) -> None:
        batch = {key: copy.deepcopy(value) for key, value in event.items()
                 if key not in {"kind", "session_id", "run_id", "request_id", "task_id"}}
        if isinstance(batch.get("items"), tuple):
            batch["items"] = list(batch["items"])
        previous = self.batches.get(batch.get("batch_id"), {})
        batch["source_run_id"] = previous.get("source_run_id", previous.get("run_id", run_id))
        batch["run_id"] = batch["source_run_id"]
        if previous:
            for key in ("source_batch_id", "source_item_ids"):
                if key in previous:
                    batch[key] = copy.deepcopy(previous[key])
                else:
                    batch.pop(key, None)
        if not self.accept(batch):
            return
        if log is None:
            self.integrity = False
            self.warning = "协作状态没有持久化日志，退出服务后无法恢复。"
            return
        try:
            log.commit_event(JOURNAL_EVENT, batch=batch)
        except OSError:
            self.integrity = False
            self.warning = "协作状态保存失败；当前结果仍可查看，重启后可能不完整。"

    def accept_execution(self, execution: Any) -> bool:
        if (not isinstance(execution, dict)
                or any(not isinstance(execution.get(key), str) or not execution[key]
                       for key in ("batch_id", "item_id", "execution_id"))
                or type(execution.get("revision")) is not int or execution["revision"] < 1
                or execution.get("execution_state") not in {"starting", "running", "settled"}
                or execution.get("execution_phase") not in {"primary", "follow_up"}
                or not isinstance(execution.get("controls", []), (list, tuple))
                or len(execution.get("controls", [])) > 128
                or not all(isinstance(control, dict) for control in execution.get("controls", []))):
            self.integrity = False
            self.warning = "部分条目执行记录无法读取。"
            return False
        batch = self.batches.get(execution["batch_id"])
        if not batch or not any(item.get("id") == execution["item_id"] for item in batch["items"]):
            self.integrity = False
            self.warning = "条目执行记录缺少所属批次。"
            return False
        eid = execution["execution_id"]
        previous = self.executions.get(eid)
        if previous and (previous["batch_id"] != execution["batch_id"] or previous["item_id"] != execution["item_id"]):
            self.integrity = False
            self.warning = "条目执行身份发生冲突。"
            return False
        if previous and previous["revision"] >= execution["revision"]:
            return False
        if previous is None:
            self.current[(execution["batch_id"], execution["item_id"])] = eid
        value = copy.deepcopy(execution)
        value["source_run_id"] = batch.get("source_run_id", batch.get("run_id", ""))
        value["live"] = bool(value.get("live", value["execution_state"] in {"starting", "running"}))
        value["recovered"] = False
        self.executions[eid] = value
        return True

    def record_execution(self, event: dict, log: Any) -> bool:
        execution = {key: copy.deepcopy(value) for key, value in event.items()
                     if key not in {"kind", "session_id", "run_id", "request_id", "task_id"}}
        if not self.accept_execution(execution):
            return False
        try:
            if log is None:
                raise OSError("no persistent log")
            log.commit_event(EXECUTION_EVENT, execution=execution)
        except OSError:
            self.integrity = False
            self.warning = "条目执行状态保存失败；重启后可能不完整。"
        return True

    def restore(self, path: Path) -> None:
        try:
            with path.open(encoding="utf-8", errors="replace") as handle:
                for line in handle:
                    if not line.endswith("\n"):
                        self.integrity = False
                        self.warning = "日志尾部尚未完整写入，协作状态可能不完整。"
                        continue
                    try:
                        record = json.loads(line)
                    except ValueError:
                        self.integrity = False
                        self.warning = "日志含损坏记录，协作状态可能不完整。"
                        continue
                    if not isinstance(record, dict):
                        self.integrity = False
                        self.warning = "日志含损坏记录，协作状态可能不完整。"
                        continue
                    if record.get("event") == JOURNAL_EVENT:
                        batch = record.get("batch")
                        if isinstance(batch, dict):
                            self.accept(batch)
                        else:
                            self.integrity = False
                            self.warning = "部分协作记录无法读取。"
                    elif record.get("event") == EXECUTION_EVENT:
                        self.accept_execution(record.get("execution"))
        except OSError:
            self.integrity = False
            self.warning = "协作日志不可读取。"
        for execution in self.executions.values():
            execution["live"] = False
            execution["recovered"] = True
            if execution["execution_state"] != "settled":
                execution["recorded_execution_state"] = execution["execution_state"]
                execution["execution_state"] = "unknown"
        for batch in self.batches.values():
            if not batch.get("final"):
                batch["interrupted_by_restart"] = True
                for item in batch["items"]:
                    if item.get("status") in {"queued", "running"}:
                        item["status"] = "interrupted_by_restart"
                        item["error"] = "服务中断，未记录可靠结算；请检查已有成果。"

    def snapshot(self) -> list[dict[str, Any]]:
        batches = copy.deepcopy(list(reversed(self.batches.values())))
        for batch in batches:
            for item in batch["items"]:
                identity = self.current.get((batch["batch_id"], item.get("id")))
                execution = self.executions.get(identity, {})
                if execution:
                    item.update({key: copy.deepcopy(value) for key, value in execution.items()
                                 if key not in {"batch_id", "item_id", "kind", "revision", "controls", "workspace", "model"}})
                    item["execution_workspace"] = execution.get("workspace", "")
                    item["execution_model"] = execution.get("model", "")
                else:
                    item.update(live=False, recovered=bool(batch.get("interrupted_by_restart")))
        return batches
