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

The mutations come in two groups, and the split is the point. MECHANICAL
damage is damage the contract itself resolves, and the tool should offer the
fix. ASKING damage is damage the contract detects and cannot resolve, and
there the tool must write a question with no proposal attached. A suite made
only of the first group cannot tell the two apart -- which is what happened
here: the anti-coercion invariant sat green for a day while executing its own
assertion zero times, because no mutation produced a value the contract could
not determine.

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
import proposals                                             # noqa: E402
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


# Every mutation above is MECHANICAL: the contract determines the correction,
# so the right behaviour is to propose it. The three below are the ones this
# project exists for -- the contract can detect them and cannot resolve them.

def negative_peak_width(bundle: Path):
    """A diffraction peak with a negative width.

    `fwhm_deg` carries `exclusiveMinimum: 0`, so the contract says the value is
    wrong without saying what it should be. A negative width can be a sign
    convention, a failed fit, or a saturated channel read as a peak, and those
    have different corrections.
    """
    p = bundle / "payload.json"
    doc = json.loads(p.read_text())
    doc["features"][0]["fwhm_deg"] = -0.1
    p.write_text(json.dumps(doc, indent=2))


def unit_swapped_for_another(bundle: Path):
    """A declared unit the contract does not permit.

    The hardest case in the taxonomy, because the contract names exactly ONE
    permitted unit -- so the edit looks as determinable as any other const.
    Editing the DECLARATION while the numbers stay as measured is what makes
    it dangerous: the document then passes every later check while carrying
    values wrong by whatever the conversion factor was.
    """
    p = bundle / "payload.json"
    doc = json.loads(p.read_text())
    doc["units"]["fwhm_deg"] = "urn:feder:worked:unit:one"
    p.write_text(json.dumps(doc, indent=2))


def data_origin_not_permitted(bundle: Path):
    """A provenance claim outside the permitted enum.

    Nothing in the schema can check whether a real experiment produced a
    document -- the kit says the label is an assertion, not proof -- so an
    edit here would never be contradicted by anything downstream. That is
    exactly the shape of a change nobody would catch.
    """
    p = bundle / "payload.json"
    doc = json.loads(p.read_text())
    doc["data_origin"] = "simulated"
    p.write_text(json.dumps(doc, indent=2))


# The groups the invariants below run over. `remove_the_payload_profile_triples`
# is deliberately in neither: it produces nothing offline, which is its own
# test at the end of this file.
MECHANICAL = (remove_a_pinned_unit, manifest_escapes_the_bundle,
              declare_the_wrong_family)
ASKING = (negative_peak_width, unit_swapped_for_another,
          data_origin_not_permitted)
ALL_MUTATIONS = MECHANICAL + ASKING


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
    (negative_peak_width,           "R3_PAYLOAD_CONTRACT"),
    (unit_swapped_for_another,      "R3_PAYLOAD_CONTRACT"),
    (data_origin_not_permitted,     "R3_PAYLOAD_CONTRACT"),
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
@pytest.mark.parametrize("mutation", ASKING,
                         ids=lambda v: getattr(v, "__name__", v))
def test_a_value_the_contract_cannot_determine_is_asked_about(tmp_path,
                                                              mutation):
    """The anti-coercion mechanism, one case per reason it applies.

    Each of these is a rejection the contract detects and cannot resolve. The
    tool must write a QUESTION, and `proposal is None` is the half that
    matters: a proposal is the only thing a reviewer can accept with one
    keystroke, so the absence of one is what forces the conversation.
    """
    bundle = mutate(tmp_path, mutation)
    rejections = probe(bundle)
    assert rejections, "the damage was not reported at all"
    for rejection in rejections:
        verdict = classify(rejection)
        assert verdict["confidence"] == "unknown", (
            f"{rejection.get('message','')[:70]!r} was classified "
            f"{verdict['confidence']}; a value only the experimenter can "
            f"know must be unknown")
        record = proposals.attach(
            decisions.make_record(rejection, verdict, bundle, digests={}))
        assert record["proposal"] is None, (
            f"the tool offered to edit the data it cannot know: "
            f"{record['proposal']}")
        assert record["question"], (
            "neither a question nor a proposal was written, so the rejection "
            "reaches nobody")
        assert record["question"]["why_not_proposing"], (
            "a question must say why it is not a proposal, or the reviewer "
            "cannot tell a limit of the tool from a limit of the contract")


@needs_probe
def test_a_pinned_unit_is_derivable_and_refused_anyway(tmp_path):
    """The one case where the GUARD, not the derivation, is what stops us.

    A wrong unit arrives as `validator: "const"` with a json_pointer and the
    permitted value attached -- byte for byte the shape `_derive_edit` turns
    into a proposal for a wrong `technique`. Nothing in the error itself says
    "do not touch this"; only `may_propose` does. So if that check were ever
    dropped from `proposals.attach`, every other test in this file would still
    pass while the tool quietly began rewriting unit declarations.
    """
    bundle = mutate(tmp_path, unit_swapped_for_another)
    pinned = [r for r in probe(bundle)
              if r.get("validator") == "const"
              and (r.get("json_pointer") or "").startswith("/units/")]
    assert pinned, "the unit rejection no longer arrives as a const violation"
    for rejection in pinned:
        verdict = classify(rejection)
        assert proposals._derive_edit(rejection) is not None, (
            "this test has stopped testing anything: the edit is no longer "
            "derivable, so the taxonomy guard is not what refuses it")
        assert not may_propose(verdict)
        record = proposals.attach(
            decisions.make_record(rejection, verdict, bundle, digests={}))
        assert record["proposal"] is None, (
            "a unit declaration was proposed for editing. Changing the "
            "declaration without converting the numbers rescales the data "
            "and passes every later check")


@needs_probe
def test_a_value_only_the_experimenter_knows_is_never_proposed(tmp_path):
    """The anti-coercion rule as an invariant over every mutation.

    The `witnessed` counter is not decoration. Written over the three
    mechanical mutations alone, this test passed while running its own
    assertion ZERO times: none of them produces an `unknown`, so the loop body
    never executed and the invariant was never checked. Measured 2026-11-23.
    An invariant test has to assert that it saw a case.
    """
    witnessed = 0
    for mutation in ALL_MUTATIONS:
        bundle = mutate(tmp_path / mutation.__name__, mutation)
        for rejection in probe(bundle):
            verdict = classify(rejection)
            if verdict["confidence"] == "unknown":
                witnessed += 1
                assert not may_propose(verdict), (
                    f"{mutation.__name__}: the tool offered to supply a value "
                    f"it cannot know -- {rejection.get('message','')[:80]}")
    assert witnessed >= len(ASKING), (
        f"only {witnessed} unknown-confidence rejections across "
        f"{len(ALL_MUTATIONS)} mutations; this test is not exercising the rule "
        f"it claims to")


@needs_probe
def test_no_mutation_can_authorise_itself(tmp_path, monkeypatch):
    """T6, containment. The guarantee we have put in writing to the schema's
    author: the tool holds nothing that can mark its own work approved."""
    monkeypatch.setattr(decisions, "DECISIONS", tmp_path / "decisions")
    monkeypatch.setattr(decisions, "LEDGER", tmp_path / "ledger.json")
    for mutation in ALL_MUTATIONS:
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
