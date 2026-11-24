#!/bin/sh
# Creates the InvoiceHub database roles and database.
# Runs from /docker-entrypoint-initdb.d in Compose and from CI via psql.
# Connect as a superuser. Passwords are throwaway local/CI test values, overridable by env.
set -eu

MIGRATOR_PASSWORD="${INVOICEHUB_MIGRATOR_PASSWORD:-migrator-dev-pw}"
APP_PASSWORD="${INVOICEHUB_APP_PASSWORD:-app-dev-pw}"

psql -v ON_ERROR_STOP=1 \
  -v migrator_pw="$MIGRATOR_PASSWORD" -v app_pw="$APP_PASSWORD" \
  ${PGHOST:+-h "$PGHOST"} ${PGPORT:+-p "$PGPORT"} ${PGUSER:+-U "$PGUSER"} \
  --dbname "${PGDATABASE:-postgres}" <<'SQL'
CREATE ROLE invoicehub_migrator LOGIN CREATEDB NOSUPERUSER NOBYPASSRLS PASSWORD :'migrator_pw';
CREATE ROLE invoicehub_app LOGIN NOSUPERUSER NOBYPASSRLS NOCREATEDB PASSWORD :'app_pw';
CREATE DATABASE invoicehub OWNER invoicehub_migrator;
GRANT CONNECT ON DATABASE invoicehub TO invoicehub_app;
GRANT invoicehub_app TO invoicehub_migrator;
SQL
