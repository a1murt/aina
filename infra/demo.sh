#!/usr/bin/env bash
# make demo (SPEC §17, NFR-08): build -> infrastructure -> migrations -> seed -> history
# (sim backfill 01.09 -> demo_start into the DB + engine replay) -> case import -> live services.
#
# Every step runs in the service images (docker compose run), so a clean clone needs only
# Docker, make and bash. Infrastructure containers that already run are reused as they are
# (--no-recreate), so a shared host keeps its database and broker.
#
#   DEMO_FRESH=1   start from an empty demo database and an empty Redis DB (drops the database
#                  named in DATABASE_URL and flushes the Redis DB of REDIS_URL — demo data only)
#   DEMO_SKIP_BUILD=1  reuse the images that exist
set -euo pipefail

COMPOSE=${COMPOSE:-docker compose}
DC="$COMPOSE --profile demo"
ROOT=$(cd "$(dirname "$0")/.." && pwd)
cd "$ROOT"
[ -f .env ] || cp .env.example .env

env_value() { # value of KEY in .env (last one wins), else $2
  local v
  v=$(grep -E "^$1=" .env | tail -n 1 | cut -d= -f2- || true)
  echo "${v:-$2}"
}
DATABASE_URL=$(env_value DATABASE_URL postgresql+asyncpg://qost:qost@timescaledb:5432/qost)
REDIS_URL=$(env_value REDIS_URL redis://redis:6379/0)
PG_USER=$(env_value POSTGRES_USER qost)
DB_NAME=${DATABASE_URL##*/}
DB_NAME=${DB_NAME%%\?*}
REDIS_DB=${REDIS_URL##*/}

T0=$(date +%s)
LAST=$T0
TIMES=()
step() {
  local now
  now=$(date +%s)
  if [ -n "${CURRENT:-}" ]; then TIMES+=("$CURRENT: $((now - LAST)) s"); fi
  CURRENT=$1
  LAST=$now
  printf '\n=== [%4ss] %s\n' "$((now - T0))" "$1"
}

if [ "${DEMO_SKIP_BUILD:-0}" != "1" ]; then
  step "build images"
  $DC build
fi

step "infrastructure (timescaledb, redis, mqtt)"
$COMPOSE up -d --wait --wait-timeout 300 --no-recreate timescaledb redis mqtt

psql_admin() { $COMPOSE exec -T timescaledb psql -v ON_ERROR_STOP=1 -U "$PG_USER" -d postgres -tAc "$1"; }
if [ "${DEMO_FRESH:-0}" = "1" ]; then
  step "fresh demo data (drop database '$DB_NAME', flush Redis DB $REDIS_DB)"
  $DC stop sim collector engine api notifier web >/dev/null 2>&1 || true
  psql_admin "DROP DATABASE IF EXISTS \"$DB_NAME\" WITH (FORCE)"
  $COMPOSE exec -T redis redis-cli -n "$REDIS_DB" FLUSHDB
  $DC run --rm --no-deps --entrypoint sh collector -c 'rm -rf /var/lib/qost/spool/*' || true
fi
if [ "$(psql_admin "SELECT 1 FROM pg_database WHERE datname = '$DB_NAME'")" != "1" ]; then
  psql_admin "CREATE DATABASE \"$DB_NAME\""
fi

step "migrations (alembic upgrade head)"
$DC run --rm --no-deps api alembic -c services/api/alembic.ini upgrade head

step "seed (users of all roles, reference tables, calendar, plan)"
$DC run --rm --no-deps api python -m qost_api seed

step "history: sim backfill -> database"
$DC run --rm --no-deps sim python -m qost_sim backfill --sink db: --batch-size 5000

step "history: engine replay (derived tables, checkpoint, baseline)"
$DC run --rm --no-deps engine python -m qost_engine replay

step "import data/case/source/case2_data.docx (as admin)"
$DC run --rm --no-deps -v "$ROOT/data/case/source:/app/data/case/source:ro" api \
  python -m qost_api import data/case/source/case2_data.docx --user admin

step "live services (sim, collector, engine, api, notifier, web)"
$DC up -d --no-deps --wait --wait-timeout 300 sim collector engine api notifier web

step "done"
TOTAL=$(( $(date +%s) - T0 ))
echo
echo "make demo timing:"
for t in "${TIMES[@]}"; do echo "  $t"; done
echo "  total: ${TOTAL} s"
mkdir -p var
printf '%s\n' "${TIMES[@]}" "total: ${TOTAL} s" > var/demo_timing.txt
echo
echo "Web: http://localhost:3000   API: http://localhost:8000/api/docs   console: make demo-reset"
echo "Users: director, master, operator (ASSY-1), maintenance, quality, admin; password = DEMO_PASSWORD"
