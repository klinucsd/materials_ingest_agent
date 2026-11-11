# Source this, do not run it. Exports the libpq settings the kit expects.
#
# The kit's CLI says "use libpq PG* settings, never command-line passwords",
# so the password reaches it through a 0600 .pgpass file and never through a
# process argument where `ps` would show it.
_here=$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)
export PGHOST=127.0.0.1
export PGPORT=8433
export PGUSER=postgres
export PGDATABASE=feder_materials
export PGPASSFILE="$_here/.pgpass"
export MIA_KIT_DIR="${MIA_KIT_DIR:-$_here/../data/kit}"
unset _here
