#!/usr/bin/env bash
# Remove the container and its data. Nothing else on this host is touched --
# no prune, no image removal, no other project's containers.
set -euo pipefail
cd "$(dirname "$0")/.."
docker compose -f code/docker-compose.yml down -v --remove-orphans
rm -f code/.pgpass
echo "  container and volume removed; code/.pgpass deleted"
echo "  postgres:18.6 image kept, so the next bootstrap does not re-pull 457 MB"
