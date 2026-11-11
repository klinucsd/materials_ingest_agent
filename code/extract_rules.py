"""Pull the `reject_if` rule predicates out of the kit's schema.sql.

Why this exists
---------------
The schema enforces its model with ~558 calls of the form

    PERFORM reject_if(EXISTS(SELECT ... ), 'SOME_RULE_LABEL');

inside deferred constraint triggers. They fire at COMMIT, and the first one to
be true aborts the whole transaction -- so an ingestion that violates twelve
rules reports one, twelve times in a row, each costing a full staged
transaction.

If the predicates are self-contained SQL expressions, we can stage the rows in
one transaction, evaluate every predicate as a plain `SELECT <expr>`, collect
all the true ones, and roll back. One round trip, every violation. That turns
an agent that asks one question per attempt into one that asks a batch.

Whether they ARE self-contained is the open question this module exists to
settle: a predicate referencing a PL/pgSQL local or a trigger transition table
cannot be evaluated standalone. `classify` answers it against a live database.

A regex cannot do the extraction. Predicates span lines, nest parentheses, and
contain quoted literals that themselves contain parentheses and commas, so the
scanner below tracks SQL lexical state properly.
"""
from __future__ import annotations

import json
import os
import re
import sys
from pathlib import Path

CALL = "reject_if"


def _skip_to_end_of_dollar_quote(sql: str, i: int) -> int | None:
    """If a dollar-quoted string opens at i, return the index just past it."""
    m = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$").match(sql, i)
    if not m:
        return None
    close = sql.find(m.group(0), m.end())
    return len(sql) if close == -1 else close + len(m.group(0))


