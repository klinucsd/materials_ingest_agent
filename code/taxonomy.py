"""Classify a rejection: what kind of problem is it, and whose decision is it?

A pure ordered lookup. No model, no network, no database — so it is cheap,
deterministic and testable, and a test asserts that every one of the schema's
552 rule labels and every error the kit can raise maps to exactly one class.
A kit update that adds a rule fails that test instead of falling through
silently into "escalate".

The agent uses this to decide WHICH of two things to write, and that is the
whole point of the design:

  a PROPOSAL, when the correct value follows from the contract itself, or
  a QUESTION, when the value is a fact about the experiment.

`confidence` carries that distinction, and `unknown` must never become a
proposal. A missing unit declaration whose schema permits exactly one value is
mechanical. An out-of-range measurement might be a bad reading, a sign
convention, or a real result that the contract did not anticipate — and a
plausible guess at it is precisely the failure this project exists to prevent.
"""
from __future__ import annotations

import re

# --------------------------------------------------------------- vocabulary --
# Deliberately small and fixed. Free-text classification drifts between runs
# and makes two reviews incomparable.

DECISION_TYPES = {
    "owner_approval":     "only the database owner can resolve it, and the act "
                          "is append-only",
    "data_fix":           "the bundle is wrong and can be corrected",
    "contract_authoring": "the data may be sound and there is no contract for "
                          "it yet",
    "escalate":           "needs a human judgement this tool should not make",
}

CONFIDENCE = {
    "mechanical": "the contract determines the correct value; a proposal may "
                  "state it exactly",
    "inferred":   "the correct value is strongly implied but not stated; "
                  "propose it and say what it rests on",
    "unknown":    "only someone who knows the experiment can answer. ASK. "
                  "Never propose a value",
}

CLASSES = {
    "R1_CONTRACT_NOT_APPROVED": "no owner-approved contract for this profile",
    "R2_VALIDATOR_FINGERPRINT": "the validating environment is not the one the "
                                "approval was made under",
    "R3_PAYLOAD_CONTRACT":      "the payload document disagrees with its contract",
    "R4_GRAPH_SHAPE":           "the record graph is the wrong shape",
    "R5_DOMAIN_PROVENANCE":     "the graph is well-formed but violates the "
                                "model's scientific intent",
    "R5B_GRAPH_JSON":           "an embedded JSON document disagrees with the "
                                "graph around it",
    "R6_SEALED":                "already sealed, or a receipt does not match",
    "R7_ENVIRONMENT":           "deployment, privileges or bundle hygiene, not data",
    "R8_NO_BRANCH":             "no implemented contract covers this technique",
}


class Verdict(dict):
    """A dict so it serialises straight into a decision record."""

    def __init__(self, cls, decision, confidence, why):
        super().__init__(**{"class": cls, "decision_type": decision,
                            "confidence": confidence, "why": why})


# -------------------------------------------------------------- the matchers --
# Ordered: the first match wins. Each entry is (channel-or-None, pattern,
# class, decision_type, confidence, why).
#
# `pattern` is matched against the rejection's message, except where a rule
# label is present, in which case the label is matched. Patterns are anchored
# where a prefix would otherwise swallow a more specific rule -- CHAR_JSON_*
# must be tested before CHAR_*, and that ordering is load-bearing.

