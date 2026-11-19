"""Stored approval resume and the app-side pre-dispatch authorization point.

The final read is a real, counted get_subscription round. The short authorization
transaction never performs HTTP. This stage records permission only: there is no
grant transport, receipt, or claim that the business effect happened.
"""
from datetime import datetime
import json
from typing import Annotated, Literal
from uuid import uuid4

from pydantic import Field
from apps.supportflow.domain.clock import business_now
from apps.supportflow.domain.contracts import Identifier, StrictModel, ToolEnvelope
from apps.supportflow.domain.grant_contracts import GrantSubscriptionObservation
from apps.supportflow.domain.proposals import command_value, public_proposal
from apps.supportflow.tools.readonly import ToolExecutor
from . import proposal_waiting
from .contracts import StopFragment


class ApprovalRefreshRef(StrictModel):
    """Private seventh-round binding for the current subscription refresh."""
    run_id: Identifier
    execution_generation: Annotated[int, Field(ge=1)]
    ordinal: Annotated[int, Field(ge=1, le=7)]
    tool_id: Literal['get_subscription']


class ExecutionRefreshRef(ApprovalRefreshRef):
    """Version 2 grants can spend one extra read round before the write."""
    ordinal: Annotated[int, Field(ge=1, le=8)]


def read_resume(conn, run, *, approval_id):
    """Build the resume value only from the committed approval ledger."""
    row = conn.execute("""SELECT * FROM approvals
        WHERE tenant_id=%s AND run_id=%s AND approval_id=%s""",
        (run['tenant_id'], run['run_id'], approval_id)).fetchone()
    if row is None or row['status'] != 'approved':
        return None
    if (row.get('decision') != 'approve' or type(row.get('version')) is not int or row['version'] < 2
            or type(row.get('resume_generation')) is not int
            or not 2 <= row['resume_generation'] <= run['execution_generation']
            or not isinstance(row.get('decided_by'), str) or row['decided_by'] == run['actor_id']
            or not isinstance(row.get('decided_at'), datetime) or row['decided_at'].tzinfo is None):
        raise StopFragment('APPROVAL_RESUME_INVALID')
    return {'kind':'approval', 'approval_id':approval_id, 'decision':'approve',
            'version':row['version'], 'resume_generation':row['resume_generation']}


def _command(proposal):
    try:
        public_proposal(proposal)
        return command_value(proposal['canonical_args'])
    except (KeyError, ValueError, TypeError, OverflowError):
        raise StopFragment('PROPOSAL_IMMUTABLE') from None


def existing_authorization(conn, run, proposal, *, authorization_id=None):
    row = conn.execute("""SELECT * FROM dispatch_authorizations
        WHERE tenant_id=%s AND run_id=%s AND proposal_id=%s""",
        (run['tenant_id'], run['run_id'], proposal['proposal_id'])).fetchone()
    if row is None:
        return None
    command = _command(proposal)
    expected = {'tenant_id':run['tenant_id'], 'run_id':run['run_id'], 'requested_by':run['actor_id'],
        'proposal_id':proposal['proposal_id'], 'approval_id':proposal['approval_id'],
        'action_id':proposal['action_id'], 'canonical_args_hash':proposal['canonical_args_hash'],
        'policy_version':command.policy_version, 'business_version':command.business_version}
    if (not isinstance(row.get('authorization_id'),str) or not row['authorization_id']
            or any(row.get(key) != value for key,value in expected.items())
            or type(row.get('execution_generation')) is not int
            or not 2 <= row['execution_generation'] <= run['execution_generation']
            or authorization_id is not None and row.get('authorization_id') != authorization_id):
        raise StopFragment('CHECKPOINT_INCOMPATIBLE')
    approval = conn.execute("""SELECT status,decision,decided_by FROM approvals
        WHERE tenant_id=%s AND run_id=%s AND approval_id=%s""",
        (run['tenant_id'], run['run_id'], row['approval_id'])).fetchone()
    if (approval is None or approval['status'] != 'approved' or approval['decision'] != 'approve'
            or row.get('approved_by') != approval['decided_by']):
        raise StopFragment('CHECKPOINT_INCOMPATIBLE')
    return row


def _fresh_subscription(conn, run, ref):
    try:
        from apps.supportflow.domain.approvals import execution_compatible_config
        config = run.get('config_snapshot', {})
        contract = ExecutionRefreshRef if execution_compatible_config(config) and config['execution_contract_version'] == 2 else ApprovalRefreshRef
        value = contract.model_validate_json(json.dumps(ref)).model_dump()
        if (value['run_id'] != run['run_id'] or value['execution_generation'] != run['execution_generation']
                or value['tool_id'] != 'get_subscription'):
            raise ValueError('Invalid refresh binding')
        row = conn.execute("""SELECT tool_id,envelope FROM run_tool_observations
            WHERE run_id=%s AND execution_generation=%s AND ordinal=%s""",
            (run['run_id'], value['execution_generation'], value['ordinal'])).fetchone()
        if row is None or row['tool_id'] != 'get_subscription':
            raise ValueError('Missing refresh')
        envelope = ToolEnvelope.model_validate_json(json.dumps(row['envelope']))
        if not envelope.ok or envelope.source != 'sandbox.subscription_grant':
            raise ValueError('Invalid refresh source')
        return GrantSubscriptionObservation.model_validate_json(json.dumps(envelope.data))
    except (KeyError, ValueError, TypeError, OverflowError):
        raise StopFragment('FACTS_INCOMPLETE') from None


def before_authorize(conn, run, proposal):
    """No-op in production. A private isolated test bootstrap may pause here."""


