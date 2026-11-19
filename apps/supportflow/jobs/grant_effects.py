"""One durable send intent, effect-first recovery and atomic app finalization.

This module has no worker import, credential reads or network activity at import.
The public cancel path uses only read_cancel_effect, under its existing locks.
"""
from datetime import datetime, timezone
import json

from psycopg.types.json import Jsonb

from apps.supportflow.domain.approvals import execution_compatible_config
from apps.supportflow.domain.clock import business_now
from apps.supportflow.domain.contracts import Customer, Subscription
from apps.supportflow.domain.grant_commands import GrantCommand
from apps.supportflow.domain.grant_effects import (
    GRANT_REPLY_ACKNOWLEDGEMENT, GRANT_REPLY_PUBLIC_OPENING, GrantExecutionResult, GrantVerification,
    streamed_grant_reply_enabled,
)
from apps.supportflow.tools.readonly import compact
from .contracts import FragmentOutcome, StaleFragment, StopFragment
from .action_effects import classify, read_cancel_effect
from .grant_transport import GrantClient, bound_receipt


def before_send(conn, run, proposal):
    """Production noop; before the last cancellable, no-intent transaction."""


def before_intent_commit(conn, run, command):
    """Production noop; throwing here rolls back both ordinal eight and intent."""


def after_intent_commit(conn, run, command):
    """Production noop; crashes from here onward are conservatively potential."""


def after_http_response(conn, run, response):
    """Production noop; a response may exist before its durable app receipt."""


def after_receipt_commit(conn, run, receipt):
    """Production noop; receipt is durable, graph checkpoint may still be absent."""


def before_finalize(conn, run, outcome):
    """Production noop, inside the app finalization transaction before commit."""


def after_finalize_commit(conn, run, outcome):
    """Production noop; called only after the final app transaction commits."""


def observe(record):
    """Production noop; private collectors record raw counters/lock ownership."""


def read_dispatch(conn, run, *, lock=False):
    if not execution_compatible_config(run.get('config_snapshot')):
        return None
    return conn.execute('SELECT * FROM action_dispatches WHERE tenant_id=%s AND run_id=%s'+
                        (' FOR UPDATE' if lock else ''), (run['tenant_id'],run['run_id'])).fetchone()


def _command(row):
    command = GrantCommand.model_validate_json(compact(row['command']))
    command.validate_canonical_hash()
    if any(getattr(command,key) != row[key] for key in
           ('action_id','tenant_id','authorization_id','canonical_args_hash')):
        raise ValueError('Stored dispatch identity is incompatible')
    return command


def _context(run):
    return {key:run[key] for key in ('run_id','tenant_id','actor_id','execution_generation')}


def validate_send_evidence(conn,run,proposal,command):
    """A historical authorization cannot bypass current citation withdrawal.

    Retired index generations remain valid; the referenced policy's document
    version, ACL and complete pinned section must still be current.
    """
    from apps.supportflow.retrieval.service import KnowledgeError, reviewed_support, validate_refs
    from apps.supportflow.domain.grant_policy import load_policy_bundle
    try:
        rows=validate_refs(conn,run,proposal['evidence_refs'],current_generation=False,lock=True)
        reviewed_support(rows)
    except KnowledgeError:
        raise StopFragment('EVIDENCE_REQUIRED') from None
    try:
        pin=load_policy_bundle()
    except (ValueError,TypeError,OSError,OverflowError):
        raise StopFragment('POLICY_INVALID') from None
    if (command.policy_version != pin['policy_version'] or not rows
            or any(row['active_version_id'] != row['version_id'] for row in rows)
            or not any(row['section_id']==pin['section_id'] and row['content_sha256']==pin['content_sha256']
                       and row['section_content_sha256']==pin['content_sha256'] for row in rows)):
        raise StopFragment('EVIDENCE_REQUIRED')


