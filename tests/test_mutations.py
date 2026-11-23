"""Break a known-good bundle on purpose, and insist the tool ASKS.

Stage 7. Everything before it proved the tool works on data that is already
correct, which is the weaker half: a checker that finds nothing is
indistinguishable from a checker that is not running. Here each test takes one
of the sixteen bundles that load cleanly, damages it in exactly one way, and
asserts the damage is reported as something a person can act on.

Two outcomes count as failures, not just the obvious one:

    "it crashed"  -- a traceback loses every rejection found before it, and
                     the operator learns nothing about the bundle.
    "it coerced"  -- a value the experimenter alone can know was supplied by
                     the tool. Where the answer is not determined by the
                     contract, the tool must ASK.

These write no decision records into the repository. `decisions` keeps its
store in module-level constants, so every test that records anything
monkeypatches them to a temporary directory; without that, running the suite
would silently add to the real ledger.
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
from taxonomy import classify, may_propose                   # noqa: E402

KIT = os.environ.get("MIA_KIT_DIR")
VENV_KIT = ROOT / ".venv-kit" / "bin" / "python"
GOOD = ROOT / "data" / "bundles" / "xray_diffraction"

needs_probe = pytest.mark.skipif(
    not KIT or not VENV_KIT.exists() or not GOOD.is_dir(),
    reason="needs MIA_KIT_DIR, .venv-kit and the copied bundles")


# ------------------------------------------------------------- the mutations --
# Each takes a bundle directory and damages it in one way. The name is what
# the failure message will say, so it is written to be read.

def remove_a_pinned_unit(bundle: Path):
    """A required unit key is gone. The contract pins it, so it is mechanical."""
    p = bundle / "payload.json"
    doc = json.loads(p.read_text())
    doc["units"].pop("two_theta_deg")
    p.write_text(json.dumps(doc, indent=2))


def manifest_escapes_the_bundle(bundle: Path):
    """The manifest points outside its own directory."""
    p = bundle / "manifest.json"
    doc = json.loads(p.read_text())
    doc[next(iter(doc))] = "../payload.json"
    p.write_text(json.dumps(doc, indent=2))


def declare_the_wrong_family(bundle: Path):
    """The payload claims a family whose contract it does not satisfy.

    This is the mutation that used to take the probe down: the kit's semantic
    rules are indexed by family and raise a KeyError when the document is not
    the branch they expect.
    """
    p = bundle / "payload.json"
    doc = json.loads(p.read_text())
    doc["family"] = "thermal"
    p.write_text(json.dumps(doc, indent=2))


def remove_the_payload_profile_triples(bundle: Path):
    """Datasets lose the triple naming the contract they are validated against."""
    p = bundle / "records.ttl"
    kept = [ln for ln in p.read_text().splitlines()
            if not (ln.strip().startswith("fm:payloadProfile ex:")
                    and ln.strip().endswith(";"))]
    p.write_text("\n".join(kept) + "\n")


# ------------------------------------------------------------------ helpers --

def mutate(tmp_path: Path, mutation) -> Path:
    bundle = tmp_path / "bundle"
    shutil.copytree(GOOD, bundle)
    mutation(bundle)
    return bundle


def probe(bundle: Path) -> list[dict]:
    """Run the probe exactly as mia.py does, across the venv boundary."""
    proc = subprocess.run(
        [str(VENV_KIT), str(ROOT / "code" / "probe.py"), "dryrun",
         str(bundle), "--json"],
        capture_output=True, text=True)
    assert proc.returncode in (0, 1), (
        f"the probe did not run (exit {proc.returncode}). A crash loses every "
        f"rejection found before it.\n{proc.stderr[-1500:]}")
    assert proc.stdout.strip(), (
        f"the probe produced no output, which is not the same as a clean "
        f"bundle.\n{proc.stderr[-1500:]}")
    return json.loads(proc.stdout)


def records_for(rejections, bundle, limit=200):
    """Decision records, without touching the repository's store."""
    out = []
    for rejection in rejections[:limit]:
        verdict = classify(rejection)
        out.append(decisions.make_record(rejection, verdict, bundle,
                                         digests={}))
    return out


# -------------------------------------------------------------------- tests --

@needs_probe
def test_the_unmutated_bundle_is_clean():
    """The baseline. Without this, every test below could pass vacuously."""
    assert probe(GOOD) == [], (
        "a bundle the kit's own verification accepts was rejected by our "
        "probe. The fault is ours, not the data's.")


@needs_probe
@pytest.mark.parametrize("mutation,expected_class", [
    (remove_a_pinned_unit,          "R3_PAYLOAD_CONTRACT"),
    (manifest_escapes_the_bundle,   "R7_ENVIRONMENT"),
    (declare_the_wrong_family,      "R3_PAYLOAD_CONTRACT"),
], ids=lambda v: getattr(v, "__name__", v))
def test_a_mutation_is_reported_and_classified(tmp_path, mutation,
                                               expected_class):
    rejections = probe(mutate(tmp_path, mutation))
    assert rejections, "the damage was not reported at all"
    classes = {classify(r)["class"] for r in rejections}
    assert expected_class in classes, (
        f"expected {expected_class}, got {sorted(classes)}")
    assert "UNCLASSIFIED" not in classes, (
        "a rejection fell through the taxonomy; the agent has stopped "
        "understanding part of the schema")


