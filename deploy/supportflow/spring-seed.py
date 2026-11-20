"""Idempotent, setup-only seed for the independent Java business database."""
from pathlib import Path
import sys

import psycopg
from psycopg.types.json import Jsonb

from apps.supportflow.db.demo_data import DemoSeedData, materialize
from apps.supportflow.db.seed_data import SeedData
from apps.supportflow.domain.grant_policy import load_policy_bundle


def main():
    if sys.argv[1:] not in ([], ['--demo']):
        raise SystemExit('Unsupported Java seed options')
    base = SeedData.model_validate_json(Path('/fixture/base.json').read_bytes())
    pin = load_policy_bundle()
    password = Path('/run/secrets/java_migrator_password').read_text(encoding='ascii').strip()
    with psycopg.connect(host='java-postgres',dbname='sf_java_workspace',user='sf_java_migrator',password=password,
                         connect_timeout=3,options='-c statement_timeout=15000') as db:
        db.execute('SELECT pg_advisory_xact_lock(92708500)')
        db.execute('''INSERT INTO grant_policy_state VALUES(true,%s,%s,%s,%s) ON CONFLICT DO NOTHING''',
            (pin['policy_version'],pin['section_id'],pin['content_sha256'],Jsonb(pin['constants'])))
        for item in base.subscriptions:
            db.execute('''INSERT INTO subscriptions(tenant_id,customer_id,subscription_id,base_seats,used_seats,temporary_seats,
                temporary_expires_at,plan_status,business_version) VALUES(%(tenant_id)s,%(customer_id)s,%(subscription_id)s,
                %(base_seats)s,%(used_seats)s,%(temporary_seats)s,%(temporary_expires_at)s,%(plan_status)s,%(business_version)s)
                ON CONFLICT DO NOTHING''',item.model_dump())
        policy, invites, statuses = base.invitation_policies, base.invitations, base.service_statuses
        if sys.argv[1:]:
            demo = DemoSeedData.model_validate_json(Path('/fixture/demo.json').read_bytes())
            extras = materialize(demo,db.execute('SELECT transaction_timestamp()').fetchone()[0])
            policy, invites, statuses = [*policy,*extras[0]], [*invites,*extras[1]], [*statuses,*extras[2]]
        for item in policy:
            row = item.model_dump()
            if row['allowed_domains'] is not None:
                row['allowed_domains'] = Jsonb(row['allowed_domains'])
            db.execute('''INSERT INTO invitation_policies(tenant_id,customer_id,allowed_domains,allow_subdomains,invitation_permission)
                VALUES(%(tenant_id)s,%(customer_id)s,%(allowed_domains)s,%(allow_subdomains)s,%(invitation_permission)s)
                ON CONFLICT DO NOTHING''',row)
        for item in invites:
            db.execute('''INSERT INTO invitations(tenant_id,customer_id,invitation_id,email,email_domain,created_at,expires_at,status,reason_code)
                VALUES(%(tenant_id)s,%(customer_id)s,%(invitation_id)s,%(email)s,%(email_domain)s,%(created_at)s,%(expires_at)s,%(status)s,%(reason_code)s)
                ON CONFLICT DO NOTHING''',item.model_dump())
        for item in statuses:
            db.execute('''INSERT INTO service_statuses(tenant_id,customer_id,component,status,updated_at)
                VALUES(%(tenant_id)s,%(customer_id)s,%(component)s,%(status)s,%(updated_at)s) ON CONFLICT DO NOTHING''',item.model_dump())
    print('Java business seed complete; existing rows retained.')


if __name__ == '__main__':
    main()
