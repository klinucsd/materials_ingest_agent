"""Find every reason a bundle would be rejected, in one pass.

The ingestion path reports one rejection per attempt. Its enforcement rules are
deferred to COMMIT, so the first true one aborts the whole transaction and you
learn about that one; its payload validation calls `validate()` rather than
`iter_errors()`, so one schema error surfaces at a time. A dataset with a dozen
problems therefore takes a dozen staged transactions to describe, and an agent
built on that behaviour asks one question per round trip.

This collects them all instead. Every stage accumulates rather than raising, and
the SQL stage stages the rows, evaluates all 557 rule predicates, and rolls
back — so nothing is committed and nothing is lost.

    probe.py dryrun <bundle> [--db] [--json]
    probe.py env [--assert-lock]
    probe.py approvals
    probe.py rules --extract [-o out.json]

Output is a JSON array of rejection objects, each with a stage, a channel, the
verbatim message, and whatever locating detail that stage can give. It makes no
judgement about what any of them mean: classification is taxonomy.py's job, and
deciding is a person's.

This is the ONLY module that imports the collaborator's kit. Nothing with an
LLM in it goes near these dependencies, because the schema fingerprints the
installed jsonschema version against every approval it holds.
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent
KIT = Path(os.environ.get("MIA_KIT_DIR") or ROOT / "data" / "kit")
MODEL = KIT / "model"

if not (MODEL / "characterization").is_dir():
    sys.exit(
        f"  no kit at {KIT}\n"
        f"  The schema and ingestion kit are a collaborator's unreleased work\n"
        f"  and are not in this repository. Set MIA_KIT_DIR to a local copy,\n"
        f"  so that $MIA_KIT_DIR/model/postgresql/schema.sql exists.")

sys.path[:0] = [str(MODEL), str(MODEL / "characterization")]

MAX_BYTES = 16777216          # the kit's own per-artifact limit


def reject(stage, channel, message, **extra):
    # Messages are flattened to one line. jsonschema's carry an indented
    # multi-line explanation, which turns any grouped display into noise and
    # truncates to something like "Failed valida".
    r = {"stage": stage, "channel": channel,
         "message": " ".join(str(message).split())}
    r.update({k: v for k, v in extra.items() if v is not None})
    return r


def dedupe(rejections):
    """One row per distinct defect, with the datasets it showed up under.

    The three datasets of a bundle point at the SAME payload artifact by
    design -- raw, processed and features are sections of one document. So a
    single missing unit is found once per dataset, and a run of a bundle broken
    in one place reported it five times. That is one defect, and a person
    asked about it five times rightly loses confidence in the asker.
    """
    seen, order = {}, []
    for r in rejections:
        key = (r["stage"], r["channel"], r["message"],
               r.get("json_pointer"), r.get("rule_label"))
        if key in seen:
            prev = seen[key]
            prev["occurrences"] = prev.get("occurrences", 1) + 1
            for field in ("dataset", "artifact"):
                if r.get(field):
                    bag = prev.setdefault(field + "s", [])
                    if r[field] not in bag:
                        bag.append(r[field])
            prev.pop(field, None) if False else None
            continue
        seen[key] = dict(r)
        order.append(key)
    out = []
    for key in order:
        r = seen[key]
        # Where a defect spans datasets, carry the list and drop the singular.
        if r.get("datasets"):
            first = r.pop("dataset", None)
            if first and first not in r["datasets"]:
                r["datasets"].insert(0, first)
        out.append(r)
    return out


# ----------------------------------------------------------------- stage 1 --
def stage_bundle_shape(bundle: Path):
    """Manifest and payload files. Every problem, not the first.

    The kit's own loop raises on the first bad path; this reports all of them,
    so a manifest with three broken entries is three rejections in one pass.
    """
    out, manifest = [], None
    mpath = bundle / "manifest.json"
    if not mpath.is_file():
        return [reject("bundle", "missing_file", "no manifest.json in bundle",
                       path=str(mpath))], None, {}
    try:
        from characterization.documents import parse
        manifest = parse(mpath.read_text(encoding="utf-8"))
    except Exception as exc:                                 # noqa: BLE001
        return [reject("bundle", "python_valueerror",
                       f"{type(exc).__name__}: {exc}", path="manifest.json")], None, {}

    contents = {}
    for artifact, rel in (manifest or {}).items():
        target = (bundle / rel).resolve()
        if not target.is_relative_to(bundle.resolve()):
            out.append(reject("bundle", "python_valueerror",
                              "Payload path escapes bundle",
                              artifact=artifact, path=rel))
            continue
        if not target.exists():
            out.append(reject("bundle", "missing_file",
                              "Missing payload file named in the manifest",
                              artifact=artifact, path=rel))
            continue
        if not target.is_file():
            out.append(reject("bundle", "python_valueerror",
                              "Payload must be an ordinary operator-owned file",
                              artifact=artifact, path=rel))
            continue
        size = target.stat().st_size
        if not 0 < size <= MAX_BYTES:
            out.append(reject("bundle", "python_valueerror", "PAYLOAD_SIZE",
                              artifact=artifact, path=rel, bytes=size,
                              limit=MAX_BYTES))
            continue
        contents[artifact] = target.read_bytes()
    return out, manifest, contents


# ----------------------------------------------------------------- stage 2 --
def stage_graph(bundle: Path):
    """Parse records.ttl, then compile it to rows. Single errors by nature."""
    from rdflib import Graph
    tpath = bundle / "records.ttl"
    if not tpath.is_file():
        return [reject("graph", "missing_file", "no records.ttl in bundle",
                       path=str(tpath))], None, None
    try:
        graph = Graph().parse(tpath, format="turtle")
    except Exception as exc:                                 # noqa: BLE001
        return [reject("graph", "turtle_parse",
                       f"{type(exc).__name__}: {exc}")], None, None

    # verify.compiled() raises on the first NO_IDENTITY_COLLAPSE / IRI-required
    # problem. An accumulating mirror of it is a later refinement; for now a
    # failure here is reported as the one rejection it is, because without rows
    # there is nothing to stage and the SQL stage cannot run at all.
    import importlib.util
    spec = importlib.util.spec_from_file_location(
        "controlled_rows", MODEL / "postgresql" / "verify.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    try:
        rows = mod.compiled(graph)
    except Exception as exc:                                 # noqa: BLE001
        return [reject("graph", "python_valueerror",
                       f"{type(exc).__name__}: {exc}")], graph, None
    return [], graph, (mod, rows)


# ----------------------------------------------------------------- stage 3 --
def stage_graph_semantics(graph):
    """The CHAR_JSON_* rules, offline, from the kit's own checker.

    `documents.graph_check` already returns the complete set rather than the
    first failure, so this stage is free: seven rules, no database, his code.
    """
    if graph is None:
        return []
    try:
        from characterization.documents import graph_check
        from rdflib import Namespace
        m = Namespace("https://w3id.org/feder/materials/proposal/0.1/")
        return [reject("graph_semantics", "sql_rule_offline", label)
                for label in sorted(graph_check(graph, m))]
    except Exception as exc:                                 # noqa: BLE001
        return [reject("graph_semantics", "probe_error",
                       f"graph_check itself failed: {type(exc).__name__}: {exc}")]


# ----------------------------------------------------------------- stage 4 --
def stage_payload(graph, contents, conn=None):
    """Every payload error, not the first.

    Two differences from the ingestion path. It uses `iter_errors` instead of
    `validate`, so all schema violations come back together. And when a
    database is available it validates against the ENROLLED schema bytes --
    the ones `admit()` will actually use -- rather than the packaged file, and
    says which it used either way.
    """
    out = []
    if graph is None or not contents:
        return out
    from rdflib import Namespace, RDF
    from jsonschema import Draft202012Validator
    from characterization.documents import parse
    from build_worked_examples import semantic_errors

    m = Namespace("https://w3id.org/feder/materials/proposal/0.1/")
    roles = {"SignalDataset": "/raw", "ProcessedDataset": "/processed",
             "FeatureCollection": "/features"}
    datasets = []
    for cls in roles:
        for s in graph.subjects(RDF.type, m[cls]):
            datasets.append((str(s), cls))

    for dataset_iri, cls in sorted(datasets):
        from rdflib import URIRef
        artifact = graph.value(URIRef(dataset_iri), m["payloadArtifact"])
        profile = graph.value(URIRef(dataset_iri), m["payloadProfile"])
        if artifact is None:
            out.append(reject("payload", "missing_link",
                              "dataset names no payloadArtifact",
                              dataset=dataset_iri, dataset_class=cls))
            continue
        body = contents.get(str(artifact))
        if body is None:
            out.append(reject("payload", "python_valueerror",
                              "Missing explicit artifact in bundle manifest",
                              dataset=dataset_iri, artifact=str(artifact)))
            continue

        try:
            doc = parse(body.decode("utf-8"))
        except Exception as exc:                             # noqa: BLE001
            out.append(reject("payload", "python_valueerror",
                              f"{type(exc).__name__}: {exc}",
                              dataset=dataset_iri, artifact=str(artifact)))
            continue

        schema, source = _schema_for(doc, profile, conn)
        if schema is None:
            out.append(reject("payload", "no_approved_schema", source,
                              dataset=dataset_iri,
                              profile=str(profile) if profile else None))
            continue

        for err in sorted(Draft202012Validator(schema).iter_errors(doc),
                          key=lambda e: list(e.absolute_path)):
            out.append(reject("payload", "jsonschema", err.message,
                              dataset=dataset_iri,
                              json_pointer="/" + "/".join(
                                  str(p) for p in err.absolute_path),
                              validator=err.validator,
                              validator_value=_small(err.validator_value),
                              schema_source=source))
        for msg in semantic_errors(doc):
            out.append(reject("payload", "payload_semantics", msg,
                              dataset=dataset_iri, schema_source=source))
    return out


def _small(v):
    """Keep a schema's expected value, unless it is bulk."""
    s = json.dumps(v) if not isinstance(v, str) else v
    return v if len(s) <= 200 else f"<{type(v).__name__}, {len(s)} chars>"


