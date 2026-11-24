"""T3: the probe's prediction measured against what commit-time really does.

The probe's whole claim is that one pass finds every reason a bundle would be
refused, so a person is asked once instead of once per round trip. Nothing
measured that claim until 2026-11-24, and bug 10.2 is what the gap cost: 552
rejections asserting gaps in the probe's own coverage that did not exist, found
only because a mutation happened to surface them. This file is the tier that
should have caught it.

Two properties, and they are not the same thing:

    no false positives   everything the probe predicts is real. A probe that
                         reports problems the real load does not have trains
                         its reader to ignore it.
    a superset           everything the real load rejects was predicted. A
                         probe that misses things is worse than one that is
                         slow, because "nothing to decide" then means nothing.

### Why the superset half is checked one element at a time

Commit-time discovery reports ONE rejection per attempt -- the enforcement
rules are deferred to COMMIT, so the first true one aborts the transaction.
The real path cannot enumerate its own findings. That is precisely why the
batched probe exists, and it means "superset" can only be established per
bundle as: *the single thing commit-time reported was in the set the probe
predicted*. Run over one mutation per rejection class, as here, that becomes a
real measurement, one element at a time.

### What this file does NOT cover, and where that half lives

The clean case -- probe predicts nothing, the real load succeeds -- is not here
because a successful commit WRITES, so a test asserting it would consume a
bundle on every run and could pass only once per bundle per database. That half
is already covered: T1 asserts all sixteen bundles probe clean with `--db`, and
T2 asserts all sixteen then commit, reaching the 16 blobs and 48 bindings the
kit's own verification asserts. Between them, sixteen clean bundles predicted
nothing and sixteen clean bundles committed.

Every case below is expected to be REJECTED, and a rejected ingest rolls back,
so this file writes nothing and is safe to run repeatedly.
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
sys.path.insert(0, str(Path(__file__).resolve().parent))

from taxonomy import classify                                # noqa: E402

# The mutations are defined once, with the offline suite, and reused here.
# Defining them twice is how two suites quietly stop testing the same thing.
from test_mutations import (                                 # noqa: E402
    data_origin_not_permitted,
    declare_the_wrong_family,
    declare_the_wrong_family_in_the_embedded_json,
    manifest_escapes_the_bundle,
    negative_peak_width,
    remove_a_pinned_unit,
    remove_the_payload_profile_triples,
    unit_swapped_for_another,
)

KIT = os.environ.get("MIA_KIT_DIR")
VENV_KIT = ROOT / ".venv-kit" / "bin" / "python"
HARNESS = ROOT / "code" / "t3_fidelity.py"
BUNDLES = ROOT / "data" / "bundles"

PROBE_DB = "feder_mia_probe"
PROTECTED_DB = "feder_materials"

# Every mutation here is written against this bundle, and it must stay
# uncommitted in the probe database for these cases to be about the mutation
# rather than about the load.
SUBJECT = "xray_diffraction"


def pg_env(database: str = PROBE_DB) -> dict:
    assert database != PROTECTED_DB, (
        f"refusing to point a committing test at {PROTECTED_DB}")
    env = dict(os.environ)
    env.update(PGHOST="127.0.0.1", PGPORT="8433", PGUSER="postgres",
               PGDATABASE=database,
               PGPASSFILE=str(ROOT / "code" / ".pgpass"))
    return env


def db_reachable() -> bool:
    if not KIT or not VENV_KIT.exists() or not (BUNDLES / SUBJECT).is_dir():
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


def mutated(tmp_path: Path, mutation) -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(BUNDLES / SUBJECT, bundle)
    # copytree preserves the kit's read-only bits and a mutation writes.
    for p in [bundle, *bundle.rglob("*")]:
        p.chmod(p.stat().st_mode | 0o200)
    mutation(bundle)
    return bundle


def compare(bundle: Path, database: str = PROBE_DB) -> dict:
    p = subprocess.run(
        [str(VENV_KIT), str(HARNESS), "compare", str(bundle), "--json"],
        capture_output=True, text=True, env=pg_env(database))
    assert p.returncode == 0 and p.stdout.strip(), (
        f"the T3 harness did not run (exit {p.returncode})\n"
        f"{p.stderr[-1500:]}")
    return json.loads(p.stdout)


MUTATIONS = [
    remove_a_pinned_unit,
    negative_peak_width,
    unit_swapped_for_another,
    data_origin_not_permitted,
    declare_the_wrong_family,
    manifest_escapes_the_bundle,
    remove_the_payload_profile_triples,
    declare_the_wrong_family_in_the_embedded_json,
]


# -------------------------------------------------------------------- tests --

@needs_db
@pytest.mark.parametrize("mutation", MUTATIONS,
                         ids=lambda v: getattr(v, "__name__", v))
def test_the_probe_predicted_what_commit_time_rejected(tmp_path, mutation):
    """Both T3 properties, per mutation.

    `no_false_positive`: the probe predicted at least one problem and the real
    load really did refuse the bundle. A probe that predicts a problem the real
    path does not have is the failure that makes a reader stop reading.

    `predicted_what_commit_found`: the specific thing commit-time reported was
    in the predicted set. This is the superset property, one element at a time
    -- see the module docstring for why it cannot be checked all at once.
    """
    result = compare(mutated(tmp_path, mutation))

    assert result["predicted_count"] >= 1, (
        "the probe predicted nothing for a bundle the real load refuses. "
        "An offline 'nothing to decide' would have been read as clean.")
    assert result["commit"]["outcome"] == "rejected", (
        f"the probe predicted {result['predicted_count']} problem(s) and the "
        f"real load committed anyway — every prediction was a false positive")
    assert result["predicted_what_commit_found"] is True, (
        f"commit-time rejected on something the probe did not predict:\n"
        f"  commit: {result['commit']['error'][:300]}\n"
        f"  predicted: "
        f"{[r['message'][:70] for r in result['predicted']]}")


@needs_db
@pytest.mark.parametrize("mutation", MUTATIONS,
                         ids=lambda v: getattr(v, "__name__", v))
def test_one_defect_is_not_reported_twice(tmp_path, mutation):
    """Every predicted rejection is a distinct finding, not a restatement.

    Measured 2026-11-24, before `drop_restatements`: `admit` re-runs the
    payload validation, so a schema error was reported once by our own
    `iter_errors` pass and again as the exception that stopped the seal --
    different channel, longer text, different `logical_id`, two decision
    records, a person asked twice about one problem. Separately, a rule checked
    offline in stage 5 was checked again by the SQL probe in stage 7, so each
    CHAR_JSON_* rule arrived once per path.

    Neither is a false positive in the T3 sense -- nothing reported was
    fictional -- which is exactly why the tier above would not catch it. This
    is the 21,026-row and 552-rule failure in miniature: one cause, several
    findings.
    """
    result = compare(mutated(tmp_path, mutation))
    predicted = result["predicted"]

    labels = [r["rule_label"] for r in predicted if r["rule_label"]]
    assert len(labels) == len(set(labels)), (
        f"the same rule label was reported more than once: "
        f"{sorted(labels)}")

    # A seal failure must say something the earlier stages did not.
    others = [r["message"] for r in predicted
              if r["channel"] != "seal_failed" and len(r["message"]) >= 15]
    for r in predicted:
        if r["channel"] != "seal_failed":
            continue
        for earlier in others:
            assert earlier not in r["message"], (
                f"a seal failure restates a defect already reported:\n"
                f"  earlier: {earlier[:100]}\n"
                f"  seal:    {r['message'][:100]}")


@needs_db
@pytest.mark.parametrize("mutation", MUTATIONS,
                         ids=lambda v: getattr(v, "__name__", v))
def test_nothing_predicted_falls_through_the_taxonomy(tmp_path, mutation):
    """A predicted rejection nobody can classify cannot be acted on.

    T3 measures whether the predictions are true. This asserts they are also
    usable: an UNCLASSIFIED rejection reaches a person with no decision type
    and no owner, which is a finding the agent has stopped understanding.
    """
    for r in compare(mutated(tmp_path, mutation))["predicted"]:
        verdict = classify(r)
        assert verdict["class"] != "UNCLASSIFIED", (
            f"unclassified: {r['channel']}: {r['message'][:120]}")


@needs_db
def test_the_harness_refuses_the_known_good_database():
    """It commits. The database holding the known-good load is not a fixture."""
    p = subprocess.run(
        [str(VENV_KIT), str(HARNESS), "compare", str(BUNDLES / SUBJECT),
         "--json"],
        capture_output=True, text=True,
        env={**os.environ, "PGDATABASE": PROTECTED_DB,
             "PGPASSFILE": str(ROOT / "code" / ".pgpass"),
             "PGHOST": "127.0.0.1", "PGPORT": "8433", "PGUSER": "postgres"})
    assert p.returncode != 0, (
        "the T3 harness agreed to commit into the known-good load")
    assert PROTECTED_DB in (p.stdout + p.stderr)
