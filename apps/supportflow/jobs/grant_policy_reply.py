"""Publish no-effect policy answers only after current authorization/evidence checks."""
from apps.supportflow.domain.grant_policy_reply import build_reply, enabled, verify_reply
from .contracts import FragmentOutcome, StopFragment
from .proposal_waiting import candidate_context
from psycopg.types.json import Jsonb


def context_and_customer(conn, run, evidence_refs, observation_refs):
    if not enabled(run['config_snapshot']):
        raise ValueError('Policy reply contract is absent')
    context = candidate_context(conn, run, evidence_refs=evidence_refs, observation_refs=observation_refs)
    customer = conn.execute('SELECT customer_id,customer_code,display_name FROM customers '
        'WHERE tenant_id=%s AND customer_id=%s FOR SHARE', (run['tenant_id'], context['customer_id'])).fetchone()
    if customer is None:
        raise ValueError('Customer disappeared')
    if conn.execute('SELECT 1 FROM actions WHERE run_id=%s LIMIT 1', (run['run_id'],)).fetchone():
        raise ValueError('A no-effect reply cannot replace an existing action')
    return context, customer


def prepare(conn, run, evidence_refs, observation_refs):
    context, customer = context_and_customer(conn, run, evidence_refs, observation_refs)
    result = build_reply(context, observation_refs, customer)
    return FragmentOutcome('succeeded', result.resolution_code, result.model_dump(mode='json'))


def validate_publication(conn, run, outcome):
    try:
        value = outcome.result
        context, customer = context_and_customer(conn, run, value['evidence_refs'], value['observation_refs'])
        verify_reply(value, context, customer)
        if (outcome.status, outcome.reason_code) != ('succeeded','CAPACITY_NOT_EXHAUSTED'):
            raise ValueError('Unexpected terminal outcome')
        return outcome
    except (ValueError, TypeError, KeyError, StopFragment):
        return FragmentOutcome('handoff','EVIDENCE_REQUIRED')


def finalize_readonly(conn, run, outcome):
    """Same final transaction, immutable run audit, no synthetic action/receipt."""
    value = outcome.result
    if (not enabled(run['config_snapshot']) or outcome.status != 'succeeded'
            or not isinstance(value, dict) or value.get('schema_version') not in {5,7}
            or value.get('action_receipts') != []):
        return
    ticket = conn.execute('''SELECT t.ticket_id FROM threads h JOIN tickets t USING(tenant_id,ticket_id)
        WHERE h.tenant_id=%s AND h.thread_id=%s AND h.actor_id=%s FOR UPDATE OF t''',
        (run['tenant_id'],run['thread_id'],run['actor_id'])).fetchone()
    inserted = conn.execute('''INSERT INTO ticket_resolution_audits(run_id,tenant_id,ticket_id,action_id,status,resolution_code,result)
        VALUES(%s,%s,%s,NULL,%s,%s,%s) ON CONFLICT(run_id) DO NOTHING RETURNING run_id''',
        (run['run_id'],run['tenant_id'],ticket['ticket_id'],outcome.status,outcome.reason_code,Jsonb(value))).fetchone()
    if inserted:
        # A later read cannot erase a prior committed effect or human fields.
        conn.execute('''UPDATE tickets SET agent_resolution_code=%s,agent_effect_status='none',
            agent_resolved_at=clock_timestamp(),version=version+1
            WHERE tenant_id=%s AND ticket_id=%s AND agent_action_id IS NULL''',
            (outcome.reason_code,run['tenant_id'],ticket['ticket_id']))
