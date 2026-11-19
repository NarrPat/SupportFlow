-- The official entrypoint runs this file once, on an empty data volume.
\set ON_ERROR_STOP on
\getenv app_db SF_APP_DB
\getenv sandbox_db SF_SANDBOX_DB

SELECT trim(pg_read_file('/run/secrets/app_migrator_password')) AS app_migrator,
       trim(pg_read_file('/run/secrets/app_runtime_password')) AS app_runtime,
       trim(pg_read_file('/run/secrets/sandbox_migrator_password')) AS sandbox_migrator,
       trim(pg_read_file('/run/secrets/sandbox_runtime_password')) AS sandbox_runtime
\gset pw_

CREATE ROLE sf_app_migrator LOGIN PASSWORD :'pw_app_migrator';
CREATE ROLE sf_app_runtime LOGIN PASSWORD :'pw_app_runtime';
CREATE ROLE sf_sandbox_migrator LOGIN PASSWORD :'pw_sandbox_migrator';
CREATE ROLE sf_sandbox_runtime LOGIN PASSWORD :'pw_sandbox_runtime';

CREATE DATABASE :"app_db" OWNER sf_app_migrator;
CREATE DATABASE :"sandbox_db" OWNER sf_sandbox_migrator;
-- pg_catalog remains implicitly searched first. Do not expand a role-named schema.
ALTER DATABASE :"app_db" SET search_path = public;
ALTER DATABASE :"sandbox_db" SET search_path = public;

REVOKE ALL ON DATABASE postgres, template1, :"app_db", :"sandbox_db" FROM PUBLIC;
GRANT CONNECT ON DATABASE :"app_db" TO sf_app_runtime;
GRANT CONNECT ON DATABASE :"sandbox_db" TO sf_sandbox_runtime;

\connect :app_db
CREATE EXTENSION vector;
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE sf_app_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sf_app_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE sf_app_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sf_app_runtime;

\connect :sandbox_db
REVOKE CREATE ON SCHEMA public FROM PUBLIC;
ALTER DEFAULT PRIVILEGES FOR ROLE sf_sandbox_migrator IN SCHEMA public
    GRANT SELECT, INSERT, UPDATE, DELETE ON TABLES TO sf_sandbox_runtime;
ALTER DEFAULT PRIVILEGES FOR ROLE sf_sandbox_migrator IN SCHEMA public
    GRANT USAGE, SELECT ON SEQUENCES TO sf_sandbox_runtime;
