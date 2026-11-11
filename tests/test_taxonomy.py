"""Every rejection the system can produce must classify to exactly one class.

The point of this file is that a kit update which adds a rule FAILS here,
rather than falling through into "escalate" where nobody notices that the
agent stopped understanding part of the schema.

The kit-dependent tests skip unless MIA_KIT_DIR is set; the error-string test
runs anywhere, because the strings are listed here.
"""
import json
import os
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "code"))
from taxonomy import (CLASSES, CONFIDENCE, DECISION_TYPES,  # noqa: E402
                      classify, classify_label, may_propose)

# Every ValueError the kit can raise, collected by grepping it on 2026-11-11.
KIT_ERRORS = [
    "Application and validator roles cannot assume the schema-owner role",
    "Duplicate JSON key",
    "Installation requires autocommit connection",
    "Invalid family",
    "IRI required",
    "Missing explicit artifact in bundle manifest",
    "NO_IDENTITY_COLLAPSE: unsupported identity assertion",
    "Non-finite JSON number",
    "No owner-approved schema for this profile",
    "Payload contract mismatch",
    "Payload must be an ordinary operator-owned file",
    "Payload path escapes bundle",
    "PAYLOAD_SIZE",
    "Referenced schemas unsupported; approve a self-contained contract",
    "Retained digest mismatch",
    "Role inherits prohibited owner/receipt/metadata privileges",
    "Roles cannot disable trigger enforcement",
    "Schema digest mismatch",
    "Unapproved validator implementation",
    "Unknown family",
    "Unknown instrument family",
    "Use existing unprivileged roles",
    "Writer and validator must be separate roles",
    "Writer inherits validator privileges",
    "Writer must not be a member of the validator role",
]

PREDICATES = ROOT / "data" / "rule_predicates.json"
needs_rules = pytest.mark.skipif(
    not PREDICATES.is_file(),
    reason="run probe.py rules --extract first (needs MIA_KIT_DIR)")


def labels():
    return sorted({r["label"] for r in json.loads(PREDICATES.read_text())})


# ------------------------------------------------------------ the guarantee --
@needs_rules
def test_every_sql_rule_label_classifies():
    missed = [l for l in labels() if classify_label(l)["class"] == "UNCLASSIFIED"]
    assert missed == [], (
        f"{len(missed)} rule label(s) have no entry in the taxonomy: "
        f"{missed[:10]}. Add them rather than letting them escalate silently.")


def test_every_kit_error_string_classifies():
    missed = [e for e in KIT_ERRORS
              if classify({"channel": "python_valueerror",
                           "message": e})["class"] == "UNCLASSIFIED"]
    assert missed == [], f"unclassified kit errors: {missed}"


@needs_rules
def test_vocabularies_are_closed():
    """Nothing may invent a class, decision type or confidence level."""
    for l in labels():
        v = classify_label(l)
        assert v["class"] in CLASSES, v
        assert v["decision_type"] in DECISION_TYPES, v
        assert v["confidence"] in CONFIDENCE, v


# ------------------------------------------- the anti-coercion guarantee ----
def test_a_value_outside_its_domain_is_never_proposable():
    """The whole point. A measurement is the experimenter's, not ours.

    An out-of-domain reading might be a bad measurement, a unit error, a sign
    convention, or a real result the contract did not anticipate. Proposing a
    number here would be a guess wearing the clothes of an answer.
    """
    for message in ("quantity domain features.solute_fraction",
                    "nonfinite corrected_rate_per_s"):
        v = classify({"channel": "payload_semantics", "message": message})
        assert v["confidence"] == "unknown", (message, v)
        assert not may_propose(v), f"{message} must be asked, not proposed"


def test_a_value_outside_schema_bounds_is_never_proposable():
    v = classify({"channel": "jsonschema",
                  "message": "-1.0 is less than the minimum of 0"})
    assert v["confidence"] == "unknown"
    assert not may_propose(v)


def test_a_missing_declaration_IS_proposable():
    """Where the contract pins the value, stating it is not a guess."""
    for channel, message in (("payload_semantics", "missing unit depth_nm"),
                             ("jsonschema", "'corrected_count' is a required property")):
        v = classify({"channel": channel, "message": message})
        assert v["confidence"] == "mechanical", (message, v)
        assert may_propose(v)


def test_an_unrecognised_rejection_escalates_rather_than_passing():
    v = classify({"channel": "something_new", "message": "no idea"})
    assert v["class"] == "UNCLASSIFIED"
    assert v["decision_type"] == "escalate"
    assert not may_propose(v)


