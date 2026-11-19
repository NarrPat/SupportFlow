"""Pure, server-owned grant eligibility; no database, model or business writes.

Inputs must be read under the caller's current tenant/customer authorization.
The graph repeats this function after re-reading current facts and authorized
references under its final transaction. Passing this function is not approval.
The policy pin and full section originate in the frozen SF-FX-GRANT-PROPOSAL
design; runtime code never imports or reads evaluation data.
"""
from copy import deepcopy
from datetime import UTC, datetime, timedelta
import hashlib
import json
from pathlib import Path
from typing import Literal

from pydantic import BaseModel, Field

from .contracts import EvidenceBundle, InvitationObservation, ServiceStatus, StrictModel
from .grant_contracts import GrantConfirmedFields, GrantHistory, GrantPolicyBinding, GrantSubscriptionObservation
from .proposals import ProposalCommand
from .rules import domain_allowed, invitation_codes


_POLICY_PATH = Path(__file__).with_name('grant_policy_bundle.json')
_SECTION_PATH = Path(__file__).resolve().parents[1] / 'knowledge/documents/temporary-seat-policy-v1.md'
_PIN_SHA256 = '89310ab4bedb6b246ab9367e75082acb0d594c5b13ed374273a1209c057695e6'
_SECTION_LINES = (1, 10)
_CONSTANTS = {
    'action_type': 'grant_temporary_seats', 'command_schema_version': 1,
    'seats_min': 1, 'seats_max': 20, 'days_min': 1, 'days_max': 7,
    'required_plan_status': 'active', 'cooldown_seconds': 2592000,
    'proposal_ttl_seconds': 900, 'day_seconds': 86400, 'minimum_remaining_seats': 1,
    'temporary_grant_mode': 'replace_expired', 'approval_role': 'supervisor',
    'self_approval_allowed': False,
}


def _parsed(model, value):
    # Revalidation prevents model_construct or a mutable DTO from bypassing checks.
    if isinstance(value, BaseModel):
        value = value.model_dump(mode='json')
    return model.model_validate_json(json.dumps(value, ensure_ascii=False, allow_nan=False))


def load_policy_bundle() -> dict:
    """Return a fresh, validated product pin; never a caller-supplied policy."""
    value = _parsed(GrantPolicyBinding, json.loads(_POLICY_PATH.read_text(encoding='utf-8')))
    span = '\n'.join(_SECTION_PATH.read_text(encoding='utf-8').splitlines())
    if (value.policy_version != 'v1' or value.section_id != 'policy.temporary_seats.v1'
            or value.content_sha256 != _PIN_SHA256
            or value.constants.model_dump() != _CONSTANTS
            or hashlib.sha256(span.encode('utf-8')).hexdigest() != _PIN_SHA256
            or len(span.splitlines()) != _SECTION_LINES[1]):
        raise ValueError('Product policy pin is inconsistent')
    return deepcopy(value.model_dump(mode='json'))


class GrantDecision(StrictModel):
    decision: Literal['not_requested', 'input_required', 'handoff', 'proposal']
    reason_code: str | None
    required_fields: list[str] = Field(default_factory=list)
    command: ProposalCommand | None = None
    derived: dict[str, int | bool] = Field(default_factory=dict)
    blocking_codes: list[str] = Field(default_factory=list)
    unknown_codes: list[str] = Field(default_factory=list)


def _stop(code, *, derived=None, blocking=None, unknown=None):
    return GrantDecision(decision='handoff', reason_code=code, derived=derived or {},
                         blocking_codes=sorted(set(blocking or [])), unknown_codes=sorted(set(unknown or [])))