def _schema_for(doc, profile, conn):
    """The enrolled schema bytes if the database has them, else the packaged file."""
    if conn is not None and profile is not None:
        row = conn.execute(
            "SELECT schema_bytes FROM materials.payload_schema WHERE profile_id=%s",
            (str(profile),)).fetchone()
        if row is None:
            return None, "No owner-approved schema for this profile"
        from characterization.documents import parse
        return parse(bytes(row[0]).decode("utf-8")), "enrolled"
    family = doc.get("family")
    path = MODEL / "characterization" / "payload-schemas" / f"{family}.schema.json"
    if not path.is_file():
        return None, f"no packaged schema for family {family!r}"
    from characterization.documents import parse
    return parse(path.read_text(encoding="utf-8")), "packaged_file"


# ----------------------------------------------------------------- stage 5 --
def stage_sql_rules(compiled, conn, contents):
    """Stage the rows, seal the payloads, evaluate every rule, roll back.

    This is the stage the whole design rests on, and the reason it works is
    that all 557 predicates are self-contained `EXISTS(...)` expressions --
    measured, see docs. So they can be evaluated as ordinary SELECTs against
    staged rows instead of being waited for at COMMIT, one abort at a time.

    The sealing step is not optional. A first version staged the rows and
    stopped there, and every dry run of a perfectly good bundle reported
    PAYLOAD_REQUIRED -- the rule that every dataset must carry a validation
    receipt. That was true: the probe had not written any. Staging without
    sealing is not a dry run of ingestion, it is a dry run of half of it, and
    the half it skips is the half the gate is about. So the payloads are sealed
    here too, inside the transaction that gets rolled back.
    """
    if compiled is None or conn is None:
        return []
    mod, rows = compiled
    predicates = _predicates()
    if not predicates:
        return [reject("sql_rules", "probe_error",
                       "no rule predicates; run `probe.py rules --extract`")]

    out = []
    try:
        with conn.transaction(force_rollback=True):
            conn.execute("SET LOCAL search_path=materials,pg_catalog,public")
            mod.apply(conn, {}, rows)

            # Seal each payload-bearing dataset, exactly as ingest_bundle
            # does, so the deferred rules see the state a real commit would.
            # A failure here is a rejection in its own right and is reported
            # rather than raised: that is the whole point of the pass.
            # The dataset list, derived exactly as ingest_bundle derives it
            # (ingestion.py:52) rather than guessed at from the graph.
            from ingestion import admit
            datasets = sorted({i for i, t in rows["entity_type"]
                               if t in ("SignalDataset", "ProcessedDataset",
                                        "FeatureCollection")})
            for dataset in datasets:
                try:
                    artifact = conn.execute(
                        "SELECT materials.object_of(%s,'payloadArtifact')",
                        (dataset,)).fetchone()[0]
                    body = contents.get(artifact)
                    if body is None:
                        out.append(reject(
                            "sql_rules", "python_valueerror",
                            "Missing explicit artifact in bundle manifest",
                            dataset=dataset, artifact=artifact))
                        continue
                    admit(conn, dataset, body)
                except Exception as exc:                     # noqa: BLE001
                    out.append(reject("sql_rules", "seal_failed",
                                      f"{type(exc).__name__}: {exc}",
                                      dataset=dataset))

            for rule in predicates:
                try:
                    hit = conn.execute(
                        f"SELECT {rule['predicate']}").fetchone()[0]
                except Exception as exc:                     # noqa: BLE001
                    # Recorded, not hidden: a predicate we cannot evaluate is
                    # a gap in this probe, and the agent must know its list is
                    # incomplete rather than assume it is clean.
                    out.append(reject("sql_rules", "probe_unsupported",
                                      f"{rule['label']}: {type(exc).__name__}: "
                                      f"{str(exc)[:120]}",
                                      rule_label=rule["label"]))
                    continue
                if hit:
                    out.append(reject("sql_rules", "sql_reject_if",
                                      f"materials: {rule['label']}",
                                      rule_label=rule["label"],
                                      sqlstate="23514",
                                      schema_line=rule.get("line")))
    except Exception as exc:                                 # noqa: BLE001
        out.append(reject("sql_rules", "probe_error",
                          f"staging failed: {type(exc).__name__}: {exc}"))
    return out