def result_for(conn, run, row, *, resolution=None):
    command = _command(row)
    phase = classify(row)['phase']
    receipt = bound_receipt(command,row['receipt']) if phase == 'committed' else None
    verification = GrantVerification.model_validate_json(compact(row['verification'])) if row.get('verification') else None
    if resolution is None:
        if phase == 'committed':
            resolution = ('VERIFICATION_MISMATCH' if verification and verification.status == 'mismatch' else
                          'GRANT_COMMITTED_CANCELLED' if run['cancel_requested'] else 'GRANT_TEMPORARY_SEATS_SUCCEEDED')
        else:
            resolution = 'UNKNOWN_COMMIT' if phase == 'potential' else row.get('last_error_code') or 'GRANT_REJECTED'
    customer = conn.execute('SELECT customer_id,customer_code,display_name FROM customers WHERE tenant_id=%s AND customer_id=%s',
                            (command.tenant_id,command.customer_id)).fetchone()
    proposal = conn.execute('SELECT evidence_refs FROM proposals WHERE tenant_id=%s AND run_id=%s AND proposal_id=%s',
                            (run['tenant_id'],run['run_id'],command.proposal_id)).fetchone()
    if phase == 'potential':
        reply = '本次临时席位操作的提交结果尚未确认，系统正在按原动作编号只读核对。请勿重复提交。'
    elif phase == 'rejected':
        reply = '本次临时席位操作已停止，未获得提交回执，请转人工处理。'
    elif verification is None or verification.status == 'pending':
        reply = '临时席位已获得提交回执，当前订阅核验或本地最终化尚待完成。'
    elif verification.status == 'mismatch':
        reply = '临时席位已有提交回执，但当前订阅与回执不一致，已保留双方事实并继续核对。'
    else:
        reply = f'已确认本次操作提交了 {receipt.seats} 个临时席位，期限 {receipt.days} 天，到期时间 {receipt.temporary_expires_at.isoformat()}。'
        reply += f'回执编号：{receipt.action_id}。'
        if verification.status == 'verified_historical':
            reply += '该回执证明历史提交；当前订阅可能已有后续变更或临时额度已自然到期。'
        if run['cancel_requested']:
            reply += '取消请求已记录；此前已提交的席位变更保留。'
    return GrantExecutionResult(resolution_code=resolution,
        customer=Customer.model_validate(customer) if customer else None,
        effect_status={'potential':'unknown','committed':'committed','rejected':'none'}[phase],
        action_id=command.action_id, action_receipts=[receipt] if receipt else [],
        evidence_refs=proposal['evidence_refs'] if proposal else [], verification=verification,
        reply_text=reply).model_dump(mode='json')


def effect_outcome(conn, run, *, row=None, reason=None):
    row = row if row is not None else read_dispatch(conn,run)
    if row is None:
        return None
    phase = classify(row)['phase']
    if phase == 'potential':
        return FragmentOutcome('needs_reconciliation',reason or 'UNKNOWN_COMMIT',result_for(conn,run,row,resolution='UNKNOWN_COMMIT'))
    if phase == 'rejected':
        code = row.get('last_error_code') or 'GRANT_REJECTED'
        return FragmentOutcome('cancelled' if code == 'CANCEL_REQUESTED' else 'handoff',code,result_for(conn,run,row))
    if row.get('last_error_code') == 'FINALIZATION_PENDING':
        return FragmentOutcome('needs_reconciliation','FINALIZATION_PENDING',
                               result_for(conn,run,row,resolution='FINALIZATION_PENDING'))
    verification = row.get('verification')
    if not verification or verification['status'] == 'pending':
        return FragmentOutcome('needs_reconciliation',reason or 'FINALIZATION_PENDING',
                               result_for(conn,run,row,resolution='FINALIZATION_PENDING'))
    if verification['status'] == 'mismatch':
        return FragmentOutcome('needs_reconciliation','VERIFICATION_MISMATCH',result_for(conn,run,row))
    status, code = ('cancelled','GRANT_COMMITTED_CANCELLED') if run['cancel_requested'] else ('succeeded','GRANT_TEMPORARY_SEATS_SUCCEEDED')
    return FragmentOutcome(status,code,result_for(conn,run,row,resolution=code))