def _history_reason(history, subscription, tenant_id, customer_id, now):
    if not history.complete:
        return 'GRANT_HISTORY_INCOMPLETE'
    receipts = history.receipts
    action_ids, versions = set(), set()
    for receipt in receipts:
        if (receipt.tenant_id != tenant_id or receipt.customer_id != customer_id
                or receipt.subscription_id != subscription.subscription_id
                or receipt.action_id in action_ids or receipt.after_version in versions
                or receipt.after_version != receipt.before_version + 1
                or receipt.after_version > subscription.business_version
                or receipt.committed_business_at > now
                or receipt.temporary_expires_at != receipt.committed_business_at + timedelta(days=receipt.days)):
            return 'GRANT_HISTORY_INVALID'
        action_ids.add(receipt.action_id)
        versions.add(receipt.after_version)
    if receipts:
        ordered = sorted(receipts, key=lambda row: row.committed_business_at)
        if any(previous.after_version >= following.after_version
               or previous.committed_business_at == following.committed_business_at
               for previous, following in zip(ordered, ordered[1:])):
            return 'GRANT_HISTORY_INVALID'
        latest = max(receipts, key=lambda row: row.committed_business_at)
        if (latest.after_version == subscription.business_version
                and (subscription.temporary_seats != latest.seats
                     or subscription.temporary_expires_at != latest.temporary_expires_at)):
            return 'GRANT_HISTORY_INVALID'
    return None


def _complete_policy_evidence(evidence, policy):
    try:
        bundle = _parsed(EvidenceBundle, evidence)
    except (ValueError, TypeError, OverflowError):
        return False
    if not bundle.ok:
        return False
    for ref, context in zip(bundle.refs, bundle.context):
        if (ref.section_id == policy['section_id'] and ref.content_sha256 == policy['content_sha256']
                and (ref.start_line, ref.end_line) == _SECTION_LINES
                and len(context.text.splitlines()) == _SECTION_LINES[1]
                and hashlib.sha256(context.text.encode('utf-8')).hexdigest() == _PIN_SHA256):
            return True
    return False


