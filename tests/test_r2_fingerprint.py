"""T4/R2: a valid bundle, refused because the environment moved.

The eighth and last of the plan's T4 mutations, and the only one where
**nothing is wrong with the data**. Every other mutation damages a bundle. This
one leaves the bundle byte-perfect and changes the room it is validated in.

The kit fingerprints the validating environment against every approval it
holds -- `sys.version`, the installed `jsonschema` version, and the bytes of
six kit files -- and `ingestion.py` raises `ValueError('Unapproved validator
implementation')` when the fingerprint does not match the one stored beside the
approved contract. That makes R2 the class where a proposal would be actively
destructive: the only correct edit to the bundle is none.

### Why a third virtual environment

The fingerprint can only be moved three ways: change Python, change the
`jsonschema` version, or change the kit's bytes. The kit is write-protected and
its digests are checked by `kit.py verify`, so that route is closed by design.
Changing `.venv-kit` in place would alter the fingerprint for BOTH databases at
once and leave every approval in each unmatched until the lock was restored --
reversible, but it perturbs state the clean database depends on, every time
this test runs.

So `.venv-r2` is built from `kit-venv.lock` with exactly one package changed:

    S=/tmp/r2 && grep -v '^jsonschema==' kit-venv.lock > $S.lock
    echo 'jsonschema==4.25.0' >> $S.lock
    uv venv --python 3.12.12 .venv-r2
    uv pip install --python .venv-r2/bin/python -r $S.lock

`.venv-kit` is never touched, and the test is repeatable. One package is enough
to move the digest, which is itself the finding worth keeping: this is what
makes dependency drift able to invalidate a database.

Nothing here writes. A probe stages and rolls back, and the mismatch stops the
seal before anything is committed.
"""
import json
import os
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
VENV_R2 = ROOT / ".venv-r2" / "bin" / "python"
BUNDLES = ROOT / "data" / "bundles"

PROBE_DB = "feder_mia_probe"
PROTECTED_DB = "feder_materials"

# Must be a bundle whose contract IS approved in the probe database, so that
# the only thing standing between it and a seal is the fingerprint.
SUBJECT = "xray_diffraction"


def pg_env() -> dict:
    env = dict(os.environ)
    env.update(PGHOST="127.0.0.1", PGPORT="8433", PGUSER="postgres",
               PGDATABASE=PROBE_DB,
               PGPASSFILE=str(ROOT / "code" / ".pgpass"))
    assert env["PGDATABASE"] != PROTECTED_DB
    return env


def ready() -> bool:
    if not KIT or not VENV_KIT.exists() or not VENV_R2.exists():
        return False
    if not (BUNDLES / SUBJECT).is_dir():
        return False
    try:
        p = subprocess.run([str(VENV_KIT), str(ROOT / "code" / "probe.py"),
                            "approvals"], capture_output=True, text=True,
                           env=pg_env(), timeout=30)
    except (OSError, subprocess.TimeoutExpired):
        return False
    return p.returncode == 0 and bool(json.loads(p.stdout or "[]"))


needs_both_venvs = pytest.mark.skipif(
    not ready(),
    reason="needs .venv-r2 as well as .venv-kit, and the probe database -- "
           "see the build command in this file's docstring")


def fingerprint(interpreter: Path) -> str:
    """What `implementation_digest()` returns in one environment."""
    code = (
        "import sys, os\n"
        "from pathlib import Path\n"
        "K = Path(os.environ['MIA_KIT_DIR']) / 'model'\n"
        "sys.path[:0] = [str(K), str(K / 'characterization')]\n"
        "from ingestion import implementation_digest\n"
        "print(implementation_digest())\n")
    p = subprocess.run([str(interpreter), "-c", code],
                       capture_output=True, text=True, env=pg_env())
    assert p.returncode == 0, f"{interpreter}: {p.stderr[-600:]}"
    return p.stdout.strip()


def approved_fingerprints() -> set[str]:
    code = (
        "import psycopg\n"
        "with psycopg.connect(autocommit=True) as c:\n"
        "    rows = c.execute('SELECT DISTINCT validator_digest "
        "FROM materials.payload_schema').fetchall()\n"
        "print('\\n'.join(r[0] for r in rows))\n")
    p = subprocess.run([str(VENV_KIT), "-c", code],
                       capture_output=True, text=True, env=pg_env())
    assert p.returncode == 0, p.stderr[-600:]
    return {line for line in p.stdout.split() if line}


def probe_from(interpreter: Path, bundle: Path) -> list[dict]:
    p = subprocess.run(
        [str(interpreter), str(ROOT / "code" / "probe.py"), "dryrun",
         str(bundle), "--db", "--json"],
        capture_output=True, text=True, env=pg_env())
    assert p.returncode in (0, 1), (
        f"the probe did not run (exit {p.returncode})\n{p.stderr[-1500:]}")
    assert p.stdout.strip(), f"no output\n{p.stderr[-1500:]}"
    return json.loads(p.stdout)