_RULES: list[tuple] = [
    # ---- R1: no approved contract. The single biggest gate.
    (None, r"^No owner-approved schema for this profile$",
     "R1_CONTRACT_NOT_APPROVED", "owner_approval", "mechanical",
     "the profile is named by the bundle; an owner must enroll a contract for "
     "it. The command is determined, the decision is not"),
    (None, r"^Schema digest mismatch$",
     "R1_CONTRACT_NOT_APPROVED", "owner_approval", "unknown",
     "the stored schema bytes do not hash to the stored digest, which should "
     "be impossible; the owner must look at the database"),
    (None, r"^PAYLOAD_SCHEMA_PROFILE$",
     "R1_CONTRACT_NOT_APPROVED", "owner_approval", "mechanical",
     "the profile is not typed as a ProfileSpecification, or its version "
     "disagrees with the approved contract version"),

    # ---- R2: the environment moved. Not fixable by approving again.
    (None, r"^Unapproved validator implementation$",
     "R2_VALIDATOR_FINGERPRINT", "owner_approval", "mechanical",
     "the fingerprint covers the Python version, the jsonschema version and "
     "the bytes of six kit files. Because approvals are append-only and keyed "
     "on the profile, re-approving the same profile is impossible: either "
     "restore the environment, or mint new profile identities"),

    # ---- A mapping gap: the SOURCE never provided something the schema
    #      requires. Tested before the R4 rules, because it arrives carrying
    #      the CARDINALITY label it would fire as at commit -- and that label
    #      classifies as mechanical, which is right for a built bundle and
    #      wrong here. On a built bundle the catalog tells you the shape of the
    #      missing triple. When the source never stated the value, nobody but
    #      the experimenter knows it, and the catalog cannot help.
    ("mapping_gap", r"^(materials: )?CARDINALITY \w+\.\w+$",
     "R4_GRAPH_SHAPE", "data_fix", "unknown",
     "the schema requires this and the source document does not state it. The "
     "shape of what is missing is known; the value is not, and is not ours to "
     "choose. ASK the provider"),
    ("mapping_gap", r".",
     "R4_GRAPH_SHAPE", "data_fix", "unknown",
     "the source does not provide something the schema requires"),
    ("fabrication", r".",
     "R3_PAYLOAD_CONTRACT", "escalate", "unknown",
     "a claim about the source did not check out against the source text. "
     "Nothing is built from it. Worth a look: it is either a reading error or "
     "a value with nothing behind it"),

    # ---- R8: no contract exists for this technique. Tested before R3,
    #      because "Unknown family" would otherwise read as a payload defect.
    (None, r"^(Unknown family|Invalid family|Unknown instrument family)$",
     "R8_NO_BRANCH", "contract_authoring", "unknown",
     "the data may be perfectly sound and simply have no implemented branch. "
     "The honest outputs are to author a contract or to call it out of scope, "
     "never to load it under the nearest family that fits"),
    (None, r"^Referenced schemas unsupported",
     "R8_NO_BRANCH", "contract_authoring", "mechanical",
     "the approved schema must be self-contained; its $ref must be inlined "
     "before it can be enrolled"),
    (None, r"^CHAR_FAMILY$",
     "R8_NO_BRANCH", "contract_authoring", "unknown",
     "the declared family is outside the sixteen the schema implements"),

    # ---- R6: sealed or mismatched receipts. Never proposable for mutation.
    (None, r"^(materials: )?PAYLOAD_(IMMUTABLE|BINDING_MISMATCH|DIGEST)$",
     "R6_SEALED", "escalate", "unknown",
     "sealed records are append-only. A correction is a NEW artifact and "
     "dataset identity, never an edit to this one"),
    (None, r"^(materials: )?PAYLOAD_REQUIRED$",
     "R6_SEALED", "escalate", "mechanical",
     "rows were staged for a dataset whose payload was never sealed. During a "
     "dry run this is usually consequential -- something earlier stopped the "
     "seal -- so resolve the other rejections first and re-probe"),
    (None, r"^Retained digest mismatch$",
     "R6_SEALED", "escalate", "unknown",
     "the digest the database generated differs from the bytes we sent; this "
     "should be impossible and wants a human"),
    ("sql_constraint", r"duplicate key|already exists|23505",
     "R6_SEALED", "escalate", "mechanical",
     "this bundle, artifact or profile is already recorded. Nothing to do "
     "unless a new identity is genuinely wanted"),

    # ---- R7: deployment, privilege and bundle hygiene. Not data.
    (None, r"^(Writer and validator must be separate roles|"
           r"Use existing unprivileged roles|"
           r"Application and validator roles cannot assume the schema-owner role|"
           r"Roles cannot disable trigger enforcement|"
           r"Role inherits prohibited owner/receipt/metadata privileges|"
           r"Writer inherits validator privileges|"
           r"Writer must not be a member of the validator role|"
           r"Installation requires autocommit connection)$",
     "R7_ENVIRONMENT", "escalate", "mechanical",
     "the database roles or connection are not set up as the schema requires; "
     "the remediation is a deployment step, not a change to the data"),
    (None, r"^PAYLOAD_SIZE$",
     "R7_ENVIRONMENT", "escalate", "unknown",
     "the artifact is empty or past the per-artifact limit. Splitting it or "
     "holding it by reference is a decision about the data's shape"),
    (None, r"^Payload path escapes bundle$",
     "R7_ENVIRONMENT", "data_fix", "mechanical",
     "the manifest points outside the bundle directory; the path can be "
     "corrected"),
    (None, r"^Payload must be an ordinary operator-owned file$",
     "R7_ENVIRONMENT", "escalate", "unknown",
     "the manifest names something that is not a regular file"),
    (None, r"^Missing (explicit artifact in bundle manifest|payload file)",
     "R7_ENVIRONMENT", "data_fix", "mechanical",
     "the manifest and the graph disagree about which artifacts exist; which "
     "of the two is right is usually evident from the bundle"),
    ("missing_file", r".",
     "R7_ENVIRONMENT", "data_fix", "mechanical",
     "a file the bundle must contain is absent"),
    ("sql_privilege", r".",
     "R7_ENVIRONMENT", "escalate", "mechanical",
     "the connected role lacks a privilege the operation needs"),
    ("probe_error", r".",
     "R7_ENVIRONMENT", "escalate", "unknown",
     "the probe itself failed, so the rejection list is incomplete and must "
     "not be read as clean"),
    ("probe_unsupported", r".",
     "R7_ENVIRONMENT", "escalate", "mechanical",
     "this rule could not be evaluated outside its trigger, so it remains "
     "discoverable only at commit. The list is incomplete here"),

    # ---- R5b: embedded JSON vs the graph. Carved out of R5 because the kit
    #      reproduces these offline with exact subjects, so they are
    #      mechanical enough to propose.
    (None, r"^(materials: )?CHAR_JSON_",
     "R5B_GRAPH_JSON", "data_fix", "inferred",
     "an embedded configuration or dataset descriptor disagrees with the "
     "graph it belongs to. The kit reproduces these offline with the exact "
     "subjects, so the edit is usually determinable"),

    # ---- R3: the payload document vs its contract.
    (None, r"^'[^']+' is a required property\b",
     "R3_PAYLOAD_CONTRACT", "data_fix", "mechanical",
     "a required key is absent. Where the contract pins one permitted value "
     "for it, the proposal states that value"),
    # WHEN MAY A DECLARATION BE EDITED TO MATCH THE CONTRACT?
    #
    # The contract names one permitted value, so every such violation looks
    # like a lookup. The test is not what the field is called. It is:
    #
    #     if the edit were wrong, would anything catch it?
    #
    # Propose the edit when being wrong produces further rejections -- the
    # remaining validation is then doing the checking. Ask when the edit makes
    # the document internally consistent and wrong, because nothing downstream
    # will ever disagree with it again.
    #
    #   unit              relabelling micrometre as nanometre leaves the
    #                     NUMBERS unchanged and silently rescales the data by a
    #                     thousand. Nothing catches it.                   ASK
    #   data_origin       "synthetic" -> "experimental" asserts an experiment
    #                     happened. The kit says so itself: "that label is an
    #                     assertion, not proof".                           ASK
    #   contract_version  claiming a different contract is tested by every
    #                     other check against that contract; a document that
    #                     does not satisfy it produces more rejections, not
    #                     silence.                                     PROPOSE
    #   family/technique  likewise -- the payload's field names are branch
    #                     specific, so a false claim fails validation. PROPOSE
    #
    # A UNIT is tested before the general const rule, and is never mechanical.
    # The contract names the one permitted unit, so "change micrometre to
    # nanometre" looks determinable -- and editing the declaration while the
    # NUMBERS stay in micrometres yields data wrong by a factor of a thousand
    # that passes every later check. The kit says so itself: "unit conversions
    # require explicit upstream processing". Converting the data is a decision
    # about the data, and whoever measured it decides.
    (None, r"^'urn:feder:worked:unit:[^']+' was expected",
     "R3_PAYLOAD_CONTRACT", "data_fix", "unknown",
     "the contract pins a different unit than the source states. Do NOT edit "
     "the declaration to match: that leaves the numbers in the source's unit "
     "and silently rescales the data. The values must be converted upstream, "
     "which is the provider's decision, not a contract lookup"),
    # data_origin: unverifiable by construction, so never proposable.
    (None, r"^'(synthetic|experimental)' (was expected|is not one of)|"
           r"is not one of \['synthetic', 'experimental'\]",
     "R3_PAYLOAD_CONTRACT", "data_fix", "unknown",
     "data_origin asserts whether a real experiment produced this. Nothing in "
     "the schema can verify it -- the kit says the label is an assertion, not "
     "proof -- so an edit here would never be contradicted by anything. Only "
     "whoever produced the data can say"),
    ("json_pointer:/data_origin", r".",
     "R3_PAYLOAD_CONTRACT", "data_fix", "unknown",
     "data_origin is an unverifiable assertion about provenance"),
    (None, r"was expected\b|is not one of|does not match|"
           r"is not valid under any of the given schemas",
     "R3_PAYLOAD_CONTRACT", "data_fix", "mechanical",
     "the contract permits an enumerated or constant value and the document "
     "carries a different one"),
    (None, r"^Additional propert",
     "R3_PAYLOAD_CONTRACT", "data_fix", "mechanical",
     "the contract is closed; the extra key either belongs elsewhere or "
     "cannot be carried"),
    (None, r"is not of type|is too short|is too long|"
           r"has non-unique elements",
     "R3_PAYLOAD_CONTRACT", "data_fix", "inferred",
     "the shape is wrong in a way the contract describes; the correction is "
     "usually evident but may hide a units or convention problem"),
    (None, r"less than|greater than|minimum|maximum",
     "R3_PAYLOAD_CONTRACT", "data_fix", "unknown",
     "a VALUE is outside the contract's bounds. That may be a bad reading, a "
     "unit, a sign convention, or a real result the contract did not "
     "anticipate. ASK -- do not choose a number"),
    (None, r"^missing unit ",
     "R3_PAYLOAD_CONTRACT", "data_fix", "mechanical",
     "every numeric column must be declared in units, and the contract pins "
     "the permitted value for each, so the declaration can be stated exactly"),
    (None, r"^quantity domain ",
     "R3_PAYLOAD_CONTRACT", "data_fix", "unknown",
     "a measurement is outside the domain its quantity allows -- a negative "
     "width, a fraction above one. Only someone who knows the measurement can "
     "say whether the value, the unit or the convention is wrong"),
    (None, r"^nonfinite ",
     "R3_PAYLOAD_CONTRACT", "data_fix", "unknown",
     "a non-finite number stands for something -- a failed fit, a saturated "
     "channel, a gap. What it stands for is not ours to decide"),
    (None, r"^(Duplicate JSON key|Non-finite JSON number)$",
     "R3_PAYLOAD_CONTRACT", "data_fix", "unknown",
     "the document is malformed in a way that loses information: which of the "
     "duplicate values was meant cannot be recovered from the file"),
    (None, r"^Payload contract mismatch$",
     "R3_PAYLOAD_CONTRACT", "data_fix", "inferred",
     "the document's family, technique or contract version disagrees with the "
     "approved contract. Either the document names the wrong branch or the "
     "wrong contract was approved for it"),
    (None, r"^(orientation quaternion|tensor (rank|size|mask|coordinates|"
           r"nonfinite|slice index)|duplicate feature key|unknown trace "
           r"reference|graph role contract|graph node must reference|"
           r"duplicate graph identity|dangling graph endpoint|unknown graph "
           r"entity_ref)",
     "R3_PAYLOAD_CONTRACT", "data_fix", "inferred",
     "a structural rule inside the payload. The contract says what the shape "
     "must be, so the fix is usually determinable from it"),
    ("payload_semantics", r".",
     "R3_PAYLOAD_CONTRACT", "data_fix", "unknown",
     "a semantic check on the payload that this table does not recognise; "
     "treat the value as the experimenter's until told otherwise"),
    ("seal_failed", r".",
     "R3_PAYLOAD_CONTRACT", "data_fix", "unknown",
     "sealing this dataset's payload failed for a reason this table does not "
     "recognise. The specific causes are matched above, so reaching here means "
     "something new: ask, do not propose"),

    # ---- R4: graph shape. The label names the class and property, and the
    #      catalog gives domain, range and parent, so these are mechanical.
    (None, r"^(materials: )?CARDINALITY ",
     "R4_GRAPH_SHAPE", "data_fix", "mechanical",
     "the label names the class and property whose count is wrong, and the "
     "catalog gives the permitted range. The missing or surplus triple is "
     "determinable"),
    (None, r"^(materials: )?RANGE ",
     "R4_GRAPH_SHAPE", "data_fix", "mechanical",
     "the object of this property is not of the type the catalog declares for "
     "its range"),
    (None, r"^(materials: )?DISJOINT ",
     "R4_GRAPH_SHAPE", "data_fix", "mechanical",
     "one entity is typed as both of two classes the model declares disjoint; "
     "the label names the pair"),
    (None, r"^(materials: )?(TYPE_CLOSURE|ENTITY_TYPE)$",
     "R4_GRAPH_SHAPE", "data_fix", "mechanical",
     "an entity is missing an ancestor type the class hierarchy requires; the "
     "catalog gives the chain"),
    (None, r"^IRI required$",
     "R4_GRAPH_SHAPE", "data_fix", "mechanical",
     "an object property was given a literal or blank node where the model "
     "requires a named entity"),
    (None, r"^NO_IDENTITY_COLLAPSE",
     "R4_GRAPH_SHAPE", "escalate", "unknown",
     "the bundle asserts two distinct IRIs are the same thing. Whether they "
     "really are the same specimen is an identity judgement with no code path "
     "and no alias mechanism behind it"),
    ("turtle_parse", r".",
     "R4_GRAPH_SHAPE", "data_fix", "inferred",
     "records.ttl does not parse; the parser names the position"),
    ("missing_link", r".",
     "R4_GRAPH_SHAPE", "data_fix", "mechanical",
     "a required link between records is absent"),

    # ---- R5: the model's scientific intent. Last, so the specific
    #      structural rules above are not swallowed by it.
    (None, r"^(materials: )?(CHAR_|PISCES_|COMPARE_|DETAIL_|METROLOGY_|"
           r"COMPOSITION_)",
     "R5_DOMAIN_PROVENANCE", "escalate", "unknown",
     "this encodes the model's scientific intent -- what may be compared with "
     "what, which context must agree, which measurement is admissible. A "
     "plausible coercion here is exactly the failure to avoid, so it is a "
     "question with the rule, the subjects and the schema's own comment "
     "attached"),
    (None, r"^(materials: )?(ACYCLIC|HISTORY_DAG|ANALYSIS_DAG|PLAN_RUN|"
           r"ONE_PRODUCER|ONE_CONSUMER|ORDER|OBS_TIME|OBS_STATE|STATE_TIME|"
           r"TIME|INTERVAL|NO_SELF_STATE|PARTICIPATION|DESTRUCTIVE|"
           r"AGGREGATION|STEP_KIND|DIMENSION|PROPERTY_DIMENSION|"
           r"CONDITION_DIMENSION|QUALIFIER_UNIQUE)$",
     "R5_DOMAIN_PROVENANCE", "escalate", "unknown",
     "a history, timing or dimensional rule. These describe how the model "
     "believes an experiment is shaped, and a guess at one quietly rewrites "
     "the science"),
]

