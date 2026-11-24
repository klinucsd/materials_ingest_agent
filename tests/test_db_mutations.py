"""The mutations that only a database can catch.

Stage 7, the half that was blocked. `dryrun --db` stages a bundle's rows,
seals the payloads, evaluates all 558 rule predicates and rolls back -- so two
of the plan's T4 mutations live here and nowhere else:

    an unapproved profile     the contract lookup is a table, so this is
                              INVISIBLE offline. Verified below.
    re-ingesting a committed  the collision is in a primary key, and nothing
    bundle                    offline knows what has already been committed.

Neither could be covered while the only database held the known-good load:
probing an already-loaded bundle reports the load, not the mutation. There are
now two databases in the one container, and these tests use the disposable one.

    feder_materials   the known-good T2 load. Never touched from here.
    feder_mia_probe   approvals, and whatever a test leaves behind.

The guard below is not ceremony. `pgenv.sh` exports PGDATABASE=feder_materials,
so anything that forgets to override it lands in the database that has to stay
clean -- and these tests WRITE, unlike the offline suite.
"""
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
import decisions                                             # noqa: E402
import proposals                                             # noqa: E402
from taxonomy import classify, may_propose                   # noqa: E402

KIT = os.environ.get("MIA_KIT_DIR")
VENV_KIT = ROOT / ".venv-kit" / "bin" / "python"
BUNDLES = ROOT / "data" / "bundles"

PROBE_DB = "feder_mia_probe"
PROTECTED_DB = "feder_materials"

# A bundle these tests load into the probe database, and one they leave alone.
LOADED = "thermal"
UNLOADED = "xray_diffraction"


def pg_env(database: str = PROBE_DB) -> dict:
    """libpq settings for the probe database, never inherited from the shell."""
    assert database != PROTECTED_DB, (
        f"refusing to point a writing test at {PROTECTED_DB}")
    env = dict(os.environ)
    env.update(PGHOST="127.0.0.1", PGPORT="8433", PGUSER="postgres",
               PGDATABASE=database,
               PGPASSFILE=str(ROOT / "code" / ".pgpass"))
    return env


