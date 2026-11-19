"""Lightweight SQL projection shared by cancellation and worker effect handling.

Importing this module never loads transport code, credentials, or model clients.
"""
from apps.supportflow.domain.approvals import execution_compatible_config


def classify(row):
    if row is None:
        return {'action_id': None, 'phase': 'none', 'receipt': None}
    return {'action_id': row['action_id'],
        'phase': {'sending': 'potential', 'unknown': 'potential', 'committed': 'committed', 'rejected': 'rejected'}[row['status']],
        'receipt': row.get('receipt')}


async def read_cancel_effect(db, run):
    """Caller holds limits, run and current authorization. No HTTP or retries."""
    if not execution_compatible_config(run.get('config_snapshot')):
        return classify(None)
    row = await (await db.execute('SELECT * FROM action_dispatches WHERE tenant_id=%s AND run_id=%s FOR UPDATE',
        (run['tenant_id'], run['run_id']))).fetchone()
    return classify(row)
