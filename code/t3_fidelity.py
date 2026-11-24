"""T3: does the batched probe predict what commit-time actually rejects?

The probe's whole claim is that one pass finds every reason a bundle would be
refused, so that a person is asked once instead of once per round trip. That
claim needs measuring against the thing it replaces, and nothing so far has
measured it. Bug 10.2 in the technical report -- 552 rejections asserting gaps
in the probe's own coverage that did not exist -- is exactly the failure this
tier is for, and it went unnoticed for a day because T3 had never been run.

Two properties, and they are not the same:

    no false positives   everything the probe predicts is real. A probe that
                         reports problems the real load does not have trains
                         its reader to ignore it.
    a superset           everything the real load rejects was predicted. A
                         probe that misses things is worse than one that is
                         slow, because "nothing to decide" then means nothing.

The asymmetry matters. Commit-time discovery reports ONE rejection per
attempt: the enforcement rules are deferred to COMMIT, so the first true one
aborts the transaction. So the real path cannot enumerate its own findings, and
"superset" can only be checked one rejection at a time -- which is precisely
why the batched probe exists. What this harness establishes per bundle is:

    the single thing commit-time reports IS in the set the probe predicted.

Run it on as many mutations as there are rejection classes and that becomes a
real superset measurement, one element at a time.

    t3_fidelity.py compare <bundle> [--json]

It takes a bundle that is ALREADY in whatever state the caller wants measured.
It does not mutate anything: the mutations live with the tests, so there is one
definition of each.

WRITES. A clean bundle will actually commit, which consumes it for the target
database. Point PGDATABASE at the scratch database, never at the one holding
the known-good load. The check below refuses the latter by name.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import probe                                                 # noqa: E402

PROTECTED_DB = "feder_materials"
VALIDATOR_ROLE = os.environ.get("MIA_VALIDATOR_ROLE", "mia_validator")

# A rule label as it appears in a rejection message, e.g.
# "materials: CARDINALITY SignalDataset.payloadProfile".
LABEL = re.compile(r"\b([A-Z][A-Z0-9_]{3,})\b")


def refuse_the_protected_database():
    """A real ingest writes. The known-good load is not a test fixture."""
    target = os.environ.get("PGDATABASE", "")
    if target == PROTECTED_DB:
        sys.exit(f"  refusing: PGDATABASE is {PROTECTED_DB}, which holds the\n"
                 f"  known-good load. This command commits. Use the scratch\n"
                 f"  database.")
    if not target:
        sys.exit("  refusing: PGDATABASE is unset, so this would commit into\n"
                 "  whatever libpq defaults to. Name the scratch database.")
    return target


def signatures(rejections: list[dict]) -> set[str]:
    """What the probe predicted, in a form a commit error can be matched to.

    Both the labels and the messages, because the two paths word the same
    finding differently: the probe reports `materials: CARDINALITY X.y` from
    its own SELECT, while commit-time raises a CheckViolation whose text wraps
    the same label in PostgreSQL's own phrasing.
    """
    out = set()
    for r in rejections:
        if r.get("rule_label"):
            out.add(r["rule_label"])
        message = r.get("message") or ""
        out.update(LABEL.findall(message))
        # The exception-shaped ones: "ValueError: No owner-approved schema ..."
        tail = message.split(": ", 1)[-1].strip()
        if tail:
            out.add(tail)
    return {s for s in out if len(s) > 3}


def attempt_commit(bundle: Path, as_owner: bool = False) -> dict:
    """Really ingest, really commit. Returns what commit-time said.

    The nesting is load-bearing and mirrors load_examples.py: `ingest_bundle`
    opens its own transaction, and the deferred constraint triggers fire when
    the OUTER one commits. Flattening it would mean the deferred rules never
    run, and this harness would report a clean commit for a bundle the real
    path refuses -- the exact false negative it exists to detect.
    """
    import psycopg
    from psycopg import sql
    from ingestion import ingest_bundle

    with psycopg.connect(autocommit=True) as conn:
        try:
            with conn.transaction():
                if not as_owner:
                    conn.execute(sql.SQL("SET LOCAL ROLE {}")
                                 .format(sql.Identifier(VALIDATOR_ROLE)))
                datasets = ingest_bundle(conn, bundle)
            return {"outcome": "committed", "datasets": len(datasets),
                    "error_type": None, "error": None}
        except Exception as exc:                             # noqa: BLE001
            return {"outcome": "rejected", "datasets": 0,
                    "error_type": type(exc).__name__,
                    "error": " ".join(str(exc).split())}


def compare(bundle: Path) -> dict:
    predicted = probe.dryrun(bundle, use_db=True)
    sigs = signatures(predicted)
    commit = attempt_commit(bundle)

    matched, match_on = None, None
    if commit["outcome"] == "rejected":
        text = commit["error"] or ""
        # Prefer an exact label hit; fall back to any predicted signature
        # appearing in the commit text.
        hits = sorted((s for s in sigs if s in text), key=len, reverse=True)
        matched = bool(hits)
        match_on = hits[0] if hits else None

    return {
        "bundle": bundle.name,
        "database": os.environ.get("PGDATABASE"),
        "predicted_count": len(predicted),
        "predicted": [{"channel": r.get("channel"),
                       "rule_label": r.get("rule_label"),
                       "message": (r.get("message") or "")[:200]}
                      for r in predicted],
        "commit": commit,
        # The two T3 properties, per bundle.
        "no_false_positive": (commit["outcome"] == "rejected"
                              if predicted else
                              commit["outcome"] == "committed"),
        "predicted_what_commit_found": matched,
        "match_on": match_on,
    }


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("compare", help="probe a bundle, then really commit it")
    c.add_argument("bundle", type=Path)
    c.add_argument("--json", action="store_true")
    a = ap.parse_args(argv[1:])

    refuse_the_protected_database()
    result = compare(a.bundle)

    if a.json:
        print(json.dumps(result, indent=1))
        return 0

    print(f"  bundle            {result['bundle']}")
    print(f"  database          {result['database']}")
    print(f"  probe predicted   {result['predicted_count']} rejection(s)")
    for r in result["predicted"][:8]:
        print(f"                      {r['channel']}: {r['message'][:72]}")
    if result["predicted_count"] > 8:
        print(f"                      … and {result['predicted_count'] - 8} more")
    c = result["commit"]
    print(f"  commit-time       {c['outcome']}"
          + (f" ({c['error_type']})" if c["error_type"] else
             f" ({c['datasets']} dataset(s))"))
    if c["error"]:
        print(f"                      {c['error'][:150]}")
    print(f"  no false positive {result['no_false_positive']}")
    print(f"  predicted it      {result['predicted_what_commit_found']}"
          + (f"  (matched on {result['match_on']!r})"
             if result["match_on"] else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
