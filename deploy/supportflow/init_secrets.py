"""Generate one local set of Compose file secrets without rotating existing logins."""

import hashlib
import os
import re
import secrets
import subprocess
from pathlib import Path
from tempfile import TemporaryDirectory


BASE = Path(__file__).resolve().parent
PASSWORD_NAMES = (
    "bootstrap", "app_migrator", "app_runtime", "sandbox_migrator", "sandbox_runtime", "redis_password"
)


def restrict_directory(directory: Path) -> None:
    if os.name != 'nt':
        directory.chmod(0o700)
        return

    # chmod on Windows changes a read-only flag, not the inherited NTFS ACL.
    identity = subprocess.check_output(['whoami.exe', '/user', '/fo', 'csv', '/nh'])
    sid = re.search(rb'\bS-1-\d+(?:-\d+)+\b', identity)
    if sid is None:
        raise SystemExit('Could not identify the current Windows SID.')
    principals = [sid.group().decode('ascii'), 'S-1-5-18', 'S-1-5-32-544']
    subprocess.run(
        ['icacls.exe', str(directory), '/inheritance:r', '/grant:r',
         *(f'*{principal}:(OI)(CI)F' for principal in principals)],
        check=True, stdout=subprocess.DEVNULL,
    )


def main() -> None:
    destination = BASE / ".secrets"
    if destination.exists():
        if destination.resolve() != destination or not destination.is_dir():
            raise SystemExit('Expected a local secrets directory, not a link or another file type.')
        restrict_directory(destination)
        for name in (*PASSWORD_NAMES, "redis.acl"):
            if not (destination / name).is_file() or not (destination / name).stat().st_size:
                raise SystemExit(f"Incomplete secrets directory: missing or empty {name}. Restore the original files.")
        service_token = destination / "sandbox_service_token"
        if not service_token.exists():
            with service_token.open("x", encoding="ascii") as stream:
                stream.write(secrets.token_hex(32))
            service_token.chmod(0o444)
        print("Local secret files already exist. Reusing them.")
        return

    # Rename only after the complete set exists, so concurrent setup cannot mix passwords.
    with TemporaryDirectory(prefix=".secrets-", dir=BASE) as temp:
        bundle = Path(temp) / "bundle"
        bundle.mkdir(mode=0o700)
        restrict_directory(bundle)
        for name in PASSWORD_NAMES:
            path = bundle / name
            path.write_bytes(secrets.token_hex(32).encode("ascii"))
            # Compose bind-mounts these files. The container service UID must be able
            # to read them; the 0700 host directory restricts access on POSIX.
            path.chmod(0o444)
        (bundle / "sandbox_service_token").write_text(secrets.token_hex(32), encoding="ascii")
        (bundle / "sandbox_service_token").chmod(0o444)
        password_hash = hashlib.sha256((bundle / "redis_password").read_bytes()).hexdigest()
        (bundle / "redis.acl").write_text(
            f"user default on #{password_hash} ~* &* +@all -@admin -flushall -flushdb\n", encoding="ascii"
        )
        bundle.rename(destination)
    print("Generated local credential files for sf-local.")


if __name__ == "__main__":
    main()