def protect_outcome(conn, run, outcome):
    """A stop, stale checkpoint or missing result cannot erase a possible effect."""
    row = read_dispatch(conn,run,lock=True)
    if row is None:
        return outcome
    trusted = effect_outcome(conn,run,row=row)
    config = run['config_snapshot']
    opening = None
    if config.get('grant_reply_mode') == 'assisted':
        opening = GRANT_REPLY_ACKNOWLEDGEMENT
    elif streamed_grant_reply_enabled(config):
        opening = GRANT_REPLY_PUBLIC_OPENING
    if opening is not None and trusted.status == outcome.status == 'succeeded' and trusted.reason_code == outcome.reason_code:
        # Preserve only the allowed opening over the complete, newly recomputed
        # result. Cancellation or any change in facts keeps the trusted text.
        allowed = {**trusted.result,
                   'reply_text': opening + trusted.result['reply_text']}
        if outcome.result == allowed:
            return outcome
    return trusted


def mark_finalization_pending(conn,run):
    row = read_dispatch(conn,run,lock=True)
    if row is None or row['status'] != 'committed':
        return None
    conn.execute("UPDATE action_dispatches SET last_error_code='FINALIZATION_PENDING' WHERE action_id=%s",(row['action_id'],))
    return effect_outcome(conn,run)


def _owned(conn, run):
    from .core import locked_run, owns_lock
    if not owns_lock(conn,run['run_id']):
        raise StaleFragment('Run lock missing')
    _, current = locked_run(conn,run['run_id'])
    if current is None or current['execution_generation'] != run['execution_generation']:
        raise StaleFragment('Execution generation changed')
    return current


def before_command_validation(conn,run,command):
    """Identity in production; isolated instrumentation can observe this boundary."""
    return command


def validate_command_hash(command):
    """A stale/tampered wire command is a domain stop, not a retryable crash."""
    try:
        command.validate_canonical_hash()
    except ValueError:
        raise StopFragment('PROPOSAL_IMMUTABLE') from None


