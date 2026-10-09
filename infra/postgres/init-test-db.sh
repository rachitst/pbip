#!/bin/sh
# Creates the separate database used by the integration tests (runs once, on first DB start).
set -e
psql -v ON_ERROR_STOP=1 --username "$POSTGRES_USER" --dbname "$POSTGRES_DB" <<-SQL
    CREATE DATABASE ${POSTGRES_DB}_test;
SQL
