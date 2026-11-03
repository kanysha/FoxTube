#!/usr/bin/env bash
set -euo pipefail

# Executed only for a new PostgreSQL data volume by the official image entrypoint.
psql --no-psqlrc --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" \
    --set=ON_ERROR_STOP=1 \
    --set=api_password="$API_DB_PASSWORD" \
    --set=worker_password="$WORKER_DB_PASSWORD" \
    --set=database_name="$POSTGRES_DB" <<'SQL'
CREATE ROLE foxtube_api LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'api_password';
CREATE ROLE foxtube_worker LOGIN NOSUPERUSER NOCREATEDB NOCREATEROLE NOREPLICATION PASSWORD :'worker_password';
REVOKE ALL ON DATABASE :"database_name" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"database_name" TO foxtube_api, foxtube_worker;
REVOKE ALL ON SCHEMA public FROM PUBLIC;
GRANT USAGE ON SCHEMA public TO foxtube_api, foxtube_worker;
SQL
