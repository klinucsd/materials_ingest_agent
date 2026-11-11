"""Tests for the rule-predicate scanner.

These run in a fresh clone with no access to the collaborator's kit: the
fixtures below are written here and exercise the lexical cases that actually
broke during development. The two tests that need the real schema are skipped
unless MIA_KIT_DIR points at a copy of it.

Run:  python -m pytest tests/ -q
"""
import os
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "code"))
from extract_rules import extract  # noqa: E402


def labels(sql):
    return [r["label"] for r in extract(sql)]


def test_the_case_that_found_nothing():
    """Call sites inside a $$-quoted body must be found.

    This is the bug the scanner was written twice for. Reading `$$...$$` as a
    string literal -- which it is, syntactically -- and skipping it finds
    exactly zero call sites, because every one of them lives inside a PL/pgSQL
    function body. The scanner steps over the MARKER and keeps going.
    """
    sql = """
    CREATE FUNCTION enforce() RETURNS trigger LANGUAGE plpgsql AS $$
    BEGIN
    PERFORM reject_if(EXISTS(SELECT FROM entity), 'INSIDE_DOLLARS');
    RETURN NULL; END $$;
    """
    assert labels(sql) == ["INSIDE_DOLLARS"]


def test_tagged_dollar_quote():
    sql = """
    CREATE FUNCTION f() RETURNS void AS $body$
    PERFORM reject_if(EXISTS(SELECT 1), 'TAGGED');
    $body$ LANGUAGE plpgsql;
    """
    assert labels(sql) == ["TAGGED"]


def test_the_definition_is_not_a_call_site():
    """`CREATE FUNCTION reject_if(bad boolean, rule text)` is a declaration."""
    sql = """
    CREATE FUNCTION reject_if(bad boolean, rule text) RETURNS void
    LANGUAGE plpgsql AS $$ BEGIN IF bad THEN RAISE EXCEPTION; END IF; END $$;
    """
    assert labels(sql) == []


def test_commas_at_depth_do_not_split_the_arguments():
    """The label is after the LAST top-level comma, not the first."""
    sql = ("PERFORM reject_if(EXISTS(SELECT FROM t WHERE f(a,b,c) "
           "AND g(h(i,j),k)), 'MANY_COMMAS');")
    rows = extract(sql)
    assert len(rows) == 1
    assert rows[0]["label"] == "MANY_COMMAS"
    assert rows[0]["predicate"].startswith("EXISTS(SELECT FROM t")
    assert rows[0]["predicate"].endswith("g(h(i,j),k))")


def test_a_literal_holding_a_paren_or_comma_is_opaque():
    sql = "PERFORM reject_if(EXISTS(SELECT FROM t WHERE x='a,b)('), 'QUOTED');"
    rows = extract(sql)
    assert len(rows) == 1
    assert rows[0]["label"] == "QUOTED"
    assert "'a,b)('" in rows[0]["predicate"]


def test_doubled_quote_inside_a_literal():
    sql = "PERFORM reject_if(EXISTS(SELECT FROM t WHERE x='it''s'), 'ESCAPED');"
    assert labels(sql) == ["ESCAPED"]


def test_a_label_with_a_doubled_quote_is_unescaped():
    sql = "PERFORM reject_if(TRUE, 'IT''S A RULE');"
    assert labels(sql) == ["IT'S A RULE"]


def test_comments_are_skipped():
    sql = """
    -- PERFORM reject_if(TRUE, 'IN_LINE_COMMENT');
    /* PERFORM reject_if(TRUE, 'IN_BLOCK_COMMENT'); */
    PERFORM reject_if(TRUE, 'REAL');
    """
    assert labels(sql) == ["REAL"]


def test_identifier_boundary():
    """`my_reject_if(` is a different function."""
    sql = """
    PERFORM my_reject_if(TRUE, 'NOT_OURS');
    PERFORM reject_if(TRUE, 'OURS');
    """
    assert labels(sql) == ["OURS"]


def test_multiline_predicate_keeps_its_line_number():
    sql = "\n\n\nPERFORM reject_if(EXISTS(\n  SELECT FROM t\n), 'SPANNING');"
    rows = extract(sql)
    assert rows[0]["line"] == 4
    assert rows[0]["predicate"] == "EXISTS( SELECT FROM t )"


# --------------------------------------------------------------- real kit ---
KIT = os.environ.get("MIA_KIT_DIR")
SCHEMA = Path(KIT).expanduser() / "model" / "postgresql" / "schema.sql" if KIT else None
needs_kit = pytest.mark.skipif(
    not (SCHEMA and SCHEMA.is_file()),
    reason="set MIA_KIT_DIR to a copy of the kit (not in this repository)")


@needs_kit
def test_the_real_schema_yields_what_the_spike_measured():
    """Guards against a kit update silently changing the rule surface.

    557/552 were measured on 2026-11-11. A change here is not necessarily a
    failure -- the kit may have moved -- but it should be noticed and the
    taxonomy rechecked, not discovered later.
    """
    rows = extract(SCHEMA.read_text(errors="replace"))
    assert len(rows) == 557, f"call sites moved: {len(rows)}"
    assert len({r["label"] for r in rows}) == 552


@needs_kit
def test_every_real_predicate_is_a_self_contained_expression():
    """The property the batched dry run depends on.

    Every predicate must be a bare EXISTS(...) with no reference to a trigger
    row variable or transition table, or it cannot be evaluated outside the
    trigger that holds it.
    """
    rows = extract(SCHEMA.read_text(errors="replace"))
    not_exists = [r["label"] for r in rows if not r["predicate"].startswith("EXISTS(")]
    assert not_exists == [], f"not an EXISTS expression: {not_exists[:5]}"

    import re
    bad = [r["label"] for r in rows
           if re.search(r"\b(NEW|OLD|TG_[A-Z]+)\b", r["predicate"])]
    assert bad == [], f"references trigger context: {bad[:5]}"


@needs_kit
def test_no_label_carries_two_different_predicates():
    """Decision records are keyed on the label, so it has to be unambiguous."""
    rows = extract(SCHEMA.read_text(errors="replace"))
    seen: dict[str, str] = {}
    clashes = []
    for r in rows:
        prev = seen.setdefault(r["label"], r["predicate"])
        if prev != r["predicate"]:
            clashes.append(r["label"])
    assert clashes == [], f"label reused with a different predicate: {clashes}"
