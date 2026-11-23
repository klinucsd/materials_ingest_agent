"""The loop, end to end: probe, record, wait, apply what was approved, re-probe.

Stage 6. What matters here is not that the loop can reach a clean bundle --
it is what it refuses to do on the way. Three properties, each with a test:

    it stops for a person            exit 2, and nothing on disk changed
    it applies only what was approved by name, and only while the answer
                                     still matches the question it answered
    it checks its own work           an approved change must clear what it
                                     predicted, or the record is reopened

Every test redirects the decision store to a temporary directory. `decisions`
holds it in module constants, and a run against the real store would add to
the ledger that carries the questions for the data provider -- which happened
once, by hand, while this was being written.
"""
import json
import os
import shutil
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
import decisions                                             # noqa: E402
import mia                                                   # noqa: E402

KIT = os.environ.get("MIA_KIT_DIR")
VENV_KIT = ROOT / ".venv-kit" / "bin" / "python"
GOOD = ROOT / "data" / "bundles" / "xray_diffraction"

needs_probe = pytest.mark.skipif(
    not KIT or not VENV_KIT.exists() or not GOOD.is_dir(),
    reason="needs MIA_KIT_DIR, .venv-kit and the copied bundles")


@pytest.fixture
def store(tmp_path, monkeypatch):
    """A decision store that is not the repository's."""
    monkeypatch.setattr(decisions, "DECISIONS", tmp_path / "decisions")
    monkeypatch.setattr(decisions, "LEDGER", tmp_path / "ledger.json")
    return tmp_path


def a_bundle_with_the_wrong_technique(tmp_path) -> Path:
    """A const the contract pins, carrying a value from another branch."""
    bundle = tmp_path / "bundle"
    shutil.copytree(GOOD, bundle)
    payload = bundle / "payload.json"
    doc = json.loads(payload.read_text())
    doc["technique"] = "Laser flash analysis"
    payload.write_text(json.dumps(doc, indent=2))
    return bundle


def technique_of(bundle: Path) -> str:
    return json.loads((bundle / "payload.json").read_text())["technique"]


@needs_probe
def test_it_stops_for_a_person_and_changes_nothing(store):
    bundle = a_bundle_with_the_wrong_technique(store)
    before = (bundle / "payload.json").read_text()

    assert mia.main(["mia.py", "run", str(bundle)]) == 2, (
        "a bundle with an unanswered rejection must exit 2, not 0")
    assert (bundle / "payload.json").read_text() == before, (
        "the loop edited a bundle with no approval to do so")

    records = decisions.all_records()
    assert len(records) == 1
    assert records[0]["human"] is None
    assert records[0]["population_authorized"] is False


@needs_probe
def test_a_pinned_const_is_proposed_not_asked(store):
    """The contract names one permitted value, so the edit is stated exactly."""
    bundle = a_bundle_with_the_wrong_technique(store)
    mia.main(["mia.py", "run", str(bundle)])

    record = decisions.all_records()[0]
    assert record["question"] is None
    proposal = record["proposal"]
    assert proposal is not None, (
        "a value the contract determines should be proposed, not asked about")
    assert proposal["json_pointer"] == "/technique"
    assert proposal["to"] == "Powder XRD"
    assert proposal["predicted_effect"]["clears"] == [record["logical_id"]]


@needs_probe
def test_an_approved_proposal_is_applied_and_verified(store):
    """The whole cycle. Exit 0 only after the change cleared what it predicted."""
    bundle = a_bundle_with_the_wrong_technique(store)
    assert mia.main(["mia.py", "run", str(bundle)]) == 2
    record = decisions.all_records()[0]

    decisions.answer(record["id"], "approved", by="test")

    assert mia.main(["mia.py", "run", str(bundle)]) == 0, (
        "with the one rejection approved and applied, the bundle should have "
        "nothing left to resolve")
    assert technique_of(bundle) == "Powder XRD"

    # The flags are untouched by having been acted on.
    after = decisions.load(record["id"])
    assert after["population_authorized"] is False
    assert after["review_required"] is True


@needs_probe
def test_an_answer_to_a_different_question_is_not_applied(store):
    """The signature binds an answer to the exact question it answered."""
    bundle = a_bundle_with_the_wrong_technique(store)
    mia.main(["mia.py", "run", str(bundle)])
    record = decisions.all_records()[0]
    decisions.answer(record["id"], "approved", by="test")

    # Someone edits the proposal after it was approved.
    record = decisions.load(record["id"])
    record["proposal"]["to"] = "Something Else Entirely"
    decisions.save(record)
    assert not decisions.honours_signature(record)

    assert mia.main(["mia.py", "run", str(bundle)]) == 2
    assert technique_of(bundle) == "Laser flash analysis", (
        "an approval was applied to a proposal that had changed since")


@needs_probe
def test_a_proposal_that_does_not_do_what_it_said_reopens(store):
    """The prediction is what separates a fix from a change that merely ran."""
    bundle = a_bundle_with_the_wrong_technique(store)
    mia.main(["mia.py", "run", str(bundle)])
    record = decisions.all_records()[0]

    # Approve an edit that writes a value which is still not the permitted
    # one, while claiming it will clear the rejection.
    record["proposal"]["to"] = "Still Not Powder XRD"
    decisions.save(record)
    decisions.answer(record["id"], "approved", by="test")

    assert mia.main(["mia.py", "run", str(bundle)]) == 2
    after = decisions.load(record["id"])
    assert after.get("verification", {}).get("result") == \
        "predicted_effect_not_observed", (
        "a change that did not clear what it predicted was passed over")