@needs_probe
def test_the_wrong_family_does_not_take_the_probe_down(tmp_path):
    """Regression. The kit raises here; the probe must record, not propagate.

    Found by this suite on 2026-11-23: `semantic_errors` is indexed by family
    and raised KeyError('feature_id') on a payload declaring a family it does
    not match. The probe exited non-zero with a traceback, so mia.py reported
    "the probe did not run" and nothing was learned about the bundle.
    """
    rejections = probe(mutate(tmp_path, declare_the_wrong_family))
    assert rejections, "the family mismatch produced no rejection"
    # If the kit raises again, it must arrive on the channel that says so.
    for r in rejections:
        if r["channel"] == "probe_error":
            verdict = classify(r)
            assert verdict["decision_type"] == "escalate", (
                "a probe failure means the rejection list is incomplete; it "
                "must never be presented as a routine data fix")


@needs_probe
def test_a_wrong_identity_is_reported_once_not_once_per_row(tmp_path):
    """A mislabelled payload is ONE finding, not one per row.

    `family`, `technique` and `contract_version` are const in every payload
    contract: they say which contract the document claims to be an instance
    of. When they disagree, checking the body against that contract describes
    nothing, because the body was never meant to satisfy it.

    Measured on 2026-11-23, before this was handled: a single wrong `family`
    produced **21,026** rejections, and `mia.py probe` would have written
    21,026 decision records for one mistake. The fact explaining all of them
    was somewhere in the middle of the list.

    Nothing is lost by stopping at the mismatch. Once the identity is settled
    the next probe checks the body against the contract that applies, and
    those findings survive instead of being deleted by the correction.
    """
    rejections = probe(mutate(tmp_path, declare_the_wrong_family))
    assert 1 <= len(rejections) <= len(("contract_version", "family",
                                        "technique")), (
        f"a single mislabelled payload produced {len(rejections)} rejections; "
        f"it should report only the identity fields that disagree")
    assert all(r["channel"] == "jsonschema" for r in rejections)
    assert all(r.get("json_pointer", "").lstrip("/") in
               ("contract_version", "family", "technique")
               for r in rejections), (
        "the body was validated against a contract the document is not an "
        "instance of")


@needs_probe
def test_a_value_only_the_experimenter_knows_is_never_proposed(tmp_path):
    """The anti-coercion rule, stated as an invariant over every mutation."""
    for mutation in (remove_a_pinned_unit, manifest_escapes_the_bundle,
                     declare_the_wrong_family):
        bundle = mutate(tmp_path / mutation.__name__, mutation)
        for rejection in probe(bundle):
            verdict = classify(rejection)
            if verdict["confidence"] == "unknown":
                assert not may_propose(verdict), (
                    f"{mutation.__name__}: the tool offered to supply a value "
                    f"it cannot know -- {rejection.get('message','')[:80]}")


@needs_probe
def test_no_mutation_can_authorise_itself(tmp_path, monkeypatch):
    """T6, containment. The guarantee we have put in writing to the schema's
    author: the tool holds nothing that can mark its own work approved."""
    monkeypatch.setattr(decisions, "DECISIONS", tmp_path / "decisions")
    monkeypatch.setattr(decisions, "LEDGER", tmp_path / "ledger.json")
    for mutation in (remove_a_pinned_unit, manifest_escapes_the_bundle,
                     declare_the_wrong_family):
        bundle = mutate(tmp_path / mutation.__name__, mutation)
        for record in records_for(probe(bundle), bundle):
            assert record["executable"] is False
            assert record["population_authorized"] is False
            assert record["review_required"] is True
            assert record["human"] is None
            assert record["status"] == "tentative_review_required"


@needs_probe
def test_an_answered_question_is_not_asked_again(tmp_path, monkeypatch):
    """T5, resume. The same damage, probed twice, must not ask twice."""
    monkeypatch.setattr(decisions, "DECISIONS", tmp_path / "decisions")
    monkeypatch.setattr(decisions, "LEDGER", tmp_path / "ledger.json")
    bundle = mutate(tmp_path, remove_a_pinned_unit)
    rejections = probe(bundle)

    first = records_for(rejections, bundle)
    for record in first:
        decisions.save(record)
    decisions.answer(first[0]["id"], "answered", text="degree, as delivered",
                     by="test")

    answered = decisions.answered_logical_ids()
    assert first[0]["logical_id"] in answered

    # A second probe of the same damage produces the same logical identity,
    # which is what lets the loop resume instead of re-asking.
    again = records_for(probe(bundle), bundle)
    assert {r["logical_id"] for r in again} == {r["logical_id"] for r in first}
    fresh = [r for r in again if r["logical_id"] not in answered]
    assert len(fresh) == len(first) - 1, (
        "an already-answered question came back as new work")


@needs_probe
def test_removing_the_contract_pointer_is_invisible_offline(tmp_path):
    """A KNOWN GAP, pinned so that closing it fails here and gets noticed.

    Dropping the `payloadProfile` triples leaves the datasets with no contract
    to validate against, and the offline pass reports nothing at all. The rule
    that catches it lives in SQL, so today it is discoverable only with --db.
    An offline "nothing to decide" on this bundle is therefore not a clean
    bill of health, and the probe already says so in its own output.
    """
    rejections = probe(mutate(tmp_path, remove_the_payload_profile_triples))
    assert rejections == [], (
        "the offline pass now catches a missing payloadProfile. That is an "
        "improvement -- delete this test and cover it as a normal mutation.")
