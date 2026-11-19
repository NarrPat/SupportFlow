"""An evidenced, no-effect answer when a requested grant is unnecessary."""
from datetime import datetime
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Customer, DiagnosisResult, EvidenceBundle, EvidenceRef, Facts, InvitationObservation, ServiceStatus, Subscription
from .grant_contracts import GrantConfirmedFields
from .grant_policy import _complete_policy_evidence, evaluate_grant, load_policy_bundle
from .rules import invitation_facts, subscription_facts


def enabled(snapshot):
    version = snapshot.get('grant_policy_reply_contract_version')
    return (type(version) is int and version == 1
            and snapshot.get('graph_version') == 'grant-execution-v1')


class GrantPolicyReply(DiagnosisResult):
    schema_version: Literal[7] = 7
    intent: Literal['grant_temporary_seats'] = 'grant_temporary_seats'
    resolution_code: Literal['CAPACITY_NOT_EXHAUSTED'] = 'CAPACITY_NOT_EXHAUSTED'
    requested_seats: Annotated[int, Field(ge=1, le=20)]
    requested_days: Annotated[int, Field(ge=1, le=7)]
    evidence_refs: Annotated[list[EvidenceRef], Field(min_length=1, max_length=1)]

    @model_validator(mode='after')
    def no_effect(self):
        if (self.customer is None or any(v is None for v in (self.facts.subscription, self.facts.invitation, self.facts.service_status))
                or self.blocking_codes or self.unknown_codes or self.action_receipts
                or self.facts.subscription.remaining <= 0):
            raise ValueError('A no-grant answer needs complete, unblocked facts and free seats')
        return self


def build_reply(context, observation_refs, customer):
    """Recompute policy, arithmetic and exact text; no model-supplied decision."""
    decision = evaluate_grant(**context)
    evidence = EvidenceBundle.model_validate_json(json.dumps(context['evidence']))
    pin = load_policy_bundle()
    if (decision.reason_code != 'CAPACITY_NOT_EXHAUSTED' or decision.decision != 'handoff'
            or not _complete_policy_evidence(evidence, pin)):
        raise ValueError('Only a fully evidenced unnecessary grant may be answered')
    fields = GrantConfirmedFields.model_validate_json(json.dumps(context['confirmed']))
    customer = Customer.model_validate(customer)
    if customer.customer_id != context['customer_id']:
        raise ValueError('Customer binding changed')
    # Grant-only metadata is validated above, not silently accepted by the
    # public Subscription DTO (which correctly forbids additional fields).
    sub = {k:v for k,v in context['subscription'].items() if k not in {'policy','grant_history'}}
    sub = Subscription.model_validate_json(json.dumps(sub))
    invite = InvitationObservation.model_validate_json(json.dumps(context['invitation']))
    service = ServiceStatus.model_validate_json(json.dumps(context['service_status']))
    now = context['now']
    facts = Facts(as_of=now, subscription=subscription_facts(sub, now),
        invitation=invitation_facts(invite.invitation, invite.policy, now), service_status=service)
    refs = [r for r in evidence.refs if r.section_id == pin['section_id'] and r.content_sha256 == pin['content_sha256']]
    text = (f'已核实客户 {customer.display_name}（{customer.customer_code}）。'
        f'当前有效容量 {facts.subscription.capacity} 席，已用 {facts.subscription.used_seats} 席，剩余 {facts.subscription.remaining} 席。'
        f'本次申请 {fields.seats} 席、{fields.days} 天；当前仍有空闲席位，不满足临时授额要求的容量不足条件。'
        '依据下方当前有效规则，本次无需扩容，未创建提案、审批或授予席位。')
    return GrantPolicyReply(customer=customer, facts=facts, requested_seats=fields.seats, requested_days=fields.days,
        blocking_codes=[], unknown_codes=[], action_receipts=[], evidence_refs=refs,
        observation_refs=observation_refs, reply_text=text)


def verify_reply(result, context, customer):
    saved = GrantPolicyReply.model_validate_json(json.dumps(result))
    # The evidence describes durable observed facts at as_of; recheck the gate
    # at current time, then reconstruct that original projection exactly.
    if saved.facts.as_of > context['now'] or evaluate_grant(**context).reason_code != 'CAPACITY_NOT_EXHAUSTED':
        raise ValueError('Current policy no longer supports the saved result')
    rebuilt = build_reply({**context, 'now':saved.facts.as_of}, saved.observation_refs, customer)
    if saved.model_dump(mode='json') != rebuilt.model_dump(mode='json'):
        raise ValueError('Saved no-grant result differs from trusted observations')
    return rebuilt