def evaluate_grant(*, tenant_id, customer_id, confirmed, subscription, invitation, service_status, evidence,
                   now: datetime) -> GrantDecision:
    """Evaluate only confirmed intent and independently observed business facts.

All malformed/unknown inputs fail closed with fixed codes. No raw exception or
model content is returned. Authorization and persistence remain the caller's
responsibility; neither an EvidenceBundle nor this result is an ACL capability.
"""
    try:
        confirmation = _parsed(GrantConfirmedFields, confirmed)
    except (ValueError, TypeError, OverflowError):
        return _stop('CONFIRMED_FIELDS_INVALID')
    if confirmation.requested_action is None:
        return GrantDecision(decision='not_requested', reason_code=None)
    if (not isinstance(tenant_id, str) or not tenant_id.strip() or '\0' in tenant_id
            or customer_id is not None and (not isinstance(customer_id, str) or not customer_id.strip() or '\0' in customer_id)
            or confirmation.customer_id is not None and confirmation.customer_id != customer_id):
        return _stop('CONFIRMED_FIELDS_INVALID')
    required = [name for name, value in (
        ('customer_id', customer_id), ('invitation_email', confirmation.invitation_email),
        ('seats', confirmation.seats), ('days', confirmation.days)) if value is None]
    if required:
        return GrantDecision(decision='input_required', reason_code='INPUT_REQUIRED', required_fields=required)
    if not isinstance(now, datetime) or now.tzinfo is None or now.utcoffset() is None:
        return _stop('FACTS_INCOMPLETE')
    now = now.astimezone(UTC)
    if subscription is None:
        return _stop('FACTS_INCOMPLETE')
    if isinstance(subscription, BaseModel):
        subscription = subscription.model_dump(mode='json')
    if not isinstance(subscription, dict):
        return _stop('FACTS_INCOMPLETE')
    try:
        pinned = load_policy_bundle()
        supplied = _parsed(GrantPolicyBinding, subscription.get('policy'))
        if supplied.model_dump(mode='json') != pinned:
            return _stop('POLICY_INVALID')
    except (ValueError, TypeError, OSError, OverflowError):
        return _stop('POLICY_INVALID')
    raw_history = subscription.get('grant_history')
    if (not isinstance(raw_history, dict) or raw_history.get('complete') is not True
            or isinstance(raw_history.get('receipts'), list) and len(raw_history['receipts']) > 50):
        return _stop('GRANT_HISTORY_INCOMPLETE')
    try:
        history = _parsed(GrantHistory, raw_history)
    except (ValueError, TypeError, OverflowError):
        return _stop('GRANT_HISTORY_INVALID')
    try:
        sub = _parsed(GrantSubscriptionObservation, subscription)
    except (ValueError, TypeError, OverflowError):
        return _stop('FACTS_INCOMPLETE')
    reason = _history_reason(history, sub, tenant_id, customer_id, now)
    if reason:
        return _stop(reason)
    if sub.plan_status != 'active':
        return _stop('PLAN_NOT_ACTIVE' if sub.plan_status in {'inactive', 'cancelled', 'suspended'} else 'FACTS_INCOMPLETE')
    temporary = sub.temporary_seats if sub.temporary_expires_at is not None and now < sub.temporary_expires_at else 0
    derived = {'effective_temporary': temporary, 'capacity': sub.base_seats + temporary,
               'remaining': max(sub.base_seats + temporary - sub.used_seats, 0),
               'over_capacity': sub.used_seats > sub.base_seats + temporary,
               'projected_remaining': max(sub.base_seats + confirmation.seats - sub.used_seats, 0)}
    if temporary:
        return _stop('TEMPORARY_GRANT_ACTIVE', derived=derived)
    if history.receipts:
        last = max(receipt.committed_business_at for receipt in history.receipts)
        if now < last + timedelta(seconds=pinned['constants']['cooldown_seconds']):
            return _stop('GRANT_COOLDOWN_ACTIVE', derived=derived)
    try:
        invite = _parsed(InvitationObservation, invitation)
        service = _parsed(ServiceStatus, service_status)
    except (ValueError, TypeError, OverflowError):
        return _stop('FACTS_INCOMPLETE', derived=derived)
    if invite.invitation.email != confirmation.invitation_email:
        return _stop('FACTS_INCOMPLETE', derived=derived)
    blocking, unknown = invitation_codes(invite.invitation, now)
    if derived['remaining'] == 0:
        blocking.append('CAPACITY_EXHAUSTED')
    allowed = domain_allowed(invite.invitation.email_domain, invite.policy.allowed_domains, invite.policy.allow_subdomains)
    if allowed is None:
        unknown.append('DOMAIN_POLICY_UNKNOWN')
    elif not allowed:
        blocking.append('DOMAIN_NOT_ALLOWED')
    if invite.policy.invitation_permission is None:
        unknown.append('INVITATION_PERMISSION_UNKNOWN')
    elif not invite.policy.invitation_permission:
        blocking.append('INVITATION_PERMISSION_DENIED')
    if service.status not in {'operational', 'degraded', 'outage'}:
        unknown.append('SERVICE_STATUS_UNKNOWN')
    elif service.status != 'operational':
        blocking.append('SERVICE_UNAVAILABLE')
    if unknown:
        return _stop('FACTS_INCOMPLETE', derived=derived, blocking=blocking, unknown=unknown)
    if set(blocking) - {'CAPACITY_EXHAUSTED'}:
        return _stop('GRANT_BLOCKERS_PRESENT', derived=derived, blocking=blocking)
    if derived['remaining'] > 0:
        return _stop('CAPACITY_NOT_EXHAUSTED', derived=derived)
    if derived['projected_remaining'] < pinned['constants']['minimum_remaining_seats']:
        return _stop('GRANT_CAPACITY_INSUFFICIENT', derived=derived, blocking=blocking)
    if not _complete_policy_evidence(evidence, pinned):
        return _stop('EVIDENCE_REQUIRED', derived=derived, blocking=blocking)
    command = ProposalCommand(tenant_id=tenant_id, customer_id=customer_id, subscription_id=sub.subscription_id,
        seats=confirmation.seats, days=confirmation.days, policy_version=pinned['policy_version'],
        business_version=sub.business_version, created_at=now,
        expires_at=now + timedelta(seconds=pinned['constants']['proposal_ttl_seconds']))
    return GrantDecision(decision='proposal', reason_code='APPROVAL_REQUIRED', command=command,
                         derived=derived, blocking_codes=sorted(set(blocking)))
