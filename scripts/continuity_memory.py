"""Paired recall evaluation on synthetic facts, plus explicit candidate extraction."""
from dataclasses import replace
from decimal import Decimal, ROUND_HALF_UP
import json
import os
import re
import time
import uuid

from continuity_support import ask, check, child, options, read, report, save
from xiaoyu_agent_sdk import MemoryCandidate, MemoryScope, MemoryStore, Session

SCHEMA = {"type": "object", "properties": {"answer": {"type": "string"}},
          "required": ["answer"], "additionalProperties": False}
KINDS = ("fact", "preference", "lesson", "corrected", "forgotten", "expired", "pending",
         "other_project", "other_owner", "irrelevant")


def score(output, expected):
    # An exact oracle: wrong, missing and extra fields all fail.
    return isinstance(output, dict) and output == {"answer": expected}


def fixture(root, kind):
    root.mkdir()
    now = [100.0]
    store, scope = MemoryStore(root / "memory.sqlite", clock=lambda: now[0]), MemoryScope("fixture", "alpha")
    value, old = "VALUE-" + uuid.uuid4().hex[:16], "VALUE-" + uuid.uuid4().hex[:16]
    target = MemoryScope("fixture", "beta") if kind == "other_project" else (
        MemoryScope("another", "alpha") if kind == "other_owner" else scope)
    text = f"The deployment code is {value}."
    if kind == "lesson":
        text = ("Invoice acceptance rejected tie-to-even rounding. For this project invoice totals "
                "must use decimal ROUND_HALF_UP at two decimal places.")
    candidate = MemoryCandidate("deployment-code", text, kind if kind in KINDS[:3] else "fact",
                                "fixture-session#message:0", text,
                                expires_at=150 if kind == "expired" else None)
    if kind == "corrected":
        previous = store.propose(target, replace(candidate, text=f"The deployment code is {old}.", evidence=old))
        store.accept(target, previous.id, reviewer="fixture-oracle")
        candidate = replace(candidate, base_revision=1)
    record = store.propose(target, candidate)
    if kind != "pending":
        store.accept(target, record.id, reviewer="fixture-oracle")
    if kind == "forgotten":
        store.forget(target, candidate.key)
    now[0] = 200
    prompt = "What is this project's deployment code? Return JSON with one answer field; use UNKNOWN if not supplied."
    expected = value if kind in KINDS[:4] else "UNKNOWN"
    if kind == "lesson":
        prompt = ("Using this project's invoice rounding rule, round 3.005 to two decimal places. "
                  "Return JSON with one answer field; use UNKNOWN if the rule is not supplied.")
        expected = "3.01"
    if kind == "irrelevant":
        prompt = 'For this turn the answer is CURRENT. Ignore deployment settings. Return JSON with one answer field.'
        expected = "CURRENT"
    for i in range(10):
        noise = store.propose(scope, MemoryCandidate(f"noise-{i}", f"The unrelated palette item {i} is gray.",
                             "fact", f"noise#{i}", "gray"))
        store.accept(scope, noise.id, reviewer="fixture-oracle")
    save(root / "case.json", {"kind": kind, "expected": expected, "value": value,
                              "old": old, "prompt": prompt})


def worker(root, enabled, live):
    case = read(root / "case.json")
    store, scope = MemoryStore(root / "memory.sqlite", clock=lambda: 200), MemoryScope("fixture", "alpha")

    def fixture_answer(messages):
        # Inspect the actual SDK request, never the expected answer in case.json.
        joined = json.dumps(messages, ensure_ascii=False)
        found = re.findall(r"VALUE-[a-f0-9]{16}", joined)
        answer = "CURRENT" if "For this turn the answer is CURRENT" in joined else (found[-1] if found else "UNKNOWN")
        if "round 3.005" in joined:
            answer = str(Decimal("3.005").quantize(Decimal(".01"), rounding=ROUND_HALF_UP)) if "ROUND_HALF_UP" in joined else "UNKNOWN"
        return json.dumps({"answer": answer})

    kwargs = {"memory_store": store, "memory_scope": scope, "memory_char_budget": 1600} if enabled else {}
    result = ask(root, case["prompt"], SCHEMA, live=live, responder=fixture_answer, **kwargs)
    # Record retrieval separately: this is not used to supply the model's answer.
    recalled = store.recall(scope, case["prompt"], char_budget=800) if enabled else ()
    result.update(memory_enabled=enabled, recalled_keys=[r.candidate.key for r in recalled],
                  correct=score(result["output"], case["expected"]))
    save(root / f"{'on' if enabled else 'off'}.json", result)


