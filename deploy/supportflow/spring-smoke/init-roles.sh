#!/usr/bin/env bash
set -eu
case "$POSTGRES_DB" in sf_java_smoke|sf_java_workspace) ;; *) echo 'Unexpected Java database' >&2; exit 1;; esac
export migrator=$(cat /run/secrets/java_migrator_password)
export runtime=$(cat /run/secrets/java_runtime_password)
export oracle=$(cat /run/secrets/java_oracle_password)
PGPASSWORD=$(cat /run/secrets/java_bootstrap_password) psql -X -v ON_ERROR_STOP=1 -U sf_java_bootstrap -d "$POSTGRES_DB" <<'SQL'
\getenv migrator migrator
\getenv runtime runtime
\getenv oracle oracle
\getenv java_database POSTGRES_DB
CREATE ROLE sf_java_migrator LOGIN PASSWORD :'migrator';
CREATE ROLE sf_java_runtime LOGIN PASSWORD :'runtime';
CREATE ROLE sf_java_oracle LOGIN PASSWORD :'oracle';
REVOKE ALL ON DATABASE :"java_database" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"java_database" TO sf_java_migrator, sf_java_runtime, sf_java_oracle;
ALTER SCHEMA public OWNER TO sf_java_migrator;
GRANT USAGE ON SCHEMA public TO sf_java_runtime, sf_java_oracle;
SQL
unset migrator runtime oracle