_COMPILED = [(ch, re.compile(pat), cls, dec, conf, why)
             for ch, pat, cls, dec, conf, why in _RULES]

UNCLASSIFIED = Verdict(
    "UNCLASSIFIED", "escalate", "unknown",
    "no entry in the taxonomy matches this. Escalate: an unrecognised "
    "rejection must never be treated as a clean result or quietly fixed")


# A wrapped exception names where it surfaced, not what it is. The sealing
# step re-raises whatever admit() raised, so "No owner-approved schema for this
# profile" arrived as `seal_failed: ValueError: No owner-approved...` and was
# classified by the wrapper as a payload data_fix -- sending a person to edit
# data when what is needed is an owner approval. Unwrap before classifying.
_WRAPPED = re.compile(r"^(?:[A-Za-z_][A-Za-z0-9_.]*(?:Error|Exception)): (.+)$",
                      re.S)


def unwrap(message: str) -> str:
    """Strip one layer of `ExceptionType: ` from a re-raised message."""
    seen = set()
    out = (message or "").strip()
    while out not in seen:
        seen.add(out)
        m = _WRAPPED.match(out)
        if not m:
            break
        out = m.group(1).strip()
    return out


def classify(rejection: dict) -> Verdict:
    """Classify one rejection from probe.py. Never raises."""
    channel = rejection.get("channel") or ""
    label = rejection.get("rule_label")
    subject = label or unwrap(rejection.get("message") or "")
    pointer = rejection.get("json_pointer") or ""
    for want_channel, pattern, cls, decision, confidence, why in _COMPILED:
        if want_channel is not None:
            # `json_pointer:/x` matches on where in the document the problem
            # is, which is sometimes the only thing that identifies it.
            if want_channel.startswith("json_pointer:"):
                if not pointer.startswith(want_channel.split(":", 1)[1]):
                    continue
            elif want_channel != channel:
                continue
        if pattern.search(subject):
            return Verdict(cls, decision, confidence, why)
    return Verdict(UNCLASSIFIED["class"], UNCLASSIFIED["decision_type"],
                   UNCLASSIFIED["confidence"], UNCLASSIFIED["why"])


def classify_label(label: str) -> Verdict:
    """Classify a bare SQL rule label, for the exhaustiveness test."""
    return classify({"channel": "sql_reject_if", "rule_label": label,
                     "message": f"materials: {label}"})


def may_propose(verdict: dict) -> bool:
    """Whether the agent may write a proposal rather than a question.

    The one guard the rest of the design leans on. `unknown` means the value
    is the experimenter's, and a proposal there would be a guess wearing the
    clothes of an answer.
    """
    return verdict.get("confidence") in ("mechanical", "inferred")