# ------------------------------------------------------- specific placements --
@needs_rules
def test_the_bulk_of_graph_rules_are_mechanical():
    """CARDINALITY / RANGE / DISJOINT name their class and property, and the
    catalog gives domain, range and parent -- so they are determinable. This
    is where the automation earns its place, so a regression matters."""
    mech = [l for l in labels()
            if classify_label(l)["class"] == "R4_GRAPH_SHAPE"
            and classify_label(l)["confidence"] == "mechanical"]
    assert len(mech) > 400, f"only {len(mech)} mechanical graph rules"


@needs_rules
def test_scientific_intent_rules_always_escalate():
    """CHAR_*, PISCES_*, COMPARE_* and the history/timing rules encode what the
    model believes about an experiment. A plausible coercion rewrites science."""
    for l in labels():
        head = l.split()[0]
        if head.startswith(("CHAR_JSON_",)):
            continue
        if head.startswith(("CHAR_", "PISCES_", "COMPARE_")) or head in (
                "ACYCLIC", "HISTORY_DAG", "ANALYSIS_DAG", "ONE_PRODUCER",
                "ONE_CONSUMER", "OBS_TIME", "STATE_TIME", "DIMENSION"):
            v = classify_label(l)
            if v["class"] == "R8_NO_BRANCH":      # CHAR_FAMILY is its own case
                continue
            assert v["decision_type"] == "escalate", (l, v)
            assert not may_propose(v), l


def test_owner_approval_is_never_proposable_as_a_data_fix():
    """The agent may print the command. It may not perform the act."""
    v = classify({"channel": "python_valueerror",
                  "message": "No owner-approved schema for this profile"})
    assert v["decision_type"] == "owner_approval"
    v2 = classify({"channel": "python_valueerror",
                   "message": "Unapproved validator implementation"})
    assert v2["decision_type"] == "owner_approval"


def test_sealed_records_are_not_proposable_for_mutation():
    for message in ("materials: PAYLOAD_IMMUTABLE",
                    "materials: PAYLOAD_BINDING_MISMATCH",
                    "Retained digest mismatch"):
        v = classify({"channel": "sql_reject_if", "message": message})
        assert v["class"] == "R6_SEALED"
        assert v["decision_type"] == "escalate"


def test_an_unknown_technique_is_contract_authoring_not_a_data_fix():
    """The data may be sound and simply have no home. Squeezing it into the
    nearest family that fits is the failure mode to avoid."""
    v = classify({"channel": "python_valueerror", "message": "Unknown family"})
    assert v["class"] == "R8_NO_BRANCH"
    assert v["decision_type"] == "contract_authoring"


def test_a_probe_gap_escalates_rather_than_reading_as_clean():
    v = classify({"channel": "probe_unsupported",
                  "message": "SOME_RULE: UndefinedColumn"})
    assert v["decision_type"] == "escalate"


def test_a_wrapped_error_is_classified_by_what_it_IS():
    """A re-raised exception names where it surfaced, not what it is.

    The sealing step re-raises whatever admit() raised, so an owner-approval
    problem arrived as `seal_failed: ValueError: No owner-approved schema...`
    and was classified by the wrapper as a payload data_fix -- which would send
    a person to edit data when an owner approval is what is needed.
    """
    v = classify({"channel": "seal_failed",
                  "message": "ValueError: No owner-approved schema for this profile"})
    assert v["class"] == "R1_CONTRACT_NOT_APPROVED"
    assert v["decision_type"] == "owner_approval"

    v2 = classify({"channel": "seal_failed",
                   "message": "ValidationError: 'x' is a required property "
                              "Failed validating 'required'"})
    assert v2["confidence"] == "mechanical", "trailing explanation broke the match"


def test_an_unrecognised_seal_failure_is_not_proposable():
    v = classify({"channel": "seal_failed",
                  "message": "RuntimeError: something nobody has seen"})
    assert not may_propose(v), "an unrecognised failure must be asked about"


def test_a_unit_mismatch_is_never_proposable():
    """Editing the declaration to match the contract rescales the data.

    The contract pins one unit, so "change micrometre to nanometre" reads as a
    lookup. But the NUMBERS stay in micrometres, the data is then wrong by a
    thousand, and every later check passes. The kit says it plainly: unit
    conversions require explicit upstream processing.
    """
    v = classify({"channel": "jsonschema",
                  "message": "'urn:feder:worked:unit:nanometre' was expected",
                  "json_pointer": "/units/depth_nm"})
    assert v["confidence"] == "unknown"
    assert not may_propose(v), "a unit mismatch must be asked, not proposed"


def test_a_non_unit_const_is_still_proposable():
    """The narrowing is about units, not about const violations generally."""
    v = classify({"channel": "jsonschema",
                  "message": "'0.5.0-payload' was expected"})
    assert v["confidence"] == "mechanical"
