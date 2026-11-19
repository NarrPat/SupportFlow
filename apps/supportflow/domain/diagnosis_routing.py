"""Opt-in routing and confirmed email, separate from grant authorization."""
import re

from .rules import normalize_email


def enabled(snapshot):
    version = snapshot.get("diagnosis_routing_contract_version")
    return type(version) is int and version == 1


PROMPT = (
    "First identify the scope of the actual requested answer, separately from background context. "
    "An incidental mention of an invitation failure does not broaden an explicit request limited to checking "
    "whether seats are full or how many seats remain: choose capacity_diagnosis for that limited question, "
    "without asking for an invitation email. This exception does not apply to requests to diagnose the "
    "whole invitation failure, assess permission to invite, or check service health. "
    "Customer-specific routing rules: choose invitation_diagnosis for invitation failures, missing invitation records, "
    "current inviter permissions, and questions asking whether the customer can invite or continue inviting. "
    "In particular, asking if an invitation is possible with temporary seats is invitation eligibility, "
    "NOT capacity_diagnosis: free seats alone cannot establish invitation eligibility. "
    "Asking whether adding seats helps an inactive customer's invitation is also invitation_diagnosis. "
    "Requests to verify current service health, operational status, outages, timeouts or external incident reports "
    "also require invitation_diagnosis and internal service checks. External reports do not establish an outage. "
    "When a customer-bound request compares historical, resolved or closed external incident reports with the current situation, "
    "choose invitation_diagnosis even if phrased as a warning not to assume an ongoing incident. This is a live "
    "customer incident assessment, not a general knowledge question or document search; confirm current facts and keep "
    "external reports as unverified clues. Missing email still requires clarification. "
    "If a customer-specific request states that service is normal but capacity is full and asks to distinguish the causes, choose invitation_diagnosis. Those user-stated conditions are claims to verify with live tools, not established facts. Compare service health and subscription capacity together; do not narrow that contrast to seat numbers alone. "
    "Never route a service-status request to capacity_diagnosis merely because a customer is named. "
    "Missing email does not change this intent; return null and the server will request it. "
    "Use capacity_diagnosis ONLY when the requested answer is limited to capacity/remaining-seat numbers, "
    "without asking whether invitations are possible. Use knowledge_question for general rules. Questions about the sufficiency or limitations of evidence (whether an external issue alone proves a cause, or whether unavailable observations justify claiming normal service) ask for a rule, even when the word customer appears; choose service_health_rules or service_unknown when those topics are available. This does not apply when asked to fetch, merge, filter or assess actual incident records/current service facts for this ticket. Final scope check: an instruction to read only the current customer invitation records, even framed as same-email cross-tenant isolation, is invitation_diagnosis, NOT knowledge_governance or document_search. A statement that a particular external incident is historical or closed with a warning not to claim it still affects the current case is invitation_diagnosis, NOT service_health_rules. Such record-specific statements require live internal checks even without a customer name or an explicit question mark. Only an abstract question about what evidence can prove, without asserting a particular incident status or requesting records, is a service knowledge question. "
)


def confirmed_email(run):
    value = run.get("confirmed_fields", {}).get("invitation_email")
    return normalize_email(value) if value is not None and enabled(run["config_snapshot"]) else None


# These expressions describe a particular invitation and an observed time or
# lifecycle event. General explanations still use the model's selected topic.
_INVITATION_EVENT = re.compile(
    r"邀请(?:链接)?(?:刚好|恰好|已经|已|尚未|还未|仍未)(?:到期|过期|生效)"
    r"|(?:曾|已|之前|此前)撤销(?:了)?(?:旧|之前的|此前的)?邀请"
)
_INVITATION_FACT_GAP = re.compile(
    r"邀请(?:记录)?(?:的)?(?:创建|到期|有效期)?时间(?:字段)?(?:缺失|缺少|不全)"
    r"|(?:允许)?域名(?:配置|策略)(?:缺失|缺少|不全)"
    r"|旧邀请(?:链接|记录)?[^。！？]{0,20}当前(?:有效)?邀请"
)
_POLICY_QUESTION = re.compile(r"一般来说|通常来说|规则是什么|什么是|假设|举例|如果|若遇到")
_CAPACITY_ONLY = re.compile(r"(?:只|仅)(?:想|需|需要|要)?(?:核对|检查|核查|确认|看|查)(?:一下)?(?:当前)?(?:席位|容量|剩余|是否满员)")


def resolve_customer_invitation_intent(run, intent):
    """Keep specific invitation incidents on the customer read-only path."""
    if enabled(run["config_snapshot"]) and run.get("customer_id") and intent["intent"] == "knowledge_question":
        message = run["message"]
        concrete = _INVITATION_EVENT.search(message) or _INVITATION_FACT_GAP.search(message)
        if concrete and not (_POLICY_QUESTION.search(message) or _CAPACITY_ONLY.search(message)):
            return {"intent": "invitation_diagnosis", "knowledge_topic": None,
                    "invitation_email": confirmed_email(run)}
    return intent
