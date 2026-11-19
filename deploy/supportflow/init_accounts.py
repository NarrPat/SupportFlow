"""Create random demo credentials once in the protected SupportFlow secrets directory."""
import json
import os
from pathlib import Path
import secrets
import tempfile

from init_secrets import BASE, main as ensure_database_secrets


def main():
    ensure_database_secrets()
    directory = BASE / ".secrets"
    fixture = BASE.parents[1] / "evals/fixtures/supportflow/fx_base/seed.json"
    names = {user["username"] for user in json.loads(fixture.read_text(encoding="utf-8"))["users"]}
    target = directory / "seed-accounts.json"
    if not target.exists():
        with tempfile.NamedTemporaryFile("w", dir=directory, delete=False, encoding="utf-8") as temp:
            json.dump({name: secrets.token_urlsafe(32) for name in sorted(names)}, temp)
        try:
            try:
                os.link(temp.name, target)
            except FileExistsError:
                pass
        finally:
            Path(temp.name).unlink()
    if target.is_symlink() or set(json.loads(target.read_text(encoding="utf-8"))) != names:
        raise SystemExit("Restore the original local seed accounts file.")
    print("Local seed accounts ready. Read the protected file locally to sign in.")


if __name__ == "__main__":
    main()