def send(conn, run, gate, proposal, authorization_id, *, client=None):
    """Reserve the actual POST ordinal and one intent atomically, send once."""
    from . import approval_dispatch
    from .core import Gate, lock_final_scope, remaining_seconds
    existing = read_dispatch(conn,run)
    if existing is not None:
        return existing
    client = client or GrantClient()
    before_send(conn,run,proposal)
    with conn.transaction():
        current = Gate(conn,run['run_id'],run['execution_generation'])._current()
        allowed_before_send = {7, 8} if current['config_snapshot'].get('execution_contract_version') == 2 else {7}
        if not execution_compatible_config(current['config_snapshot']) or current['tool_rounds'] not in allowed_before_send:
            raise StopFragment('TOOL_ROUNDS_EXHAUSTED')
        authorization = approval_dispatch.existing_authorization(conn,current,proposal,authorization_id=authorization_id)
        if authorization is None:
            raise StopFragment('APPROVAL_RESUME_INVALID')
        if not lock_final_scope(conn,current):
            raise StopFragment('AUTHORIZATION_REVOKED')
        approver = conn.execute('''SELECT m.role FROM memberships m JOIN customer_scopes s
            ON s.tenant_id=m.tenant_id AND s.user_id=m.user_id WHERE m.tenant_id=%s AND m.user_id=%s
            AND s.customer_id=%s FOR SHARE OF m,s''',
            (current['tenant_id'],authorization['approved_by'],current['customer_id'])).fetchone()
        if approver is None or approver['role'] != 'supervisor' or authorization['approved_by'] == current['actor_id']:
            raise StopFragment('APPROVAL_AUTHORIZATION_REVOKED')
        command = GrantCommand.from_proposal(proposal['canonical_args'], action_id=proposal['action_id'],
            proposal_id=proposal['proposal_id'], authorization_id=authorization['authorization_id'],
            authorized_at=authorization['authorized_at'])
        command = before_command_validation(conn,current,command)
        validate_command_hash(command)
        validate_send_evidence(conn,current,proposal,command)
        now = conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
        if business_now(real_now=now) >= command.proposal_expires_at:
            raise StopFragment('PROPOSAL_EXPIRED')
        ordinal = current['tool_rounds'] + 1
        conn.execute('UPDATE runs SET tool_rounds=%s,heartbeat_at=clock_timestamp() WHERE run_id=%s',(ordinal,run['run_id']))
        row = conn.execute('''INSERT INTO action_dispatches(action_id,tenant_id,run_id,authorization_id,
            canonical_args_hash,execution_generation,command,tool_ordinal,status) VALUES(%s,%s,%s,%s,%s,%s,%s,%s,'sending') RETURNING *''',
            (command.action_id,current['tenant_id'],current['run_id'],command.authorization_id,
             command.canonical_args_hash,current['execution_generation'],Jsonb(command.model_dump(mode='json')),ordinal)).fetchone()
        before_intent_commit(conn,current,command)
    after_intent_commit(conn,run,command)
    # A cancellation discovered while this same fragment knows it has not issued
    # HTTP is a proved no-send. A crash in this gap remains conservatively unknown.
    with conn.transaction():
        try:
            current = Gate(conn,run['run_id'],run['execution_generation'])._current()
            timeout = min(8,remaining_seconds(conn,current))
            if business_now() >= command.proposal_expires_at:
                raise StopFragment('PROPOSAL_EXPIRED')
        except StopFragment as error:
            conn.execute("UPDATE action_dispatches SET status='rejected',last_error_code=%s WHERE action_id=%s AND status='sending'",
                         (error.reason_code,command.action_id))
            return read_dispatch(conn,run)
    response = client.grant(command,_context(run),timeout=timeout)
    after_http_response(conn,run,response)
    row = record_response(conn,run,command,response,allow_rejection=True)
    if row['status'] == 'committed':
        after_receipt_commit(conn,run,row['receipt'])
    return row


def record_response(conn, run, command, response, *, allow_rejection=False):
    with conn.transaction():
        _owned(conn,run)
        row = read_dispatch(conn,run,lock=True)
        if row['status'] == 'committed':
            return row
        if response.error_code is None and response.envelope is not None:
            if response.status in (200,201) and response.envelope.ok:
                receipt = bound_receipt(command,response.envelope.data)
                conn.execute('''UPDATE action_dispatches SET status='committed',receipt=%s,
                    receipt_recorded_at=clock_timestamp(),last_error_code=NULL WHERE action_id=%s''',
                    (Jsonb(receipt.model_dump(mode='json')),command.action_id))
            elif (allow_rejection and response.status in (401,409,422) and not response.envelope.ok
                  and row['status'] == 'sending'):
                conn.execute("UPDATE action_dispatches SET status='rejected',last_error_code=%s WHERE action_id=%s",
                             (response.envelope.error_code,command.action_id))
            else:
                conn.execute("UPDATE action_dispatches SET status='unknown',last_error_code='UNKNOWN_COMMIT' WHERE action_id=%s",
                             (command.action_id,))
        else:
            conn.execute("UPDATE action_dispatches SET status='unknown',last_error_code='UNKNOWN_COMMIT' WHERE action_id=%s",
                         (command.action_id,))
        return read_dispatch(conn,run)


def verify_receipt(receipt, subscription, observed_at, *, now):
    """A newer resource version or natural expiry preserves historical success."""
    if subscription.subscription_id != receipt.subscription_id or subscription.business_version < receipt.after_version:
        status = 'mismatch'
    elif subscription.business_version > receipt.after_version:
        status = 'verified_historical'
    elif (subscription.temporary_seats != receipt.seats or subscription.temporary_expires_at != receipt.temporary_expires_at):
        status = 'mismatch'
    else:
        status = 'verified_historical' if now >= receipt.temporary_expires_at else 'verified_current'
    return GrantVerification(status=status,subscription=subscription,observed_at=observed_at)


