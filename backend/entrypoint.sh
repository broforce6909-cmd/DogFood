#!/bin/sh
# Container entrypoint: bring the schema up to date, optionally seed, then serve.
# Compose already gates start on the db healthcheck, but we retry anyway so the
# stack is robust to a slow first-boot Postgres on a cold laptop.
set -e

echo "[entrypoint] waiting for database..."
python -m app.wait_for_db

echo "[entrypoint] applying migrations..."
alembic upgrade head

if [ "${SEED_ON_START}" = "true" ]; then
  echo "[entrypoint] seeding fixture data..."
  python -m app.seed
  # The organizers' published fixtures.json, loaded as its own event. Runs on
  # every boot (it skips the load once present) because it also reprints the
  # four acceptance logins, which a second `docker compose up` still needs.
  echo "[entrypoint] loading the organizers' fixtures..."
  python -m app.seed_fixtures
fi

echo "[entrypoint] starting uvicorn on :8000"
# --no-access-log: app.logging_config's own request middleware already emits
# one structured (JSON, request-id-correlated) line per request. Uvicorn's
# default access log is a second, plain-text, uncorrelated line for the same
# event -- noise once the real one exists, not a second source of truth.
exec uvicorn app.main:app --host 0.0.0.0 --port 8000 --reload --no-access-log
