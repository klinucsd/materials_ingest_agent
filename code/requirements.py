"""What the target schema demands of one class, read out of the schema itself.

This is deterministic on purpose, and it is the load-bearing half of the
mapping step. The model is never asked "what does an ExposureEpisode need?" --
it is told, from the collaborator's own catalog and his own rule predicates. It
is asked only the narrow question it is actually good at: *does this source
provide that, and where does it say so?*

The difference matters. A model asked to invent the requirements will produce a
plausible list, omit the awkward ones, and nothing will catch it. A model handed
the real list can only answer about each item, and every answer is checkable
against the source text.
"""
from __future__ import annotations

import json
import os
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
KIT = Path(os.environ.get("MIA_KIT_DIR") or ROOT / "data" / "kit")
CATALOG = KIT / "model" / "catalog.json"
PREDICATES = ROOT / "data" / "rule_predicates.json"

# `CARDINALITY Class.prop` with a `< 1` lower bound is a required property.
_CARD = re.compile(r"^CARDINALITY (\w+)\.(\w+)$")
_LOWER = re.compile(r"predicate='(\w+)'\)\s*<\s*(\d+)")
_UPPER = re.compile(r"predicate='(\w+)'\)\s*>\s*(\d+)")


def _catalog():
    return json.loads(CATALOG.read_text(encoding="utf-8"))


def _predicates():
    if not PREDICATES.is_file():
        raise SystemExit(
            "  no data/rule_predicates.json -- run `probe.py rules --extract`")
    return json.loads(PREDICATES.read_text())


def ancestors(cls: str, cat=None) -> list[str]:
    cat = cat or _catalog()
    out, c = [], cls
    while c in cat["classes"]:
        out.append(c)
        c = cat["classes"][c]["parent"]
        if c in out:
            break
    return out


def requirements_for(cls: str) -> dict:
    """Every property the schema requires of `cls`, with its bounds and range.

    Inherited requirements count: an ExposureEpisode is an Activity, so an
    Activity cardinality rule applies to it. Reading only the class's own rules
    would quietly drop those, and the first the mapper would hear of them is a
    rejection at commit.
    """
    cat = _catalog()
    if cls not in cat["classes"]:
        raise SystemExit(f"  no class {cls!r} in the catalog")
    chain = ancestors(cls, cat)

    props: dict[str, dict] = {}
    for rule in _predicates():
        m = _CARD.match(rule["label"])
        if not m:
            continue
        owner, prop = m.group(1), m.group(2)
        if owner not in chain:
            continue
        pred = rule["predicate"]
        lower = next((int(g[1]) for g in _LOWER.findall(pred) if g[0] == prop), 0)
        upper = next((int(g[1]) for g in _UPPER.findall(pred) if g[0] == prop), None)
        kind = "object" if prop in cat.get("object_properties", {}) else "data"
        entry = {
            "property": prop,
            "declared_on": owner,
            "inherited": owner != cls,
            "required": lower >= 1,
            "min": lower,
            "max": upper,
            "kind": kind,
            "range": (cat.get("object_properties", {}).get(prop) or {}).get("range")
                     if kind == "object" else
                     (cat.get("data_properties", {}).get(prop) or {}).get("range"),
            "rule": rule["label"],
        }
        props[prop] = entry

    # Constraints that are not cardinality -- value domains, cross-links,
    # orderings. These are what a mapping satisfies by accident or not at all,
    # so the mapper is shown them rather than discovering them at commit.
    extra = []
    for rule in _predicates():
        lab = rule["label"]
        if _CARD.match(lab) or lab.startswith(("DISJOINT", "RANGE", "TYPE_", "ENTITY_")):
            continue
        low = rule["predicate"].lower()
        if any(t in low for t in (f"'{cls}'", _table(cls))):
            extra.append({"rule": lab, "predicate": rule["predicate"]})

    return {
        "class": cls,
        "ancestors": chain,
        "properties": sorted(props.values(),
                             key=lambda p: (not p["required"], p["property"])),
        "constraints": extra,
    }


def _table(cls: str) -> str:
    """`ExposureEpisode` -> `exposure_episode`, the view the rules query."""
    return re.sub(r"(?<!^)(?=[A-Z])", "_", cls).lower()


def summarise(req: dict) -> str:
    """A compact, model-readable statement of the requirement."""
    lines = [f"CLASS {req['class']}  (is also: {', '.join(req['ancestors'][1:])})"]
    need = [p for p in req["properties"] if p["required"]]
    opt = [p for p in req["properties"] if not p["required"]]
    lines.append(f"\nREQUIRED properties ({len(need)}):")
    for p in need:
        bound = f"at least {p['min']}" + (f", at most {p['max']}" if p["max"] else "")
        rng = f" -> {p['range']}" if p["range"] else ""
        tag = "  [inherited]" if p["inherited"] else ""
        lines.append(f"  {p['property']}{rng}   ({bound}){tag}")
    if opt:
        lines.append(f"\nOPTIONAL properties ({len(opt)}): "
                     + ", ".join(p["property"] for p in opt))
    if req["constraints"]:
        lines.append(f"\nOTHER CONSTRAINTS on this class ({len(req['constraints'])}):")
        for c in req["constraints"][:12]:
            lines.append(f"  {c['rule']}")
    return "\n".join(lines)


if __name__ == "__main__":
    import sys
    cls = sys.argv[1] if len(sys.argv) > 1 else "ExposureEpisode"
    print(summarise(requirements_for(cls)))
