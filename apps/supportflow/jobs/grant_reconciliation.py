"""Bounded read-only reconciliation; no claim, model, POST or budget reset."""
from datetime import timedelta

from . import event_hints, grant_effects
from .grant_transport import GrantClient
from .runtime import trace


def reconcile_one(conn,run_id,*,client=None):
    from .core import CLOSED, finish_locked, locked_run, session_identity, try_lock, unlock
    if not try_lock(conn,run_id):
        return {'run_id':run_id,'outcome':'RECONCILIATION_BLOCKED'}
    try:
        with conn.transaction():
            _, run = locked_run(conn,run_id)
            if run is None or run['status'] != 'needs_reconciliation':
                return {'run_id':run_id,'outcome':'inactive'}
            session_identity(conn,run_id,run['execution_generation'])
            row = grant_effects.read_dispatch(conn,run,lock=True)
            if row is None or row['status'] == 'rejected' or row['finalized_at'] is not None:
                return {'run_id':run_id,'outcome':'inactive'}
            row = conn.execute('''UPDATE action_dispatches SET reconciliation_rounds=reconciliation_rounds+1,
                next_reconcile_at=clock_timestamp()+interval '2 seconds' WHERE action_id=%s RETURNING *''',
                (row['action_id'],)).fetchone()
        client = client or GrantClient()
        command = grant_effects._command(row)
        requests = 0
        while row['status'] in {'sending','unknown'} and requests < 2:
            with conn.transaction():
                current = grant_effects._owned(conn,run)
                if current['status'] != 'needs_reconciliation':
                    return {'run_id':run_id,'outcome':'stale'}
                conn.execute('UPDATE action_dispatches SET reconciliation_requests=reconciliation_requests+1 WHERE action_id=%s',
                             (command.action_id,))
            requests += 1
            grant_effects.observe({'event':'reconciliation.request','run_id':run_id,'action_id':command.action_id,
                'execution_generation':run['execution_generation'],'payload':{'round':row['reconciliation_rounds'],
                    'ordinal':requests,'timeout_seconds':2,'tool_rounds':run['tool_rounds'],
                    'model_attempts':run['model_attempts'],'budget_origin_at':run['budget_origin_at'].isoformat(),
                    'budget_ms':run['budget_ms'],'admission_state':run['admission_state']}})
            response = client.receipt(command,grant_effects._context(run),timeout=2)
            row = grant_effects.record_response(conn,run,command,response)
            if row['status'] == 'committed':
                grant_effects.after_receipt_commit(conn,run,row['receipt'])
        if row['status'] == 'committed':
            if not row.get('verification') or row['verification']['status'] not in {'verified_current','verified_historical'}:
                if requests < 2:
                    row = grant_effects.verify(conn,run,row,normal=False,client=client)
                    requests += 1
            if row.get('verification') and row['verification']['status'] in {'verified_current','verified_historical'}:
                with conn.transaction():
                    grant_effects._owned(conn,run)
                    conn.execute('UPDATE action_dispatches SET last_error_code=NULL WHERE action_id=%s',(command.action_id,))
                    row = grant_effects.read_dispatch(conn,run)
        try:
            with event_hints.transaction(conn):
                current = grant_effects._owned(conn,run)
                outcome = grant_effects.effect_outcome(conn,current,row=row)
                if (current['status'],current['reason_code'],current.get('result')) == (outcome.status,outcome.reason_code,outcome.result):
                    finalized = current
                else:
                    finalized = finish_locked(conn,current,outcome)
        except Exception as error:
            if conn.closed or conn.broken:
                raise
            trace('finalization_pending',run_id=run_id,error_type=type(error).__name__)
            with event_hints.transaction(conn):
                current = grant_effects._owned(conn,run)
                pending = grant_effects.mark_finalization_pending(conn,current)
                if pending is None:
                    raise
                finalized = finish_locked(conn,current,pending)
        if finalized['status'] in CLOSED:
            grant_effects.after_finalize_commit(conn,finalized,outcome)
        return {'run_id':run_id,'outcome':'reconciled','status':finalized['status'],
                'reason_code':finalized['reason_code'],'requests':requests,'round':row['reconciliation_rounds']}
    finally:
        unlock(conn,run_id)


def reconcile_once(conn,*,limit=1):
    if type(limit) is not int or not 1 <= limit <= 4:
        raise ValueError('Invalid reconciliation batch')
    rows = conn.execute('''SELECT r.run_id FROM runs r JOIN action_dispatches d USING(tenant_id,run_id)
        WHERE r.status='needs_reconciliation' AND d.finalized_at IS NULL AND d.status IN ('sending','unknown','committed')
        AND d.next_reconcile_at<=clock_timestamp() ORDER BY d.next_reconcile_at,r.run_id LIMIT %s''',(limit,)).fetchall()
    results = []
    for row in rows:
        result = reconcile_one(conn,row['run_id'])
        trace('reconciliation',**result)
        results.append(result)
    return results
