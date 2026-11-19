"""Offline check of the project interpreter and its installed direct dependencies."""

import argparse
import importlib
import json
import site
import sys
import tomllib
from importlib.metadata import version
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
IMPORTS = {
    "alembic": "alembic",
    "argon2-cffi": "argon2",
    "celery": "celery",
    "fastapi": "fastapi",
    "fastembed": "fastembed",
    "httpx": "httpx",
    "jieba": "jieba",
    "tokenizers": "tokenizers",
    "onnxruntime": "onnxruntime",
    "langchain": "langchain",
    "langgraph": "langgraph.graph",
    "langgraph-checkpoint-postgres": "langgraph.checkpoint.postgres",
    "pgvector": "pgvector.sqlalchemy",
    "psycopg": "psycopg",
    "pydantic": "pydantic",
    "python-multipart": "python_multipart",
    "redis": "redis",
    "sqlalchemy": "sqlalchemy",
    "uvicorn": "uvicorn",
    "pytest": "pytest",
    "pytest-asyncio": "pytest_asyncio",
}


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--interpreter-only", action="store_true")
    args = parser.parse_args()
    pinned = (ROOT / ".python-version").read_text().strip()
    actual = ".".join(map(str, sys.version_info[:3]))
    if actual != pinned or Path(sys.prefix).resolve() != (ROOT / ".venv").resolve():
        raise SystemExit(f"Expected project .venv on Python {pinned}. Got {sys.prefix}, {actual}")
    if site.ENABLE_USER_SITE or sys.prefix == sys.base_prefix:
        raise SystemExit("Expected isolated site-packages")
    print(json.dumps({"python": actual, "prefix": sys.prefix, "user_site": site.ENABLE_USER_SITE}))
    if args.interpreter_only:
        return
    with (ROOT / "uv.lock").open("rb") as handle:
        locked = {row["name"]: row["version"] for row in tomllib.load(handle)["package"]}
    for name, module in IMPORTS.items():
        importlib.import_module(module)
        installed = version(name)
        if installed != locked[name]:
            raise SystemExit(f"{name}: expected {locked[name]}, got {installed}")
        print(f"{name}=={installed}")
    # Ensure the selected versions expose the interfaces needed for the later Spike.
    from langgraph.checkpoint.postgres import PostgresSaver
    from langgraph.graph import StateGraph

    print("Available interfaces:", PostgresSaver.__name__, StateGraph.__name__)


if __name__ == "__main__":
    main()