def after_authorization_commit(conn, run, authorization):
    """No-op in production; the authorization transaction has already exited."""


def authorize(conn, run, proposal_id, fresh_ref):
    """Record SF-D12 after all app checks, using the fixed lock connection."""
    from .core import Gate, lock_final_scope
    with conn.transaction():
        current = Gate(conn, run['run_id'], run['execution_generation'])._current()
        # Gate locks runtime_limits then run. Domain rows follow that order.
        approval = conn.execute("""SELECT * FROM approvals
            WHERE tenant_id=%s AND run_id=%s AND proposal_id=%s FOR UPDATE""",
            (run['tenant_id'], run['run_id'], proposal_id)).fetchone()
        action = conn.execute("""SELECT * FROM actions
            WHERE tenant_id=%s AND run_id=%s AND proposal_id=%s FOR UPDATE""",
            (run['tenant_id'], run['run_id'], proposal_id)).fetchone()
        proposal = proposal_waiting.prepared(conn, current, proposal_id=proposal_id)
        if (approval is None or action is None or proposal is None
                or approval['status'] != 'approved' or approval.get('decision') != 'approve'
                or action['status'] != 'proposed'):
            raise StopFragment('APPROVAL_RESUME_INVALID')
        command = _command(proposal)
        if (command.tenant_id != current['tenant_id'] or command.customer_id != current['customer_id']
                or approval['approval_id'] != proposal['approval_id']
                or action['action_id'] != proposal['action_id']):
            raise StopFragment('PROPOSAL_IMMUTABLE')
        if not lock_final_scope(conn, current):
            raise StopFragment('AUTHORIZATION_REVOKED')
        approver = conn.execute("""SELECT m.role FROM memberships m
            JOIN customer_scopes s ON s.tenant_id=m.tenant_id AND s.user_id=m.user_id
            WHERE m.tenant_id=%s AND m.user_id=%s AND s.customer_id=%s
            FOR SHARE OF m,s""", (current['tenant_id'], approval['decided_by'], command.customer_id)).fetchone()
        if (approver is None or approver['role'] != 'supervisor'
                or approval['decided_by'] == current['actor_id']):
            raise StopFragment('APPROVAL_AUTHORIZATION_REVOKED')
        now = conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
        if business_now(real_now=now) >= command.expires_at:
            raise StopFragment('PROPOSAL_EXPIRED')
        subscription = _fresh_subscription(conn, current, fresh_ref)
        from apps.supportflow.domain.grant_policy import load_policy_bundle
        try:
            if subscription.policy.model_dump(mode='json') != load_policy_bundle():
                raise StopFragment('POLICY_INVALID')
        except (ValueError, TypeError, OSError, OverflowError):
            raise StopFragment('POLICY_INVALID') from None
        if subscription.subscription_id != command.subscription_id:
            raise StopFragment('PROPOSAL_IMMUTABLE')
        if subscription.business_version != command.business_version:
            raise StopFragment('BUSINESS_VERSION_CHANGED')
        decision = proposal_waiting.evaluate_candidate(conn, current, evidence_refs=proposal['evidence_refs'],
            observation_refs=[*proposal['observation_refs'], fresh_ref])
        if decision.decision != 'proposal':
            raise StopFragment(decision.reason_code or 'EVIDENCE_REQUIRED')
        if (command.model_dump(exclude={'created_at','expires_at'})
                != decision.command.model_dump(exclude={'created_at','expires_at'})):
            raise StopFragment('PROPOSAL_IMMUTABLE')
        # Acquiring the current knowledge/policy locks above may have waited.
        # Eligibility must not extend the immutable proposal's original expiry;
        # bind the authorization timestamp to the final, locked record point.
        now = conn.execute('SELECT clock_timestamp() AS now').fetchone()['now']
        if business_now(real_now=now) >= command.expires_at:
            raise StopFragment('PROPOSAL_EXPIRED')
        row = conn.execute("""INSERT INTO dispatch_authorizations
            (authorization_id,tenant_id,run_id,proposal_id,approval_id,action_id,canonical_args_hash,
             execution_generation,requested_by,approved_by,authorized_at,policy_version,business_version)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (str(uuid4()),current['tenant_id'],current['run_id'],proposal_id,approval['approval_id'],
             action['action_id'],proposal['canonical_args_hash'],current['execution_generation'],
             current['actor_id'],approval['decided_by'],now,command.policy_version,command.business_version)).fetchone()
        conn.execute("UPDATE actions SET status='dispatch_authorized' WHERE action_id=%s",(action['action_id'],))
    return row


def resume_approved(conn, run, gate, proposal, value):
    stored = read_resume(conn, run, approval_id=proposal['approval_id'])
    if (stored is None or type(value) is not dict or set(value) != set(stored)
            or any(type(value[key]) is not type(expected) or value[key] != expected
                   for key,expected in stored.items())):
        raise StopFragment('APPROVAL_RESUME_INVALID')
    existing = existing_authorization(conn, run, proposal)
    if existing is not None:
        # A crash between app commit and graph checkpoint must not spend an
        # eighth round or create another authorization. No write was sent.
        return {'authorization_id':existing['authorization_id']}
    envelope, refs = ToolExecutor(conn,run,gate,grant_subscription=True).execute('get_subscription')
    if not envelope.ok or not refs:
        raise StopFragment('FACTS_INCOMPLETE')
    before_authorize(conn, run, proposal)
    authorization = authorize(conn, run, proposal['proposal_id'], refs[-1])
    after_authorization_commit(conn, run, authorization)
    return {'authorization_id':authorization['authorization_id']}
