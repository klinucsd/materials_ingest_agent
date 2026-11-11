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
# Only default MIA_KIT_DIR if that default actually exists. Pointing it at a
# path with no kit in it made the probe crash and the caller report "nothing to
# decide" -- an unset variable you notice beats a wrong one you do not.
if [ -z "${MIA_KIT_DIR:-}" ] && [ -d "$_here/../data/kit/model" ]; then
    export MIA_KIT_DIR="$_here/../data/kit"
fi
if [ -z "${MIA_KIT_DIR:-}" ]; then
    echo "  note: MIA_KIT_DIR is not set and no kit is at data/kit." >&2
    echo "        Anything that reads the schema will say so and stop." >&2
fi
unset _here