def db_reachable() -> bool:
    if not KIT or not VENV_KIT.exists() or not BUNDLES.is_dir():
        return False
    try:
        p = subprocess.run([str(VENV_KIT), str(ROOT / "code" / "probe.py"),
                            "approvals"], capture_output=True, text=True,
                           env=pg_env(), timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and bool(json.loads(p.stdout or "[]"))


needs_db = pytest.mark.skipif(
    not db_reachable(),
    reason=f"needs {PROBE_DB} with the kit installed and the examples "
           f"approved -- see code/approve_probe_db.sh")


def probe(bundle: Path, use_db: bool) -> list[dict]:
    cmd = [str(VENV_KIT), str(ROOT / "code" / "probe.py"), "dryrun",
           str(bundle), "--json"]
    if use_db:
        cmd.append("--db")
    p = subprocess.run(cmd, capture_output=True, text=True, env=pg_env())
    assert p.returncode in (0, 1), (
        f"the probe did not run (exit {p.returncode})\n{p.stderr[-1500:]}")
    assert p.stdout.strip(), (
        f"no output, which is not the same as a clean bundle\n"
        f"{p.stderr[-1500:]}")
    return json.loads(p.stdout)


def ensure_loaded(family: str):
    """Leave `family` committed in the probe database.

    Run unconditionally and ignore the exit status: if a previous run already
    loaded it, the loader fails on exactly the collision these tests are
    about, and that is the state we want. What proves it loaded is the probe
    result, asserted by the caller -- not this process's return code.
    """
    subprocess.run([str(VENV_KIT), str(ROOT / "code" / "load_examples.py"),
                    "--family", family],
                   capture_output=True, text=True, env=pg_env())


# -------------------------------------------------------------------- tests --

@needs_db
def test_the_tests_are_not_pointed_at_the_known_good_database():
    """The guard, as a test, because everything below writes."""
    assert pg_env()["PGDATABASE"] == PROBE_DB
    with pytest.raises(AssertionError):
        pg_env(PROTECTED_DB)


@needs_db
def test_an_unapproved_profile_is_invisible_offline_and_caught_with_db(
        tmp_path):
    """T4/R1. The contract lookup is a table, so only --db can see this.

    The bundle names a `payloadProfile` for which no owner has approved a
    contract. Offline the graph is perfectly well-formed -- which is the point
    worth stating plainly: an offline "nothing to decide" is not a clean bill
    of health, and the probe says so in its own output.
    """
    bundle = tmp_path / "bundle"
    shutil.copytree(BUNDLES / LOADED, bundle)
    ttl = bundle / "records.ttl"
    ttl.write_text(ttl.read_text().replace(
        f"ex:{LOADED}_representation", f"ex:{LOADED}_representation_unapproved"))

    assert probe(bundle, use_db=False) == [], (
        "an unapproved profile is now visible offline. That is an improvement "
        "-- cover it in the offline suite and simplify this test.")

    rejections = probe(bundle, use_db=True)
    assert rejections, "an unapproved profile was not caught even with --db"
    verdicts = [classify(r) for r in rejections]
    assert any(v["class"] == "R1_CONTRACT_NOT_APPROVED" for v in verdicts), (
        f"expected R1, got {sorted({v['class'] for v in verdicts})}")

    for rejection, verdict in zip(rejections, verdicts):
        if verdict["class"] != "R1_CONTRACT_NOT_APPROVED":
            continue
        assert verdict["decision_type"] == "owner_approval", (
            "enrolling a contract is the owner's act, and append-only; it "
            "must never be presented as a data fix")
        record = proposals.attach(
            decisions.make_record(rejection, verdict, bundle, digests={}))
        # Whatever it writes, it may not claim the authority to approve.
        assert record["population_authorized"] is False
        assert record["review_required"] is True
        assert record["human"] is None


@needs_db
def test_re_ingesting_a_committed_bundle_is_sealed_not_a_data_fix():
    """T4/R6, and the regression for a real misclassification.

    `admit` raises UniqueViolation on a bundle already committed, and reports
    on the `seal_failed` channel. The taxonomy's duplicate-key matcher was
    keyed to `sql_constraint`, which that path never uses -- so until
    2026-11-23 this landed in the catch-all at the end of R3 and came out as
    R3_PAYLOAD_CONTRACT / **data_fix**.

    That is the one answer this condition must never get. Sealed records are
    append-only; calling it a data fix invites an operator to edit a bundle
    whose records cannot be edited. The correction is a NEW identity.
    """
    ensure_loaded(LOADED)
    rejections = probe(BUNDLES / LOADED, use_db=True)
    assert rejections, (
        f"{LOADED} is not committed in {PROBE_DB}, so there was nothing to "
        f"collide with; the loader did not leave it behind")

    verdicts = [classify(r) for r in rejections]
    assert all(v["class"] == "R6_SEALED" for v in verdicts), (
        f"expected every rejection to be R6_SEALED, got "
        f"{sorted({v['class'] for v in verdicts})}")
    assert all(v["decision_type"] == "escalate" for v in verdicts), (
        "an already-sealed record was offered as something to correct")
    assert not any(v["decision_type"] == "data_fix" for v in verdicts)


@needs_db
def test_an_aborted_transaction_is_never_reported_as_missing_coverage():
    """The regression for 552 false findings, and why it mattered.

    A failed seal aborts the Postgres transaction, so every predicate
    evaluated afterwards raised InFailedSqlTransaction. Each was recorded on
    the `probe_unsupported` channel, which means "this rule could not be
    evaluated outside its trigger, so it remains discoverable only at commit".
    That was false twice over: the rules were evaluable, and the probe was
    reporting gaps in its own coverage that did not exist.

    Measured 2026-11-23 on an already-committed bundle: 554 rejections, of
    which 552 were InFailedSqlTransaction. After a SAVEPOINT per seal and per
    predicate, 3.

    `probe_unsupported` is how the agent learns its list is incomplete. A
    channel that cries wolf 552 times is worse than one that says nothing.
    """
    ensure_loaded(LOADED)
    rejections = probe(BUNDLES / LOADED, use_db=True)

    for rejection in rejections:
        assert "InFailedSqlTransaction" not in rejection.get("message", ""), (
            "a poisoned transaction is being reported as a finding about the "
            "data")
        if rejection["channel"] == "probe_unsupported":
            pytest.fail(
                f"a rule was reported as unevaluable during a seal failure: "
                f"{rejection['message'][:120]}")

    assert len(rejections) <= 10, (
        f"{len(rejections)} rejections for one already-committed bundle. One "
        f"cause should not produce a list nobody will read -- the same "
        f"failure as the 21,026-row identity bug.")


@needs_db
def test_a_clean_bundle_is_clean_against_the_database_too():
    """The baseline, and the thing that was impossible before two databases.

    Every bundle probes clean offline. Until the probe database existed, none
    could be probed with --db without colliding with the known-good load, so
    the DB-backed pass had no baseline at all and a rejection from it could
    not be told from the load talking.
    """
    assert probe(BUNDLES / UNLOADED, use_db=True) == [], (
        f"{UNLOADED} is not committed in {PROBE_DB} and its contract is "
        f"approved there, so the DB-backed pass should find nothing")


@needs_db
def test_removing_the_contract_pointer_is_caught_with_db(tmp_path):
    """T4/R4, and the other half of a gap the offline suite pins.

    `test_removing_the_contract_pointer_is_invisible_offline` in
    tests/test_mutations.py asserts that dropping the `payloadProfile` triples
    produces NOTHING offline. That was true when written and is still true:
    the rules that catch it are SQL CARDINALITY rules. That test records the
    gap; this one closes it, which only became possible once there was a
    database to probe against that does not already hold the known-good load.

    The two are a pair. If the offline one ever starts finding this, update
    both rather than deleting either.
    """
    bundle = tmp_path / "bundle"
    shutil.copytree(BUNDLES / UNLOADED, bundle)
    ttl = bundle / "records.ttl"
    ttl.write_text("\n".join(
        line for line in ttl.read_text().splitlines()
        if not (line.strip().startswith("fm:payloadProfile ex:")
                and line.strip().endswith(";"))) + "\n")

    assert probe(bundle, use_db=False) == [], (
        "the offline pass now catches a missing payloadProfile -- that is an "
        "improvement, but the offline test pins it as a gap, so update both")

    verdicts = [classify(r) for r in probe(bundle, use_db=True)]
    assert verdicts, "a missing payloadProfile was not caught even with --db"
    classes = {v["class"] for v in verdicts}
    assert "R4_GRAPH_SHAPE" in classes, (
        f"expected the CARDINALITY rules to fire; got {sorted(classes)}")
    assert "UNCLASSIFIED" not in classes, (
        "a rejection fell through the taxonomy")
