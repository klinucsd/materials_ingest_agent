"""Load the collaborator's sixteen known-good example bundles. No agent.

This is the T2 milestone: prove our driver reaches the same committed state the
kit's own verification suite asserts — 16 retained artifacts and 48 dataset
bindings — before any agent, classifier or decision record exists. If we cannot
load data that is known good, the fault is ours and not the data's, and that
makes every later failure much easier to place.

Two things it does differently from `kit.py ingest`, both deliberate:

**It loads from copies.** The kit is write-protected and its digests are part of
the validator fingerprint, so nothing may touch it. Bundles are copied to
data/bundles/ first and loaded from there. That is also where the agent will
later apply approved patches.

**It runs as the validator role, not as the superuser.** `kit.py ingest`
connects as whoever PGUSER names, which in practice is the owner — so it never
tests the privilege boundary the provisioning step just set up. Here the role is
switched inside an outer transaction, which is what the kit's own verification
does. Note that `ingest_bundle` opens its own nested transaction: the deferred
constraint triggers therefore fire when the OUTER one commits, and that nesting
is load-bearing. Do not flatten it.

Usage:
    source code/pgenv.sh
    .venv-kit/bin/python code/load_examples.py [--family xray_diffraction ...]
"""
from __future__ import annotations

import argparse
import os
import shutil
import sys
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
ROOT = HERE.parent

KIT = Path(os.environ.get("MIA_KIT_DIR") or ROOT / "data" / "kit")
MODEL = KIT / "model"
WORKED = MODEL / "characterization" / "worked"
BUNDLES = ROOT / "data" / "bundles"

# The kit's own import contract, from kit.py. Only the kit's modules are put on
# the path; nothing of ours is importable from inside it.
sys.path[:0] = [str(MODEL), str(MODEL / "characterization")]

# What the kit's verification suite asserts for a full load of the examples.
EXPECT_BLOBS = 16
EXPECT_BINDINGS = 48          # one raw + one processed + one feature per family

VALIDATOR_ROLE = os.environ.get("MIA_VALIDATOR_ROLE", "mia_validator")


def families() -> list[str]:
    return sorted(p.name for p in WORKED.iterdir()
                  if p.is_dir() and (p / "manifest.json").is_file())


def working_copy(family: str) -> Path:
    """A writable copy of one bundle, refreshed from the kit each run."""
    dst = BUNDLES / family
    if dst.exists():
        shutil.rmtree(dst)
    dst.parent.mkdir(parents=True, exist_ok=True)
    shutil.copytree(WORKED / family, dst)
    # copytree preserves the kit's read-only bits; the agent needs to patch here.
    for p in [dst, *dst.rglob("*")]:
        p.chmod(p.stat().st_mode | 0o200)
    return dst


def counts(conn) -> dict[str, int]:
    out = {}
    for table in ("payload_schema", "payload_blob", "payload_binding", "entity"):
        out[table] = conn.execute(
            f"SELECT count(*) FROM materials.{table}").fetchone()[0]
    return out


def main(argv: list[str]) -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--family", action="append", default=None,
                    help="load only these (default: all sixteen)")
    ap.add_argument("--as-owner", action="store_true",
                    help="skip the role switch and load as PGUSER, like "
                         "kit.py ingest does. Does not test the boundary.")
    a = ap.parse_args(argv[1:])

    if not WORKED.is_dir():
        print(f"  no worked examples at {WORKED}")
        print("  The kit is not in this repository; set MIA_KIT_DIR.")
        return 2

    import psycopg
    from psycopg import sql
    from ingestion import ingest_bundle, implementation_digest

    wanted = a.family or families()
    print(f"  kit        {KIT}")
    print(f"  validator  {implementation_digest()[:16]}…  "
          f"(python {sys.version.split()[0]})")
    print(f"  role       {'PGUSER (owner)' if a.as_owner else VALIDATOR_ROLE}")
    print(f"  bundles    {len(wanted)}")
    print()

    ok, failed = [], []
    with psycopg.connect(autocommit=True) as conn:
        before = counts(conn)
        for family in wanted:
            bundle = working_copy(family)
            t0 = time.monotonic()
            try:
                # The role switch and ingest_bundle's own transaction nest:
                # deferred triggers fire when this outer block commits.
                with conn.transaction():
                    if not a.as_owner:
                        conn.execute(sql.SQL("SET LOCAL ROLE {}")
                                     .format(sql.Identifier(VALIDATOR_ROLE)))
                    datasets = ingest_bundle(conn, bundle)
                dt = time.monotonic() - t0
                ok.append(family)
                print(f"  ok    {family:<30} {len(datasets)} dataset(s)  {dt:4.1f}s")
            except Exception as exc:                         # noqa: BLE001
                dt = time.monotonic() - t0
                failed.append((family, f"{type(exc).__name__}: {exc}"))
                print(f"  FAIL  {family:<30} {dt:4.1f}s")
                print(f"        {type(exc).__name__}: {str(exc)[:160]}")
        after = counts(conn)

    print()
    print("  table              before   after")
    for table in ("payload_schema", "payload_blob", "payload_binding", "entity"):
        print(f"  {table:<18} {before[table]:>6}  {after[table]:>6}")

    print()
    expected_full = sorted(wanted) == families()
    problems = []
    if failed:
        problems.append(f"{len(failed)} bundle(s) failed")
    if expected_full:
        # Only meaningful for a complete load of a clean database.
        if before["payload_blob"] == 0 and after["payload_blob"] != EXPECT_BLOBS:
            problems.append(f"payload_blob {after['payload_blob']}, "
                            f"expected {EXPECT_BLOBS}")
        if before["payload_binding"] == 0 and \
                after["payload_binding"] != EXPECT_BINDINGS:
            problems.append(f"payload_binding {after['payload_binding']}, "
                            f"expected {EXPECT_BINDINGS}")

    if problems:
        print("  NOT the expected state:")
        for p in problems:
            print(f"    {p}")
        for family, err in failed:
            print(f"    {family}: {err[:200]}")
        return 1

    print(f"  {len(ok)} of {len(wanted)} loaded"
          + (f"; {EXPECT_BLOBS} artifacts and {EXPECT_BINDINGS} bindings, "
             "matching the kit's own assertions" if expected_full else ""))
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