def _split_top_level(args: str) -> tuple[str, str] | None:
    """Split `predicate, label` on the LAST top-level comma.

    The last one, not the first: the predicate is full of commas at depth.
    """
    depth = 0
    i = 0
    last = -1
    n = len(args)
    while i < n:
        c = args[i]
        if c == "'":
            i += 1
            while i < n:
                if args[i] == "'":
                    if i + 1 < n and args[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        if c == "$":
            j = _skip_to_end_of_dollar_quote(args, i)
            if j is not None:
                i = j
                continue
        if c in "([":
            depth += 1
        elif c in ")]":
            depth -= 1
        elif c == "," and depth == 0:
            last = i
        i += 1
    if last == -1:
        return None
    return args[:last].strip(), args[last + 1:].strip()


def extract(sql: str) -> list[dict]:
    """Every reject_if call site, as {line, label, predicate}."""
    out: list[dict] = []
    i = 0
    n = len(sql)
    while i < n:
        c = sql[i]

        # -- line comment
        if c == "-" and sql.startswith("--", i):
            nl = sql.find("\n", i)
            i = n if nl == -1 else nl + 1
            continue
        # /* block comment */ (SQL allows nesting)
        if c == "/" and sql.startswith("/*", i):
            depth = 1
            i += 2
            while i < n and depth:
                if sql.startswith("/*", i):
                    depth += 1
                    i += 2
                elif sql.startswith("*/", i):
                    depth -= 1
                    i += 2
                else:
                    i += 1
            continue
        # 'string literal'
        if c == "'":
            i += 1
            while i < n:
                if sql[i] == "'":
                    if i + 1 < n and sql[i + 1] == "'":
                        i += 2
                        continue
                    break
                i += 1
            i += 1
            continue
        # $$ dollar-quoted marker. Step over the MARKER ONLY and keep
        # scanning: in this file the dollar-quoted bodies are PL/pgSQL
        # function bodies, and every reject_if call site lives inside one.
        # Skipping the body wholesale -- the obvious reading of "it's a
        # string literal" -- finds exactly zero call sites.
        if c == "$":
            m = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$").match(sql, i)
            if m:
                i = m.end()
                continue

        # reject_if( ... ) -- must be a whole identifier, not a suffix
        if sql.startswith(CALL, i):
            before = sql[i - 1] if i else " "
            after = sql[i + len(CALL):i + len(CALL) + 1]
            if not (before.isalnum() or before == "_") and after == "(":
                start = i + len(CALL) + 1
                depth = 1
                k = start
                while k < n and depth:
                    ch = sql[k]
                    if ch == "'":
                        k += 1
                        while k < n:
                            if sql[k] == "'":
                                if k + 1 < n and sql[k + 1] == "'":
                                    k += 2
                                    continue
                                break
                            k += 1
                    elif ch == "$":
                        m = re.compile(r"\$([A-Za-z_][A-Za-z0-9_]*)?\$").match(sql, k)
                        if m:
                            k = m.end()
                            continue
                    elif ch == "(":
                        depth += 1
                    elif ch == ")":
                        depth -= 1
                        if depth == 0:
                            break
                    k += 1
                parts = _split_top_level(sql[start:k])
                if parts:
                    predicate, label_expr = parts
                    label = label_expr.strip()
                    if len(label) >= 2 and label[0] == "'" and label[-1] == "'":
                        label = label[1:-1].replace("''", "'")
                    out.append({
                        "line": sql.count("\n", 0, i) + 1,
                        "label": label,
                        "predicate": " ".join(predicate.split()),
                    })
                i = k + 1
                continue
        i += 1
    # The CREATE FUNCTION itself is a definition, not a call site.
    return [r for r in out if r["predicate"] not in ("bad boolean", "bad")]


def default_schema() -> Path | None:
    """The kit's schema.sql, from MIA_KIT_DIR.

    The kit is a collaborator's unreleased material and is not in this
    repository -- see README.md. Point MIA_KIT_DIR at a local copy.
    """
    root = os.environ.get("MIA_KIT_DIR")
    if not root:
        return None
    p = Path(root).expanduser() / "model" / "postgresql" / "schema.sql"
    return p if p.is_file() else None


def main(argv: list[str]) -> int:
    args = [a for a in argv[1:] if not a.startswith("-")]
    target = Path(args[0]) if args else default_schema()
    if target is None or not target.is_file():
        print(f"usage: {Path(argv[0]).name} <schema.sql> [-o out.json]")
        print()
        print("  The schema is not in this repository: it belongs to a")
        print("  collaborator and is unreleased. Either pass its path, or set")
        print("  MIA_KIT_DIR to the directory holding the kit, so that")
        print("  $MIA_KIT_DIR/model/postgresql/schema.sql exists.")
        if os.environ.get("MIA_KIT_DIR"):
            print(f"\n  MIA_KIT_DIR is set to {os.environ['MIA_KIT_DIR']!r}")
            print("  but no schema.sql was found under it.")
        return 2
    sql = target.read_text(errors="replace")
    rules = extract(sql)

    labels = [r["label"] for r in rules]
    distinct = sorted(set(labels))
    print(f"  call sites        {len(rules)}")
    print(f"  distinct labels   {len(distinct)}")

    # A label whose predicate text differs between sites is a label that cannot
    # be reported on its own -- worth knowing before we key anything on it.
    by_label: dict[str, set[str]] = {}
    for r in rules:
        by_label.setdefault(r["label"], set()).add(r["predicate"])
    multi = {k: v for k, v in by_label.items() if len(v) > 1}
    print(f"  labels reused with a DIFFERENT predicate  {len(multi)}")

    families: dict[str, int] = {}
    for lab in labels:
        families[lab.split()[0].split("_")[0]] = \
            families.get(lab.split()[0].split("_")[0], 0) + 1
    print("  by label prefix:")
    for k, v in sorted(families.items(), key=lambda kv: -kv[1])[:14]:
        print(f"    {k:<16} {v:>4}")

    if "-o" in argv:
        out = Path(argv[argv.index("-o") + 1])
        out.write_text(json.dumps(rules, indent=1) + "\n")
        print(f"  wrote {out} ({out.stat().st_size:,} bytes)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