def r2_rejections(rejections: list[dict]) -> list[dict]:
    return [r for r in rejections
            if classify(r)["class"] == "R2_VALIDATOR_FINGERPRINT"]


# -------------------------------------------------------------------- tests --

@needs_both_venvs
def test_the_two_environments_really_do_differ():
    """The premise. Without this, every test below could pass for the wrong reason.

    This is the lesson from the anti-coercion test that sat green while its
    assertion never ran: a test whose setup has quietly stopped working looks
    exactly like a test that is passing.
    """
    kit, r2 = fingerprint(VENV_KIT), fingerprint(VENV_R2)
    assert kit != r2, (
        "both environments produce the same fingerprint, so nothing below is "
        "testing a mismatch. Rebuild .venv-r2 with a different jsonschema.")

    approved = approved_fingerprints()
    assert kit in approved, (
        f"the approvals in {PROBE_DB} were not made in .venv-kit, so a clean "
        f"probe from it would fail for this reason rather than the bundle's")
    assert r2 not in approved, (
        ".venv-r2's fingerprint is among the approved ones, so it is not the "
        "wrong environment at all")


@needs_both_venvs
def test_a_valid_bundle_is_refused_from_the_wrong_environment():
    """R2 itself. The bundle is untouched; only the room changed."""
    found = r2_rejections(probe_from(VENV_R2, BUNDLES / SUBJECT))
    assert found, (
        "a bundle whose contract is approved, probed in an environment the "
        "approval was not made in, raised no fingerprint rejection")
    for rejection in found:
        verdict = classify(rejection)
        assert verdict["decision_type"] == "owner_approval", (
            "a fingerprint mismatch is the owner's to resolve and the act is "
            "append-only; it is not a data fix and not a deployment tweak")


@needs_both_venvs
def test_the_same_bundle_is_clean_from_the_right_environment():
    """The control, and it is the whole point of the class.

    If this bundle were damaged, R2 would be indistinguishable from any other
    rejection. It is byte-identical in both runs: the only difference is which
    interpreter asked.
    """
    assert probe_from(VENV_KIT, BUNDLES / SUBJECT) == [], (
        f"{SUBJECT} is not clean from .venv-kit, so the R2 run above cannot "
        f"be attributed to the environment")


@needs_both_venvs
def test_no_edit_to_the_bundle_is_ever_proposed():
    """The class where a proposal would be pure damage.

    Every other mutation leaves something in the bundle that ought to change.
    Here nothing does. A tool that offered an edit would be proposing to
    corrupt correct data in order to quiet a complaint about its own
    environment -- which is the failure mode this project exists to prevent,
    in its purest form.
    """
    for rejection in r2_rejections(probe_from(VENV_R2, BUNDLES / SUBJECT)):
        verdict = classify(rejection)
        record = proposals.attach(decisions.make_record(
            rejection, verdict, BUNDLES / SUBJECT, digests={}))
        assert record["proposal"] is None, (
            f"an edit was proposed for a bundle that is correct: "
            f"{record['proposal']}")
        assert record["question"], "no question was written either"
        assert record["population_authorized"] is False
        assert record["review_required"] is True
        assert record["executable"] is False
        assert record["human"] is None


@needs_both_venvs
def test_the_reasoning_offers_restore_or_new_identity_never_re_approve():
    """Re-approving is impossible, so suggesting it would waste someone's day.

    `payload_schema.profile_id` is a PRIMARY KEY on an append-only table: the
    same profile cannot be approved twice. The only real routes are to restore
    the environment or to mint new profile identities, and the record has to
    say so, because "just approve it again" is the obvious wrong guess.
    """
    for rejection in r2_rejections(probe_from(VENV_R2, BUNDLES / SUBJECT)):
        verdict = classify(rejection)
        why = verdict["why"].lower()
        assert "restore" in why, "the record does not mention restoring"
        assert "ident" in why, "the record does not mention new identities"
        assert "re-approving the same profile is impossible" in why, (
            "the record does not say that re-approval is impossible, which is "
            "the thing a reader will otherwise try first")


@needs_both_venvs
def test_the_question_does_not_ask_for_a_value():
    """Asking "what should it be?" about a fingerprint is nonsense.

    Found while writing this test on 2026-11-24: `_question_text` was generic
    and asked for a value whatever the decision type, so R2 arrived as "The
    contract does not determine the right value here. What should it be?"
    There is no value. Nothing in the bundle should change. An operator sent
    that question either answers it wrongly or learns to skim them.
    """
    for rejection in r2_rejections(probe_from(VENV_R2, BUNDLES / SUBJECT)):
        verdict = classify(rejection)
        record = proposals.attach(decisions.make_record(
            rejection, verdict, BUNDLES / SUBJECT, digests={}))
        text = record["question"]["text"]
        assert "What should it be" not in text, (
            f"a fingerprint mismatch was presented as a missing value: {text}")
        assert "owner" in text.lower(), (
            f"the question does not say whose decision it is: {text}")
