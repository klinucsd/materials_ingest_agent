"""Decision records: one question or proposal, and the answer a person gave.

The record shape mirrors the collaborator's own `graph/mapping-ledger.json`,
which already solves this problem one layer up: 633 candidates, each carrying
evidence, named open questions, and a flag quintet that a person must flip.
His `verify_bundle.py` enforces that those flags never flip on their own. Ours
carries the same five fields for the same reason, and the agent has no code
path that can change them.

Two ids, deliberately:

  `id`          includes the bundle's content hash, so you can see that the
                same question recurred against changed bytes.
  `logical_id`  excludes it: rule label + target + bundle. "The same question
                about the same rule and the same thing" is recognised as
                already answered even after the bundle was edited.

Without that split the agent either re-asks an answered question every time
the file changes, or silently treats a stale answer as covering new content.

Nothing here writes to the database, and nothing here can approve anything.
`answer()` is reachable only from the `mia.py answer` CLI, which a person runs.
"""
from __future__ import annotations

import datetime
import hashlib
import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DECISIONS = ROOT / "data" / "decisions"
LEDGER = ROOT / "data" / "decision-ledger.json"

CONTRACT = "feder-materials-ingest-decision/1"

# The flag quintet, copied from the collaborator's mapping ledger. The agent
# writes these once, at these values, and has no path to change them. Only a
# person, through `mia.py answer`, adds the `human` block beside them -- and
# even that leaves these five exactly as they are.
FLAGS = {
    "status": "tentative_review_required",
    "executable": False,
    "review_required": True,
    "execution_review_status": "review_required",
    "population_authorized": False,
}

HUMAN_DECISIONS = {
    "approved":  "apply the proposal as written",
    "answered":  "here is the fact you asked for; use it",
    "amended":   "the proposal is nearly right; use this instead",
    "rejected":  "do not do this",
    "deferred":  "not now; stop asking",
}


def _now():
    return datetime.datetime.now(datetime.timezone.utc).isoformat(
        timespec="seconds").replace("+00:00", "Z")


def _norm_target(rejection):
    """What this rejection is ABOUT, normalised enough to match across edits."""
    for key in ("json_pointer", "dataset", "artifact", "path"):
        v = rejection.get(key)
        if v:
            return str(v)
    for key in ("datasets", "artifacts"):
        v = rejection.get(key)
        if v:
            return str(sorted(v)[0])
    return ""


def bundle_digests(bundle: Path) -> dict:
    out = {}
    for p in sorted(bundle.rglob("*")):
        if p.is_file():
            out[str(p.relative_to(bundle))] = hashlib.sha256(
                p.read_bytes()).hexdigest()
    return out


def make_record(rejection: dict, verdict: dict, bundle: Path,
                *, attempt: int = 1, env: dict | None = None,
                digests: dict | None = None) -> dict:
    """Build a record. It carries no question or proposal yet -- that is the
    agent's to write, and it may write exactly one of the two."""
    digests = digests if digests is not None else bundle_digests(bundle)
    rel = bundle.name
    label = rejection.get("rule_label") or rejection.get("message", "")
    target = _norm_target(rejection)

    logical = hashlib.sha1(
        "|".join([label, target, rel]).encode()).hexdigest()[:24]
    fingerprint = hashlib.sha256("|".join(
        [label, target, rel, json.dumps(digests, sort_keys=True)]
    ).encode()).hexdigest()

    record = {
        "contract": CONTRACT,
        "id": f"decision-{fingerprint[:24]}",
        "logical_id": f"identity-{logical}",
        "input_fingerprint": fingerprint,
        "created_utc": _now(),

        "bundle": rel,
        "bundle_sha256": digests,
        "attempt": attempt,
        "stage": rejection.get("stage"),

        "rejection": {k: v for k, v in rejection.items() if k != "stage"},

        "class": verdict.get("class"),
        "decision_type": verdict.get("decision_type"),
        "confidence": verdict.get("confidence"),
        "why_classified": verdict.get("why"),

        "evidence": [],
        "question": None,
        "proposal": None,
        "dependencies": [],
        "unresolved": [],

        **FLAGS,

        "human": None,
        "env": env or {},
    }
    return record


