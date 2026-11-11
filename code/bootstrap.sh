#!/usr/bin/env bash
# Stand up an isolated database with the collaborator's schema installed and
# its sixteen example contracts approved.
#
#   code/bootstrap.sh
#
# RUN THIS YOURSELF. It performs the owner approvals, which are a privileged,
# append-only act in the target schema: an approval cannot be updated or
# deleted afterwards, and a changed contract needs a new profile identity. No
# agent reaches this script, and nothing the agent runs can approve anything.
#
# Needs: docker, a code/.env holding POSTGRES_PASSWORD (see .env.example), the
# kit in data/kit or MIA_KIT_DIR, and .venv-kit built from kit-venv.lock.
set -euo pipefail
cd "$(dirname "$0")/.."
HERE=$(pwd)

# shellcheck source=/dev/null
source code/pgenv.sh

KIT="$MIA_KIT_DIR"
PY="$HERE/.venv-kit/bin/python"

say() { printf '\n==> %s\n' "$1"; }
die() { printf '\n!!  %s\n' "$1" >&2; exit 1; }

# ------------------------------------------------------------ preconditions --
[ -f code/.env ] || die "no code/.env -- cp code/.env.example code/.env and set a password"
[ -x "$PY" ] || die "no .venv-kit -- uv venv --python 3.12.12 .venv-kit && uv pip install --python .venv-kit/bin/python -r requirements-kit.txt"
[ -f "$KIT/kit.py" ] || die "no kit at $KIT -- set MIA_KIT_DIR (it is not in this repository)"

say "the validator environment must match kit-venv.lock exactly"
# The schema stores, against every approval, a fingerprint of the environment
# that validated it -- and that fingerprint includes the installed jsonschema
# version. If this drifts, every approval already in the database stops
# matching, and because the approval table is append-only and keyed on the
# profile identity you cannot re-approve: you would have to mint new
# identities. So this is checked before anything is created, not after.
"$PY" - <<'PYCHECK'
import sys, pathlib
from importlib.metadata import version, PackageNotFoundError
want = {}
for line in pathlib.Path("kit-venv.lock").read_text().splitlines():
    line = line.strip()
    if not line or line.startswith("#") or "==" not in line:
        continue
    name, _, ver = line.partition("==")
    want[name.strip().lower().replace("_", "-")] = ver.strip()
bad = []
for name, ver in want.items():
    try:
        got = version(name)
    except PackageNotFoundError:
        bad.append(f"{name}: missing, want {ver}")
        continue
    if got != ver:
        bad.append(f"{name}: have {got}, want {ver}")
if bad:
    print("  environment has drifted from kit-venv.lock:")
    for b in bad:
        print(f"    {b}")
    print("\n  Approvals made under a different environment cannot be reused and")
    print("  cannot be remade in place. Restore the locked versions first:")
    print("    uv pip install --python .venv-kit/bin/python -r kit-venv.lock")
    sys.exit(1)
print(f"  {len(want)} package(s) match")
PYCHECK

say "the kit's own digests"
( cd "$KIT" && PYTHONPATH= "$PY" kit.py verify ) \
  && echo "  all files match MANIFEST.json" \
  || die "kit digests do not match -- do not proceed; the fingerprint hashes these bytes"

# ------------------------------------------------------------------ database --
say "starting postgres:18.6 on 127.0.0.1:8433"
docker compose -f code/docker-compose.yml --env-file code/.env up -d --wait
docker exec feder-mia-pg psql -U postgres -d feder_materials -tAc 'select version();' | sed 's/^/  /'

# The password reaches libpq through a 0600 file, never a command line.
printf '%s:%s:*:%s:%s\n' "$PGHOST" "$PGPORT" "$PGUSER" \
  "$(grep -E '^POSTGRES_PASSWORD=' code/.env | cut -d= -f2-)" > code/.pgpass
chmod 600 code/.pgpass

say "installing the schema"
( cd "$KIT" && PYTHONPATH= "$PY" kit.py install ) | sed 's/^/  /'
docker exec feder-mia-pg psql -U postgres -d feder_materials -tAc "
select 'tables  '||count(*) from information_schema.tables
 where table_schema='materials' and table_type='BASE TABLE'
union all select 'views   '||count(*) from information_schema.views where table_schema='materials';
" | sed 's/^/  /'

say "creating the writer and validator roles"
# Unprivileged and NOLOGIN: milestone 1 switches to them inside the
# transaction, exactly as the kit's own verification does. Real LOGIN roles on
# separate connections come before any non-synthetic data.
# -i, or the heredoc goes nowhere: docker exec does not forward stdin
# without it, and psql then waits on a terminal it does not have. The
# roles silently were not created and the kit correctly reported them
# missing as "Use existing unprivileged roles".
docker exec -i feder-mia-pg psql -U postgres -d feder_materials -v ON_ERROR_STOP=1 -q <<'SQL'
DO $$ BEGIN
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mia_writer')    THEN CREATE ROLE mia_writer NOLOGIN;    END IF;
  IF NOT EXISTS (SELECT FROM pg_roles WHERE rolname='mia_validator') THEN CREATE ROLE mia_validator NOLOGIN; END IF;
END $$;
SQL
( cd "$KIT" && PYTHONPATH= "$PY" kit.py provision --writer mia_writer --validator mia_validator ) | sed 's/^/  /'

say "OWNER STEP: approving the sixteen example contracts"
echo "  An approval is append-only. It cannot be changed or removed, and a"
echo "  different contract would need a new profile identity."
( cd "$KIT" && PYTHONPATH= "$PY" kit.py approve-examples ) | sed 's/^/  /'
docker exec feder-mia-pg psql -U postgres -d feder_materials -tAc \
  "select '  approvals on record: '||count(*) from materials.payload_schema;"

say "extracting the enforcement rules"
"$PY" code/extract_rules.py -o data/rule_predicates.json | sed 's/^/  /'

say "ready"
cat <<DONE
  database   127.0.0.1:8433/feder_materials  (container feder-mia-pg)
  kit        $KIT
  next       code/load_examples.sh     load the sixteen known-good bundles

  Remove everything, data included:
    docker compose -f code/docker-compose.yml down -v --remove-orphans
DONE
