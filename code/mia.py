"""Materials ingest agent — the operator's command line.

Today this is the HUMAN half only: probe a bundle, turn what comes back into
decision records, read them, answer them. The agent that writes the questions
and proposals comes next, and it is built after this on purpose — if the
automated part were removed tomorrow, what is here would still be a working,
auditable way to find out why a bundle will not load.

    mia.py probe   <bundle> [--db]     find every rejection, record each one
    mia.py review  [--bundle X] [--all]
    mia.py answer  <id> --decision approved|answered|amended|rejected|deferred
                        [--answer TEXT] [--amend FILE]
    mia.py status

WHAT THIS CANNOT DO, BY CONSTRUCTION. It cannot approve a contract: there is
no code path here that writes `materials.payload_schema`, and the owner
approval lives in bootstrap.sh, which a person runs. It cannot mark its own
work authorised: `answer` is the only writer of the `human` block, and even it
leaves the five review flags exactly as they were.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

import decisions                                            # noqa: E402
from taxonomy import classify, may_propose                  # noqa: E402

ROOT = HERE.parent


def _probe(bundle: Path, use_db: bool):
    """Run the probe in its own interpreter.

    probe.py imports the collaborator's kit, whose dependency set is pinned
    because the schema fingerprints it against every approval. Keeping it
    behind a subprocess boundary means nothing this process imports can ever
    perturb that, even by accident.
    """
    import subprocess
    venv = ROOT / ".venv-kit" / "bin" / "python"
    python = str(venv) if venv.exists() else sys.executable
    cmd = [python, str(HERE / "probe.py"), "dryrun", str(bundle), "--json"]
    if use_db:
        cmd.append("--db")
    proc = subprocess.run(cmd, capture_output=True, text=True)
    # An empty stdout is the probe NOT RUNNING, never "it found nothing".
    # `json.loads(proc.stdout or "[]")` treated a crashed probe as a clean
    # bundle and printed "nothing to decide" -- a failure wearing the clothes
    # of a pass, which is the whole thing this tool exists to prevent.
    if proc.returncode not in (0, 1) or not (proc.stdout or "").strip():
        detail = (proc.stderr or proc.stdout or "").strip()
        print(detail[-1800:], file=sys.stderr)
        raise SystemExit(
            f"\n  the probe did not run (exit {proc.returncode}). Nothing was "
            f"checked, so nothing can be concluded about this bundle.")
    try:
        return json.loads(proc.stdout)
    except json.JSONDecodeError:
        print(proc.stdout[-1800:], file=sys.stderr)
        raise SystemExit("  the probe produced output that is not JSON")


def cmd_probe(a):
    bundle = Path(a.bundle).resolve()
    if not bundle.is_dir():
        print(f"  no such bundle: {bundle}")
        return 2
    rejections = _probe(bundle, a.db)
    if not rejections:
        print(f"  {bundle.name}: nothing to decide"
              + ("" if a.db else "   (offline; SQL rules not evaluated)"))
        return 0

    already = decisions.answered_logical_ids()
    digests = decisions.bundle_digests(bundle)
    fresh = answered = 0
    for rejection in rejections:
        verdict = classify(rejection)
        record = decisions.make_record(rejection, verdict, bundle,
                                       digests=digests)
        if record["logical_id"] in already:
            answered += 1
            continue
        if decisions.load(record["id"]) is not None:
            continue
        decisions.save(record)
        fresh += 1

    print(f"  {bundle.name}: {len(rejections)} rejection(s)")
    print(f"    {fresh} new decision record(s)")
    if answered:
        print(f"    {answered} already answered, not re-asked")
    if fresh:
        print(f"\n  Read them:  mia.py review --bundle {bundle.name}")
    return 1 if fresh else 0


def _group(records):
    out = {}
    for r in records:
        out.setdefault(r.get("decision_type") or "unclassified", []).append(r)
    return out


def cmd_review(a):
    records = decisions.all_records()
    if a.bundle:
        records = [r for r in records if r.get("bundle") == a.bundle]
    if not a.all:
        records = [r for r in records if not r.get("human")]
    if not records:
        print("  nothing waiting" if not a.all else "  no decision records")
        return 0

    # Most consequential first: a contract that does not exist blocks
    # everything behind it, and a question costs a person more than a proposal.
    order = ["owner_approval", "contract_authoring", "escalate", "data_fix"]
    grouped = _group(records)
    print(f"  {len(records)} record(s)\n")
    for kind in order + [k for k in grouped if k not in order]:
        rows = grouped.get(kind)
        if not rows:
            continue
        print(f"  == {kind}  ({len(rows)})")
        for r in rows:
            mark = "?" if r.get("question") else ("+" if r.get("proposal") else " ")
            if r.get("human"):
                mark = "."
            msg = (r["rejection"].get("message") or "")[:64]
            print(f"   {mark} {r['id'][:22]}  {r.get('confidence',''):<10} {msg}")
            if a.verbose:
                print(f"       class     {r.get('class')}")
                print(f"       why       {(r.get('why_classified') or '')[:110]}")
                if r.get("question"):
                    print(f"       QUESTION  {r['question'].get('text','')[:110]}")
                if r.get("proposal"):
                    p = r["proposal"]
                    print(f"       PROPOSAL  {p.get('action')}: "
                          f"{str(p.get('diff') or p.get('command'))[:94]}")
                if r.get("human"):
                    h = r["human"]
                    print(f"       ANSWERED  {h['decision']} by {h['by']} "
                          f"{h['at_utc']}")
        print()
    if not a.verbose:
        print("  mia.py review -v   for the question, proposal and reasoning")
    print("  key:  ? question    + proposal    . answered")
    return 0


def cmd_answer(a):
    amend = None
    if a.amend:
        amend = json.loads(Path(a.amend).read_text())
    try:
        record = decisions.answer(a.id, a.decision, text=a.answer,
                                  amend=amend, by=a.by)
    except ValueError as exc:
        print(f"  {exc}")
        return 2
    print(f"  {record['id']}  ->  {a.decision}")
    if record.get("decision_type") == "owner_approval":
        cmd = (record.get("proposal") or {}).get("command")
        print()
        print("  This one needs an OWNER action, which this tool does not take.")
        if cmd:
            print(f"  Run it yourself:\n      {cmd}")
        print("  The next probe reads the database to confirm it happened --")
        print("  not this record, and not the agent's memory of asking.")
    return 0


def cmd_status(a):
    records = decisions.all_records()
    waiting = [r for r in records if not r.get("human")]
    done = [r for r in records if r.get("human")]
    print(f"  decision records   {len(records)}")
    print(f"    waiting          {len(waiting)}")
    print(f"    answered         {len(done)}")
    if waiting:
        by = {}
        for r in waiting:
            by[r.get("decision_type")] = by.get(r.get("decision_type"), 0) + 1
        for k, v in sorted(by.items(), key=lambda kv: -kv[1]):
            print(f"      {k:<20} {v}")
    stale = [r for r in done if not decisions.honours_signature(r)]
    if stale:
        print(f"\n  {len(stale)} answered record(s) whose question has since "
              f"changed:")
        for r in stale[:5]:
            print(f"    {r['id'][:22]}  the answer may no longer apply")
    # The invariant worth checking every time, cheaply.
    broken = [r for r in records
              if r.get("executable") or r.get("population_authorized")
              or not r.get("review_required")]
    print(f"\n  review flags intact on all {len(records)} record(s)"
          if not broken else
          f"\n  !! {len(broken)} record(s) have had their review flags changed")
    return 1 if broken else 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)

    p = sub.add_parser("probe", help="find rejections and record each one")
    p.add_argument("bundle")
    p.add_argument("--db", action="store_true",
                   help="also evaluate the SQL rules against staged rows")

    r = sub.add_parser("review", help="read what is waiting")
    r.add_argument("--bundle")
    r.add_argument("--all", action="store_true", help="include answered")
    r.add_argument("-v", "--verbose", action="store_true")

    w = sub.add_parser("answer", help="record a decision")
    w.add_argument("id")
    w.add_argument("--decision", required=True,
                   choices=sorted(decisions.HUMAN_DECISIONS))
    w.add_argument("--answer", help="the fact, or why not")
    w.add_argument("--amend", help="a JSON file holding a corrected proposal")
    w.add_argument("--by")

    sub.add_parser("status", help="what is waiting, and are the flags intact")

    a = ap.parse_args(argv[1:])
    return {"probe": cmd_probe, "review": cmd_review,
            "answer": cmd_answer, "status": cmd_status}[a.command](a)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
