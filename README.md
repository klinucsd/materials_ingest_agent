# materials_ingest_agent

An agent that loads materials-science data into a strict relational schema and,
when the data does not fit, **writes down a specific question or a specific
proposed fix for a person to approve** — rather than failing silently or
guessing at what was meant.

Early work. At present this repository holds the rule-extraction step and its
tests; the agent itself is not built yet. See *Status* below.

## The problem

The target schema is deliberately uncompromising: it refuses data whose units,
measurement technique or missing-value convention it has not been taught,
instead of accepting a plausible interpretation. That is the right choice —
a temperature in Celsius loaded into a field expecting Kelvin is still a
number, still looks reasonable, and is wrong by 273 degrees.

The consequence is that loading real experimental data produces a wall of
refusals, and there is no mechanism for involving a person in resolving them.
Loading is all-or-nothing in a single transaction, and the enforcement rules
are evaluated at commit: the first rule to fail aborts the whole load and
reports that one failure. Fix it, try again, learn the next one.

This tool sits between a dataset and the schema and does three things:

1. **Finds every problem before changing anything**, so a person is asked one
   batch of questions rather than one question per attempt.
2. **Separates what it can determine from what it cannot.** Where the correct
   value follows from the rules, it proposes the exact edit. Where the value is
   a fact about the experiment — an out-of-range reading that might be genuine,
   a sign convention, a unit nobody stated — it asks. *A plausible guess at a
   measurement is the failure this exists to prevent.*
3. **Never approves its own work.** Approval is a privileged, append-only act
   in the target database. The agent requests; a person performs it; and the
   agent's evidence that it happened is the database, not its own record of
   having asked.

## The schema and ingestion kit are not in this repository

They are a collaborator's unreleased work, shared with us directly, and are not
ours to redistribute. `data/` is excluded wholesale by `.gitignore`, and a
pre-commit hook refuses to stage anything from it.

To run anything that needs them, point `MIA_KIT_DIR` at a local copy:

```sh
export MIA_KIT_DIR=/path/to/kit      # so $MIA_KIT_DIR/model/postgresql/schema.sql exists
```

Everything in this repository is code we wrote. It reads that material without
containing it.

## What works now

`code/extract_rules.py` recovers the schema's enforcement rules — each as a
label and the SQL expression behind it — so they can be evaluated in one pass
against staged rows and rolled back, instead of one per failed transaction.
That batching is what makes a single round of questions possible, and it was
the project's one load-bearing assumption.

```sh
python code/extract_rules.py                      # uses MIA_KIT_DIR
python code/extract_rules.py path/to/schema.sql -o rules.json
```

It is a lexical scanner rather than a regular expression, for a reason worth
knowing if you touch it: the rule call sites live inside `$$`-quoted PL/pgSQL
function bodies. Treating `$$…$$` as a string literal and skipping it — the
syntactically correct reading — finds **exactly zero** call sites. The scanner
steps over the marker and keeps going, while still treating single-quoted
literals inside as opaque.

Measured against the current kit: every rule predicate is a self-contained
`EXISTS(...)` expression, none references trigger row variables or transition
tables, and all of them evaluate standalone. The batched approach holds with no
exceptions.

## Tests

```sh
python -m pytest tests/ -q
```

The suite runs in a fresh clone with no access to the kit: the fixtures are
written in the test file and cover the lexical cases that actually broke —
dollar-quoted bodies, tagged dollar quotes, commas at depth, literals
containing parentheses, doubled quotes, comments, and the function's own
declaration not being a call site. Three further tests assert properties of the
real schema and are skipped unless `MIA_KIT_DIR` is set.

## Status

| | |
|---|---|
| Rule extraction and tests | **done** |
| Isolated test database and a hand-driven load | next |
| Batched problem-finding | after that |
| Question-vs-proposal classification | |
| The review step a person uses to answer | built before the automated part |
| The agent loop | |

The early stages deliberately produce a working, auditable loader with no
automation in it at all. If the agent were removed, a person would still have a
tool that works — and it makes the agent's contribution measurable rather than
assumed.

## Layout

```
code/      the tool, and the pre-commit hook
tests/     runnable without the kit
data/      not in git: the kit, working bundles, decision records
docs/      not in git: internal planning
```

Install the hook after cloning:

```sh
ln -sf ../../code/pre-commit .git/hooks/pre-commit
```
