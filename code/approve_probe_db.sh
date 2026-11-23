#!/usr/bin/env bash
# Approve the sixteen example contracts in the SCRATCH database.
#
#   code/approve_probe_db.sh
#
# RUN THIS YOURSELF, for the same reason as bootstrap.sh: approving a contract
# is a privileged, append-only act. It cannot be updated or deleted afterwards,
# and a changed contract needs a new profile identity. No agent reaches this
# script.
#
# Why a separate script rather than a command to paste: `pgenv.sh` exports
# PGDATABASE=feder_materials, so an override has to come AFTER sourcing it. Get
# that order wrong and the approval lands in the database holding the
# known-good T2 load -- append-only, in the one place we need to stay clean.
# The order lives here instead of in anyone's shell history, and the target is
# asserted below before the kit is invoked.
#
# feder_mia_probe is DISPOSABLE. Recreate and re-approve as often as needed:
#   docker exec -i feder-mia-pg psql -U postgres -d postgres -v ON_ERROR_STOP=1 <<'SQL'
#   DROP DATABASE IF EXISTS feder_mia_probe;
#   CREATE DATABASE feder_mia_probe TEMPLATE template0 ENCODING 'UTF8'
#     LC_COLLATE 'en_US.utf8' LC_CTYPE 'en_US.utf8';
#   SQL
# then `kit.py install`, `kit.py provision`, and this script. The collation is
# not incidental: it matches feder_materials so that a rule behaving
# differently here is a real finding and not an artefact of the locale.
set -euo pipefail
cd "$(dirname "$0")/.."
HERE=$(pwd)

TARGET=feder_mia_probe
PROTECTED=feder_materials

# Guard the CONSTANT, not the connection. Once the check below has confirmed we
# landed in $TARGET, comparing that result to $PROTECTED can never fire -- so
# the risk worth catching is someone repointing TARGET at the known-good load.
if [ "$TARGET" = "$PROTECTED" ]; then
    echo "!! TARGET is $PROTECTED, which holds the known-good T2 load." >&2
    echo "   Approvals are append-only. Use a scratch database." >&2
    exit 1
fi

source code/pgenv.sh
export PGDATABASE="$TARGET"          # AFTER the source. This is the whole point.

# pgenv.sh only defaults MIA_KIT_DIR when data/kit/model exists, and on this
# machine the kit lives in the sibling feder/ tree instead. Default to that one
# if it is there, and otherwise say so rather than guessing: a MIA_KIT_DIR
# pointing at a path with no kit in it made the probe crash while the caller
# reported "nothing to decide".
if [ -z "${MIA_KIT_DIR:-}" ] && \
   [ -d "$HERE/../feder/materials_ingest_agent/data/kit/model" ]; then
    MIA_KIT_DIR=$(cd "$HERE/../feder/materials_ingest_agent/data/kit" && pwd)
    export MIA_KIT_DIR
fi
: "${MIA_KIT_DIR:?set MIA_KIT_DIR -- the kit is not in this repository}"
[ -f "$MIA_KIT_DIR/kit.py" ] || { echo "no kit.py in $MIA_KIT_DIR" >&2; exit 1; }
PY="$HERE/.venv-kit/bin/python"
[ -x "$PY" ] || { echo "no .venv-kit" >&2; exit 1; }

# Assert where we are before doing anything append-only. `current_database()`
# is the server's answer, not our variable read back to us.
landed=$(PGPASSFILE="$HERE/code/.pgpass" psql -h "$PGHOST" -p "$PGPORT" \
          -U "$PGUSER" -d "$PGDATABASE" -At -c 'select current_database()')
if [ "$landed" != "$TARGET" ]; then
    echo "!! libpq lands in '$landed', not '$TARGET' -- refusing" >&2
    exit 1
fi

before=$(PGPASSFILE="$HERE/code/.pgpass" psql -h "$PGHOST" -p "$PGPORT" \
          -U "$PGUSER" -d "$PGDATABASE" -At \
          -c 'select count(*) from materials.payload_schema')
printf '\n==> approving in %s (%s approval(s) on record now)\n' "$landed" "$before"

( cd "$MIA_KIT_DIR" && PYTHONPATH= "$PY" kit.py approve-examples ) | sed 's/^/  /'

after=$(PGPASSFILE="$HERE/code/.pgpass" psql -h "$PGHOST" -p "$PGPORT" \
         -U "$PGUSER" -d "$PGDATABASE" -At \
         -c 'select count(*) from materials.payload_schema')
printf '\n==> %s now holds %s approval(s)\n' "$landed" "$after"
printf '    %s still holds %s\n' "$PROTECTED" \
  "$(PGPASSFILE="$HERE/code/.pgpass" psql -h "$PGHOST" -p "$PGPORT" \
      -U "$PGUSER" -d "$PROTECTED" -At \
      -c 'select count(*) from materials.payload_schema')"