def _predicates():
    path = ROOT / "data" / "rule_predicates.json"
    if not path.is_file():
        return []
    return json.loads(path.read_text())


# --------------------------------------------------------------- the driver --
def dryrun(bundle: Path, use_db: bool):
    out = []
    shape, _manifest, contents = stage_bundle_shape(bundle)
    out += shape
    graph_errs, graph, compiled = stage_graph(bundle)
    out += graph_errs
    out += stage_graph_semantics(graph)

    conn = None
    if use_db:
        import psycopg
        conn = psycopg.connect(autocommit=True)
    try:
        out += stage_payload(graph, contents, conn)
        out += stage_sql_rules(compiled, conn, contents)
    finally:
        if conn is not None:
            conn.close()
    return dedupe(out)


def cmd_env(assert_lock: bool):
    from importlib.metadata import version, PackageNotFoundError
    lock = ROOT / "kit-venv.lock"
    want = {}
    if lock.is_file():
        for line in lock.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "==" in line:
                n, _, v = line.partition("==")
                want[n.strip().lower().replace("_", "-")] = v.strip()
    drift = []
    for name, ver in want.items():
        try:
            got = version(name)
        except PackageNotFoundError:
            drift.append({"package": name, "have": None, "want": ver})
            continue
        if got != ver:
            drift.append({"package": name, "have": got, "want": ver})
    info = {"python": sys.version, "kit": str(KIT), "locked": len(want),
            "drift": drift}
    try:
        from ingestion import implementation_digest
        info["validator_digest"] = implementation_digest()
    except Exception as exc:                                 # noqa: BLE001
        info["validator_digest_error"] = f"{type(exc).__name__}: {exc}"
    print(json.dumps(info, indent=1))
    if assert_lock and drift:
        print("\n  the validator environment has drifted from kit-venv.lock.",
              file=sys.stderr)
        print("  Approvals already in the database were fingerprinted against",
              file=sys.stderr)
        print("  the locked versions and cannot be remade in place.",
              file=sys.stderr)
        return 1
    return 0


