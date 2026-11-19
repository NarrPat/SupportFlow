#!/bin/sh
# Run inside the postgres service after bootstrap. Fail on a false assertion.
set -eu

check_role() {
    role=$1
    database=$2
    other_database=$3
    can_create=$4
    secret_name=${role#sf_}_password
    PGPASSWORD=$(cat "/run/secrets/$secret_name")
    export PGPASSWORD

    result=$(psql -XAt -h 127.0.0.1 -U "$role" -d "$database" -v ON_ERROR_STOP=1 \
        -v other_db="$other_database" -v can_create="$can_create" <<'SQL'
SELECT NOT (rolsuper OR rolcreatedb OR rolcreaterole OR rolreplication OR rolbypassrls)
   AND current_schema() = 'public'
   AND NOT has_database_privilege(current_user, :'other_db', 'CONNECT')
   AND has_database_privilege(current_user, current_database(), 'CREATE') = :'can_create'::boolean
   AND has_database_privilege(current_user, current_database(), 'TEMP') = :'can_create'::boolean
   AND has_schema_privilege(current_user, 'public', 'CREATE') = :'can_create'::boolean
FROM pg_roles WHERE rolname = current_user;
SQL
    )
    [ "$result" = t ] || { echo "Privilege mismatch for $role" >&2; exit 1; }
    echo "OK $role on $database"
}

check_role sf_app_runtime sf_app sf_sandbox false
check_role sf_app_migrator sf_app sf_sandbox true
check_role sf_sandbox_runtime sf_sandbox sf_app false
check_role sf_sandbox_migrator sf_sandbox sf_app true

PGPASSWORD=$(cat /run/secrets/app_runtime_password)
export PGPASSWORD
version=$(psql -XAt -h 127.0.0.1 -U sf_app_runtime -d sf_app -v ON_ERROR_STOP=1 \
    -c "SELECT extversion FROM pg_extension WHERE extname = 'vector' AND '[1,0]'::vector <-> '[0,0]'::vector = 1")
[ -n "$version" ] || { echo 'pgvector distance check failed' >&2; exit 1; }
echo "OK vector $version"