def save(record: dict) -> Path:
    DECISIONS.mkdir(parents=True, exist_ok=True)
    path = DECISIONS / f"{record['id']}.json"
    path.write_text(json.dumps(record, indent=1) + "\n")
    _index(record)
    return path


def _index(record: dict):
    rows = json.loads(LEDGER.read_text()) if LEDGER.is_file() else []
    rows = [r for r in rows if r.get("id") != record["id"]]
    rows.append({
        "id": record["id"],
        "logical_id": record["logical_id"],
        "bundle": record["bundle"],
        "class": record["class"],
        "decision_type": record["decision_type"],
        "confidence": record["confidence"],
        "message": (record["rejection"].get("message") or "")[:120],
        "status": record["status"],
        "human": (record.get("human") or {}).get("decision"),
        "created_utc": record["created_utc"],
    })
    LEDGER.parent.mkdir(parents=True, exist_ok=True)
    LEDGER.write_text(json.dumps(rows, indent=1) + "\n")


def load(record_id: str) -> dict | None:
    path = DECISIONS / f"{record_id}.json"
    if not path.is_file():
        matches = sorted(DECISIONS.glob(f"*{record_id}*.json")) \
            if DECISIONS.is_dir() else []
        if len(matches) != 1:
            return None
        path = matches[0]
    return json.loads(path.read_text())


def all_records() -> list[dict]:
    if not DECISIONS.is_dir():
        return []
    return [json.loads(p.read_text()) for p in sorted(DECISIONS.glob("*.json"))]


def answered_logical_ids() -> dict:
    """logical_id -> the human block, for every record a person has decided."""
    out = {}
    for r in all_records():
        if r.get("human"):
            out[r["logical_id"]] = r["human"]
    return out


def question_signature(record: dict) -> str:
    """A hash of the agent-authored part of the record.

    The answer is bound to the exact question asked. If the agent later edits
    the question or the proposal, the stored signature no longer matches and
    the answer must not be treated as covering it.
    """
    body = {k: record.get(k) for k in
            ("rejection", "class", "decision_type", "confidence",
             "question", "proposal", "evidence", "dependencies")}
    return hashlib.sha256(
        json.dumps(body, sort_keys=True, default=str).encode()).hexdigest()


def answer(record_id: str, decision: str, text: str | None = None,
           amend: dict | None = None, by: str | None = None) -> dict:
    """Record a person's decision. The ONLY writer of the `human` block.

    The flag quintet is untouched -- deliberately, mirroring the collaborator's
    own invariant that those flags never flip automatically. An answered record
    is still `review_required: true` and still `population_authorized: false`;
    what changes is that there is now an answer beside the question.
    """
    if decision not in HUMAN_DECISIONS:
        raise ValueError(
            f"decision must be one of {', '.join(sorted(HUMAN_DECISIONS))}")
    record = load(record_id)
    if record is None:
        raise ValueError(f"no decision record matching {record_id!r}")
    if record.get("human"):
        raise ValueError(
            f"{record['id']} was already decided "
            f"({record['human']['decision']} at {record['human']['at_utc']}). "
            f"A decision is not overwritten; re-probe to raise a new one.")

    record["human"] = {
        "decision": decision,
        "answer": text,
        "amended_proposal": amend,
        "by": by or os.environ.get("USER") or "unknown",
        "at_utc": _now(),
        "signature": question_signature(record),
    }
    record["status"] = "human_decided"
    save(record)
    return record


def honours_signature(record: dict) -> bool:
    """Whether a stored answer still matches the question it was given for."""
    human = record.get("human")
    if not human:
        return False
    return human.get("signature") == question_signature(record)