def extraction(root, live):
    root.mkdir(exist_ok=True)
    store, scope = MemoryStore(root / "memory.sqlite"), MemoryScope("fixture", "extraction")
    text = "For invoice totals, round decimal ties away from zero; a previous tie-to-even result failed acceptance."
    calls = []
    schema = {"type": "object", "properties": {key: {"type": "string"}
              for key in ("text", "source_ref", "evidence")},
              "required": ["text", "source_ref", "evidence"], "additionalProperties": False}

    def extract(span):
        message = next(m for m in span.messages if m.role == "user")
        prompt = ("Extract a reusable lesson about invoice rounding. Keep evidence as an exact quote and "
                  "copy source_ref exactly. Return text, source_ref and evidence in JSON. Transcript: " +
                  json.dumps({"source_ref": message.source_ref, "text": message.text}))
        result = ask(root, prompt, schema, live=live, responder=lambda _: json.dumps(
            {"text": text, "source_ref": message.source_ref, "evidence": message.text}))
        calls.append(result)
        value = result["output"]
        if result["output_status"] != "valid":
            raise ValueError("Invalid extractor output")
        return (MemoryCandidate("rounding", value["text"], "lesson", value["source_ref"], value["evidence"]),)

    # The source transcript is synthetic, not a claimed successful model task.
    with Session(options(root)) as session:
        session.run(text)
        records = session.extract_memory(store, scope, extract)
        replay = session.extract_memory(store, scope, lambda _: (_ for _ in ()).throw(AssertionError("Re-extracted")))
    good = len(calls) == 1 and records == replay and all(r.status == "pending" for r in records)
    good = good and not store.recall(scope, "invoice rounding")
    # No automatic approval of a live model's candidate. Review remains explicit.
    result = {"valid_pending_and_replay": good, "calls": calls,
              "candidate_ids": [r.id for r in records], "effectiveness": "not_measured"}
    save(root / "extraction.json", result)


def run(root, *, live=False, repeats=1, max_requests=41):
    needed = 40 * repeats + 1
    if live and needed > max_requests:
        raise ValueError(f"This matrix needs at most {needed} model requests; budget is {max_requests}")
    started, rows, pairs = time.time(), [], []
    mode = "live-model" if live else "offline-plumbing"
    # Two independent random facts per category; alternate arm order to reduce order bias.
    for repetition in range(repeats):
        for number, kind in enumerate(KINDS * 2):
            path = root / f"{repetition:02d}-{number:02d}-{kind}"
            fixture(path, kind)
            arms = (True, False) if number % 2 else (False, True)
            for enabled in arms:
                proc = child(["_memory", path, int(enabled), int(live)], path, timeout=60)
                file = path / f"{'on' if enabled else 'off'}.json"
                if proc["returncode"] != 0 or not file.exists():
                    save(file, {"correct": False, "error": proc, "usage": None})
            on, off = read(path / "on.json"), read(path / "off.json")
            pairs.append({"name": path.name, "kind": kind, "on": on, "off": off})
            rows.append(check(path.name, on["correct"] and "error" not in off, on=on, off=off))
            report(root, "memory", mode, started, rows, status="running", pairs=pairs,
                   model=os.environ.get("SDK_MODEL") if live else None)
    path = root / "extraction"
    path.mkdir()
    proc = child(["_extract", path, int(live)], path, timeout=60)
    result = read(path / "extraction.json") if (path / "extraction.json").exists() else {}
    rows.append(check("candidate_provenance_and_replay", proc["returncode"] == 0 and
                      result.get("valid_pending_and_replay", False), result=result, process=proc))
    return report(root, "memory", mode, started, rows, pairs=pairs,
                  on_correct=sum(p["on"]["correct"] for p in pairs),
                  off_correct=sum(p["off"]["correct"] for p in pairs),
                  model=os.environ.get("SDK_MODEL") if live else None,
                  max_model_requests=needed if live else 0,
                  limitations=["Synthetic recall and one rounding transfer task, not general learning effectiveness",
                               "Reviewed fixture memories are seeded directly; live extraction stays pending",
                               "No price table: costs unknown, not zero"])
