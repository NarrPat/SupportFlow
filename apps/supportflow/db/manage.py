"""Apply one database's ordinary migrations or seed, inside the SF network."""
import argparse
import hashlib
import os
from pathlib import Path
import re
import sys

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory
from pydantic import ValidationError
from sqlalchemy import URL, create_engine, text
from sqlalchemy.engine import Connection
from sqlalchemy.exc import DBAPIError

from .demo_data import DemoSeedData, DemoSeedError, seed_demo_sandbox
from .seed_data import SeedData, seed_app, seed_sandbox


MIGRATIONS = Path(__file__).resolve().parent / "migrations"


class SetupError(Exception):
    """Safe summary for CLI output, without a connection string or SQL parameters."""


def database_url(target: str) -> URL:
    database = os.environ.get("PGDATABASE", f"sf_{target}")
    if database != f"sf_{target}" and not re.fullmatch(rf"sf_eval_[a-z0-9_]+_{target}", database):
        raise SetupError("Database name does not match the selected SupportFlow target")
    role = f"sf_{target}_migrator"
    if os.environ.get("PGUSER", role) != role:
        raise SetupError("Use the target's migration role")
    return URL.create(
        "postgresql+psycopg", username=role, database=database,
        host=os.environ.get("PGHOST", "postgres"), port=int(os.environ.get("PGPORT", "5432")),
    )


def migration_config(conn: Connection, target: str) -> Config:
    cfg = Config()
    cfg.set_main_option("script_location", MIGRATIONS.as_posix().replace("%", "%%"))
    cfg.set_main_option("version_locations", (MIGRATIONS / target).as_posix().replace("%", "%%"))
    cfg.set_main_option("path_separator", "os")
    cfg.attributes["connection"] = conn
    return cfg


def seed(conn: Connection, target: str, data: SeedData | DemoSeedData, digest: str) -> str:
    if isinstance(data, DemoSeedData) and target != "sandbox":
        raise SetupError("The demo invitation fixture requires the sandbox target")
    expected = ScriptDirectory.from_config(migration_config(conn, target)).get_heads()
    current = conn.execute(text("SELECT version_num FROM public.alembic_version")).scalars().all()
    if sorted(current) != sorted(expected) or not expected:
        raise SetupError("Migrate the target to its current revision before seeding")
    key = {"fixture_id": data.fixture_id, "schema_version": data.schema_version}
    previous = conn.execute(text("""
        SELECT fixture_sha256 FROM public.seed_history
        WHERE fixture_id = :fixture_id AND schema_version = :schema_version
    """), key).scalar_one_or_none()
    if previous is not None:
        if previous != digest:
            raise SetupError("Seed input differs from the committed input with this ID and version")
        return "already_applied"

    if isinstance(data, DemoSeedData):
        seed_demo_sandbox(conn, data)
    elif target == "app":
        path = os.environ.get("SF_SEED_PASSWORDS_FILE")
        if not path:
            raise SetupError("Set SF_SEED_PASSWORDS_FILE for first-time application seed")
        seed_app(conn, data, Path(path))
    else:
        seed_sandbox(conn, data)
    conn.execute(text("""
        INSERT INTO public.seed_history (fixture_id, schema_version, fixture_sha256)
        VALUES (:fixture_id, :schema_version, :fixture_sha256)
    """), {**key, "fixture_sha256": digest})
    return "applied"


def run() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="operation", required=True)
    for name in ("migrate", "seed", "seed-demo"):
        sub = commands.add_parser(name)
        sub.add_argument("--target", required=True, choices=("app", "sandbox"))
        if name in {"seed", "seed-demo"}:
            sub.add_argument("--fixture", required=True, type=Path)
    args = parser.parse_args()
    if args.operation == "seed-demo" and args.target != "sandbox":
        raise SetupError("The demo invitation fixture requires the sandbox target")
    if args.operation in {"seed", "seed-demo"}:
        raw = args.fixture.read_bytes()
        schema = DemoSeedData if args.operation == "seed-demo" else SeedData
        data = schema.model_validate_json(raw)
        digest = hashlib.sha256(raw).hexdigest()

    url = database_url(args.target)
    engine = create_engine(url, hide_parameters=True, connect_args={"connect_timeout": 8})
    try:
        with engine.begin() as conn:
            identity = conn.execute(text("SELECT current_database(), current_user")).one()
            if tuple(identity) != (url.database, url.username):
                raise SetupError("Unexpected connected database or login role")
            conn.execute(text("SET LOCAL lock_timeout = '30s'"))
            # Same key for migration and seed; PostgreSQL scopes the lock to this DB.
            conn.execute(text("SELECT pg_catalog.pg_advisory_xact_lock(1397116210, 1)"))
            if args.operation == "migrate":
                command.upgrade(migration_config(conn, args.target), "head")
                outcome = "at_head"
            else:
                outcome = seed(conn, args.target, data, digest)
    finally:
        engine.dispose()
    print(f"{args.operation} {args.target}: {outcome}")


def main():
    try:
        run()
    except (SetupError, DemoSeedError) as exc:
        print(str(exc), file=sys.stderr)
    except ValidationError as exc:
        fields = ["/".join(map(str, error["loc"])) or "root" for error in exc.errors(include_input=False)]
        print("Invalid seed fixture fields: " + ", ".join(fields), file=sys.stderr)
    except DBAPIError as exc:
        # Driver messages can contain failed rows/password hashes or connection info.
        state = getattr(exc.orig, "sqlstate", None) or "unavailable"
        print(f"Database operation failed ({type(exc.orig).__name__}, SQLSTATE={state})", file=sys.stderr)
    except Exception as exc:
        print(f"Database setup failed ({type(exc).__name__}); check configuration and inputs", file=sys.stderr)
    else:
        return 0
    return 1


if __name__ == "__main__":
    sys.exit(main())
