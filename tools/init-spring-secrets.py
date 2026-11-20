"""Provision local Java DB role credentials once; never print their contents."""
from pathlib import Path
import secrets
import sys

ROOT = Path(__file__).resolve().parents[1]
DEST = ROOT / '.local/spring-workspace-secrets'
sys.path.insert(0, str(ROOT / 'deploy/supportflow'))
from init_secrets import restrict_directory


def main():
    DEST.mkdir(parents=True, exist_ok=True)
    if DEST.resolve() != DEST:
        raise SystemExit('Expected project-local secret directory')
    restrict_directory(DEST)
    for role in ('bootstrap','migrator','runtime','oracle'):
        target = DEST / f'java_{role}_password'
        if target.exists():
            if target.is_symlink() or not target.is_file() or target.stat().st_size<32:
                raise SystemExit('Restore Java business database credentials')
            continue
        with target.open('x', encoding='ascii') as handle:
            handle.write(secrets.token_hex(32))
    print('Java business database credential files are ready.')


if __name__=='__main__':
    main()