def verify(conn, run, row, *, normal=False, client=None):
    """Normal verify is tool nine. Reconciliation has its own bounded counter."""
    from .core import Gate, remaining_seconds
    command = _command(row)
    receipt = bound_receipt(command,row['receipt'])
    if row.get('verification') and row['verification']['status'] in {'verified_current','verified_historical'}:
        return row
    with conn.transaction():
        current = _owned(conn,run)
        if normal:
            # Effect reads remain allowed after a concurrent cancel; the original
            # processing budget and exact nine-round cap still apply.
            maximum = current['config_snapshot']['tool_round_limit']
            if current['tool_rounds'] >= maximum or remaining_seconds(conn,current) <= 0:
                return row
            if current['tool_rounds'] != row.get('tool_ordinal', 8):
                raise StopFragment('TOOL_ROUNDS_EXHAUSTED')
            conn.execute('UPDATE runs SET tool_rounds=%s,heartbeat_at=clock_timestamp() WHERE run_id=%s',(current['tool_rounds']+1,run['run_id']))
            timeout = min(8,remaining_seconds(conn,current))
        else:
            timeout = 2
            conn.execute('UPDATE action_dispatches SET reconciliation_requests=reconciliation_requests+1 WHERE action_id=%s',
                         (command.action_id,))
    response = (client or GrantClient()).subscription(command,_context(run),timeout=max(.001,timeout))
    verification = GrantVerification(status='pending',subscription=None,observed_at=None)
    if response.error_code is None and response.status == 200 and response.envelope is not None and response.envelope.ok:
        subscription = Subscription.model_validate_json(compact(response.envelope.data))
        verification = verify_receipt(receipt,subscription,response.envelope.observed_at,now=business_now())
    with conn.transaction():
        _owned(conn,run)
        conn.execute('UPDATE action_dispatches SET verification=%s WHERE action_id=%s',
                     (Jsonb(verification.model_dump(mode='json')),command.action_id))
        return read_dispatch(conn,run)


def finalize_domain(conn, run, outcome):
    """Caller commits run/ticket/audit/final events together under limits/run CAS."""
    row = read_dispatch(conn,run,lock=True)
    if row is None:
        from .grant_policy_reply import finalize_readonly
        finalize_readonly(conn,run,outcome)
    if row is None or row['status'] != 'committed' or outcome.status not in {'succeeded','cancelled'}:
        return
    ticket = conn.execute('''SELECT t.ticket_id FROM threads h JOIN tickets t USING(tenant_id,ticket_id)
        WHERE h.tenant_id=%s AND h.thread_id=%s AND h.actor_id=%s FOR UPDATE OF t''',
        (run['tenant_id'],run['thread_id'],run['actor_id'])).fetchone()
    inserted = conn.execute('''INSERT INTO ticket_resolution_audits(run_id,tenant_id,ticket_id,action_id,status,resolution_code,result)
        VALUES(%s,%s,%s,%s,%s,%s,%s) ON CONFLICT(run_id) DO NOTHING RETURNING run_id''',
        (run['run_id'],run['tenant_id'],ticket['ticket_id'],row['action_id'],outcome.status,outcome.reason_code,Jsonb(outcome.result))).fetchone()
    if inserted is not None:
        conn.execute('''UPDATE tickets SET agent_resolution_code=%s,agent_action_id=%s,agent_effect_status='committed',
            agent_resolved_at=clock_timestamp(),version=version+1 WHERE tenant_id=%s AND ticket_id=%s''',
            (outcome.reason_code,row['action_id'],run['tenant_id'],ticket['ticket_id']))
        from .core import append_event
        event_run = conn.execute('UPDATE runs SET last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *',(run['run_id'],)).fetchone()
        append_event(conn,event_run,'action.completed',{'action_id':row['action_id'],'effect_status':'committed','receipt_action_id':row['action_id']})
        conn.execute('UPDATE action_dispatches SET finalized_at=clock_timestamp() WHERE action_id=%s',(row['action_id'],))
        before_finalize(conn,run,outcome)
