"""Write exactly one of a question or a proposal onto a decision record.

`make_record` deliberately leaves both empty: deciding which of the two a
rejection deserves is the judgement this whole tool exists to get right, so it
happens here, in one place, where it can be read.

The rule has two halves and both must hold before anything is proposed:

    the taxonomy must permit it   -- `may_propose`, which is false wherever
                                     the answer belongs to whoever ran the
                                     experiment; and
    the edit must be derivable    -- we must be able to state the exact change
                                     from the rejection itself, with nothing
                                     inferred.

Where the taxonomy permits a proposal but the edit cannot be derived, the
result is a QUESTION, not a guess. That asymmetry is deliberate: a question we
did not need to ask costs someone a minute, and a proposal we should not have
made costs the dataset its credibility.

Today exactly one edit is derivable: a `const` violation, where the contract
names the one permitted value and the rejection already carries it. Required
properties and unit declarations are classified mechanical by the taxonomy and
could join it, but only once the probe reports the pinned value alongside the
error -- until then they are asked about, which is the safe direction.

Nothing here applies anything. A proposal is a written intention; only
`apply` acts on it, and only after a person has approved it.
"""
from __future__ import annotations

import json
from pathlib import Path

from taxonomy import may_propose

# Where a payload-stage rejection's json_pointer is rooted.
PAYLOAD_FILE = "payload.json"


def _derive_edit(rejection: dict) -> dict | None:
    """The exact change this rejection determines, or None if it does not.

    Only `const` qualifies. The contract names one permitted value, the
    rejection carries it in `validator_value`, and `json_pointer` says where
    it goes -- so the edit is read off the rejection rather than reasoned
    about.
    """
    if rejection.get("validator") != "const":
        return None
    pointer = rejection.get("json_pointer")
    if not pointer or pointer == "/":
        return None
    required = rejection.get("validator_value")
    # A truncated value is a summary, not the value itself; `_small` replaces
    # bulk with a description, and applying that would write the description
    # into the document.
    if isinstance(required, str) and required.startswith("<") \
            and required.endswith(" chars>"):
        return None
    return {"file": PAYLOAD_FILE, "json_pointer": pointer, "to": required}


def attach(record: dict) -> dict:
    """Give the record its question or its proposal. Never both."""
    rejection = record.get("rejection") or {}
    verdict = {"class": record.get("class"),
               "decision_type": record.get("decision_type"),
               "confidence": record.get("confidence")}
    message = rejection.get("message") or ""
    where = rejection.get("json_pointer") or rejection.get("rule_label") or ""

    edit = _derive_edit(rejection) if may_propose(verdict) else None

    if edit is None:
        record["question"] = {
            "text": _question_text(record, message, where),
            "why_not_proposing": record.get("why_classified") or
            "the correct value is not determined by the contract",
        }
        record["proposal"] = None
        return record

    record["proposal"] = {
        "action": "set",
        "file": edit["file"],
        "json_pointer": edit["json_pointer"],
        "to": edit["to"],
        "why": f"the contract permits one value at {edit['json_pointer']} "
               f"and the document carries a different one",
        # What the run checks for after applying. A proposal that does not
        # clear what it said it would is reopened rather than passed over --
        # the prediction is the only thing distinguishing a fix that worked
        # from one that merely stopped this particular complaint.
        "predicted_effect": {"clears": [record["logical_id"]]},
    }
    record["question"] = None
    return record


def _question_text(record: dict, message: str, where: str) -> str:
    bundle = record.get("bundle") or "the bundle"
    if where:
        return (f"{bundle}: {message} (at {where}). The contract does not "
                f"determine the right value here. What should it be, or "
                f"should this record be built without it?")
    return (f"{bundle}: {message}. The contract does not determine the right "
            f"value here. What should it be?")


def apply(bundle: Path, proposal: dict) -> bool:
    """Make the approved edit on a bundle copy. Returns whether it changed anything.

    Bundles under data/bundles are copies for exactly this reason -- the kit
    is write-protected and its digests are part of the validator fingerprint,
    so nothing may edit the originals.
    """
    target = bundle / proposal["file"]
    if not target.is_file():
        raise ValueError(f"{proposal['file']} is not in {bundle}")
    doc = json.loads(target.read_text())

    parts = [p for p in proposal["json_pointer"].split("/") if p]
    if not parts:
        raise ValueError("a proposal must name a field, not the whole document")
    node = doc
    for part in parts[:-1]:
        if part not in node:
            raise ValueError(
                f"{proposal['json_pointer']} does not exist in "
                f"{proposal['file']}; the document is not the one the "
                f"proposal was written against")
        node = node[part]
    key = parts[-1]
    if node.get(key) == proposal["to"]:
        return False
    node[key] = proposal["to"]
    target.write_text(json.dumps(doc, indent=2) + "\n")
    return True
