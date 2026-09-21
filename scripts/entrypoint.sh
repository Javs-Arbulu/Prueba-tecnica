#!/usr/bin/env bash
# Single entrypoint for every container command. Keeps `docker compose up`
# a one-liner: wait for the database, migrate, seed, then serve.
set -euo pipefail

wait_for_db() {
  echo "==> waiting for the database"
  python - <<'PY'
import os
import sys
import time

import psycopg

dsn = os.environ.get("DATABASE_URL", "postgres://support:support@db:5432/support")
deadline = time.time() + 60
while True:
    try:
        with psycopg.connect(dsn, connect_timeout=3):
            break
    except Exception as exc:  # noqa: BLE001 - any connection error means "not ready"
        if time.time() > deadline:
            sys.exit(f"database unreachable after 60s: {exc}")
        time.sleep(1)
print("==> database is ready")
PY
}

# `if` blocks rather than `[ … ] && cmd`: under `set -e` a false test is a
# failing command, and the container would exit instead of skipping the step.
prepare() {
  wait_for_db
  if [ "${RUN_MIGRATIONS:-true}" = "true" ]; then
    python manage.py migrate --noinput
  fi
  if [ "${SEED_DEMO:-false}" = "true" ]; then
    python manage.py seed_demo
  fi
}

case "${1:-runserver}" in
  runserver)
    prepare
    # With DJANGO_DEBUG=False the development server stops serving static files
    # and the admin loses its stylesheet. --insecure puts them back, so turning
    # DEBUG off to match production stays a one-variable decision.
    if [ "${DJANGO_DEBUG:-True}" = "True" ]; then
      exec python manage.py runserver 0.0.0.0:8000
    else
      exec python manage.py runserver 0.0.0.0:8000 --insecure
    fi
    ;;
  gunicorn)
    prepare
    python manage.py collectstatic --noinput
    exec gunicorn config.wsgi:application \
      --bind 0.0.0.0:8000 \
      --workers "${WEB_CONCURRENCY:-3}" \
      --access-logfile -
    ;;
  test)
    wait_for_db
    shift
    # Pinned here because compose exports DJANGO_SETTINGS_MODULE for the server,
    # and pytest-django gives the environment variable precedence over the ini.
    # Without this the suite runs on config.settings.local while CI runs on
    # config.settings.test, which is the worst kind of difference: the one
    # nobody sees until a test passes locally and fails in the pipeline.
    exec env DJANGO_SETTINGS_MODULE=config.settings.test pytest "$@"
    ;;
  *)
    exec "$@"
    ;;
esac
