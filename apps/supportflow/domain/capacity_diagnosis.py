"""Read-only capacity scope. No invitation identity or grant eligibility is inferred."""
import hashlib
import json
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .clock import business_now
from .contracts import Customer, DiagnosisResult, EvidenceBundle, EvidenceRef, Facts, Subscription
from .knowledge_answers import KnowledgeIntent, RolesTopic, SearchTopic, enabled as knowledge_enabled
from .rules import subscription_facts
from apps.supportflow.knowledge.reviewed import reviewed_sections


QUERY = "只确认订阅席位容量：基础席位、有效临时席位、已用、剩余，容量不足邀请排障"


def enabled(snapshot):
    version = snapshot.get("capacity_diagnosis_contract_version")
    return knowledge_enabled(snapshot) and type(version) is int and version == 1


class CapacityIntent(KnowledgeIntent):
    intent: Literal["subscription_status", "invitation_diagnosis", "knowledge_question", "capacity_diagnosis"]

    @model_validator(mode="after")
    def capacity_boundary(self):
        if self.intent == "capacity_diagnosis" and self.invitation_email is not None:
            raise ValueError("Capacity-only diagnosis does not select an invitation")
        return self


class CapacityRolesIntent(CapacityIntent):
    knowledge_topic: RolesTopic | None

class CapacitySearchIntent(CapacityRolesIntent):
    knowledge_topic: SearchTopic | None


class CapacityDiagnosisResult(DiagnosisResult):
    schema_version: Literal[6] = 6
    intent: Literal["capacity_diagnosis"] = "capacity_diagnosis"
    diagnosis_scope: Literal["subscription_capacity_only"] = "subscription_capacity_only"
    resolution_code: Literal["DIAGNOSIS_COMPLETE", "EVIDENCE_REQUIRED", "FACTS_INCOMPLETE"]
    evidence_refs: Annotated[list[EvidenceRef], Field(max_length=1)]

    @model_validator(mode="after")
    def scope_boundary(self):
        if self.facts.invitation is not None or self.facts.service_status is not None:
            raise ValueError("Capacity scope cannot assert invitation or service facts")
        if any(ref.tool_id not in {"get_customer", "get_subscription"} for ref in self.observation_refs):
            raise ValueError("Unexpected capacity observation")
        if self.resolution_code == "DIAGNOSIS_COMPLETE" and (
                self.customer is None or self.facts.subscription is None or not self.evidence_refs or self.unknown_codes):
            raise ValueError("Capacity diagnosis requires both facts and supporting evidence")
        return self


def build_capacity(observations, evidence: EvidenceBundle, observation_refs):
    def observed(tool, model):
        value = observations.get(tool)
        return model.model_validate_json(json.dumps(value.data)) if value is not None and value.ok else None

    customer = observed("get_customer", Customer)
    subscription = observed("get_subscription", Subscription)
    source = observations.get("get_subscription") or observations.get("get_customer")
    if source is None:
        raise ValueError("Capacity facts require an actual observation")
    # Query-time business facts are reproducible from durable envelopes. In an
    # ordinary runtime the envelope's real observation time is used; only the
    # isolated evaluation profile may replace it with its registered clock.
    as_of = business_now(real_now=source.observed_at)
    facts = Facts(as_of=as_of, subscription=subscription_facts(subscription, as_of) if subscription else None,
                  invitation=None, service_status=None)
    unknown, blocking, refs = [], [], []
    if customer is None:
        unknown.append("CUSTOMER_UNAVAILABLE")
    if subscription is None:
        unknown.append("SUBSCRIPTION_UNAVAILABLE")
    else:
        if subscription.plan_status not in {"active", "inactive", "cancelled", "suspended"}:
            unknown.append("PLAN_STATUS_UNKNOWN")
        if facts.subscription.remaining == 0:
            blocking.append("CAPACITY_EXHAUSTED")
    if evidence.ok:
        for ref, context in zip(evidence.refs, evidence.context):
            support = reviewed_sections().get((ref.section_id, ref.content_sha256), {})
            if (ref.section_id == "invitation.capacity" and "CAPACITY_EXHAUSTED" in support.get("supported_codes", [])
                    and hashlib.sha256(context.text.encode("utf-8")).hexdigest() == ref.content_sha256):
                refs.append(ref)
    if len(refs) != 1:
        refs = []
        unknown.append("KNOWLEDGE_EVIDENCE_MISSING")
    reason = "EVIDENCE_REQUIRED" if not refs else "FACTS_INCOMPLETE" if unknown else "DIAGNOSIS_COMPLETE"
    text = "本次仅核查订阅容量。"
    if facts.subscription is not None:
        sub = facts.subscription
        text += f"查询时有效容量 {sub.capacity} 席、已用 {sub.used_seats} 席、剩余 {sub.remaining} 席。"
        if reason == "DIAGNOSIS_COMPLETE":
            text += "容量已满。" if sub.remaining == 0 else "容量尚有余量。"
    if reason != "DIAGNOSIS_COMPLETE":
        text += "必要事实或排障证据不足，转人工核查。"
    text += "未核查具体邀请、域名、邀请权限或服务状态，不能据此确定邀请失败的唯一原因或声称邀请成功。"
    text += "本次未创建提案、审批或授予席位。"
    return CapacityDiagnosisResult(resolution_code=reason, customer=customer, facts=facts,
        blocking_codes=blocking, unknown_codes=sorted(unknown), evidence_refs=refs,
        action_receipts=[], observation_refs=observation_refs, reply_text=text)


def verify_projection(result, observations, evidence, *, customer_id):
    value = CapacityDiagnosisResult.model_validate_json(json.dumps(result))
    if value.customer is not None and value.customer.customer_id != customer_id:
        raise ValueError("Capacity customer binding changed")
    rebuilt = build_capacity(observations, evidence, value.observation_refs)
    if rebuilt.model_dump(mode="json") != value.model_dump(mode="json"):
        raise ValueError("Capacity checkpoint differs from observed facts and current evidence")
    return rebuilt