def cmd_approvals():
    import psycopg
    with psycopg.connect(autocommit=True) as conn:
        rows = conn.execute(
            "SELECT profile_id, family::text, technique, contract_version "
            "FROM materials.payload_schema ORDER BY family").fetchall()
    print(json.dumps([{"profile_id": r[0], "family": r[1],
                       "technique": r[2], "contract_version": r[3]}
                      for r in rows], indent=1))
    return 0


def main(argv):
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = ap.add_subparsers(dest="command", required=True)
    d = sub.add_parser("dryrun", help="every reason a bundle would be rejected")
    d.add_argument("bundle", type=Path)
    d.add_argument("--db", action="store_true",
                   help="also evaluate the SQL rules against staged rows")
    d.add_argument("--json", action="store_true", help="machine-readable only")
    e = sub.add_parser("env", help="the validator environment and its digest")
    e.add_argument("--assert-lock", action="store_true",
                   help="exit non-zero if it has drifted from kit-venv.lock")
    sub.add_parser("approvals", help="what the database has approved")
    r = sub.add_parser("rules", help="extract the rule predicates")
    r.add_argument("--extract", action="store_true")
    r.add_argument("-o", "--out", type=Path,
                   default=ROOT / "data" / "rule_predicates.json")
    a = ap.parse_args(argv[1:])

    if a.command == "env":
        return cmd_env(a.assert_lock)
    if a.command == "approvals":
        return cmd_approvals()
    if a.command == "rules":
        from extract_rules import extract, default_schema
        schema = default_schema()
        if schema is None:
            print("  set MIA_KIT_DIR", file=sys.stderr)
            return 2
        rules = extract(schema.read_text(errors="replace"))
        a.out.parent.mkdir(parents=True, exist_ok=True)
        a.out.write_text(json.dumps(rules, indent=1) + "\n")
        print(f"  {len(rules)} rule(s) -> {a.out}")
        return 0

    rejections = dryrun(a.bundle, a.db)
    if a.json:
        print(json.dumps(rejections, indent=1))
    else:
        if not rejections:
            print(f"  {a.bundle.name}: no rejections found"
                  + ("" if a.db else "  (offline; SQL rules not evaluated)"))
        else:
            by_stage = {}
            for r in rejections:
                by_stage.setdefault(r["stage"], []).append(r)
            print(f"  {a.bundle.name}: {len(rejections)} rejection(s)")
            for stage in ("bundle", "graph", "graph_semantics", "payload",
                          "sql_rules"):
                for r in by_stage.get(stage, []):
                    where = r.get("json_pointer") or r.get("rule_label") \
                        or r.get("path") or r.get("dataset", "")
                    n = r.get("occurrences", 1)
                    tag = f" (x{n})" if n > 1 else ""
                    print(f"    {stage:<16} {r['message'][:70]}{tag}")
                    if where:
                        print(f"    {'':<16}   at {str(where)[:68]}")
    return 1 if rejections else 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
