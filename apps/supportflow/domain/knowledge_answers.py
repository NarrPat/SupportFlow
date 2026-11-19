"""Versioned, read-only answers reconstructed from complete reviewed sections.

The model selects a topic, never answer text or authority. Retrieval and final
publication supply freshly authorized evidence; this module has no I/O.
"""
import hashlib
from typing import Annotated, Literal

from pydantic import Field, model_validator

from .contracts import Customer, EvidenceBundle, EvidenceRef, Intent, ObservationRef, StrictModel
from apps.supportflow.knowledge.reviewed import reviewed_sections


KnowledgeTopic = Literal["seat_capacity", "temporary_seats_policy", "invitation_diagnosis", "unsupported"]
RolesTopic = Literal["seat_capacity", "temporary_seats_policy", "invitation_diagnosis", "invitation_roles", "unsupported"]
SearchTopic = Literal["seat_capacity", "temporary_seats_policy", "invitation_diagnosis", "invitation_roles", "document_search", 'domain_policy', 'invitation_lifecycle', 'service_health_rules', 'service_unknown', 'knowledge_governance', "unsupported"]

def search_enabled(snapshot):
    value=snapshot.get('knowledge_search_contract_version')
    return enabled(snapshot) and type(value) is int and value==1


def enabled(snapshot):
    value = snapshot.get("knowledge_question_contract_version")
    return type(value) is int and value == 1


def roles_enabled(snapshot):
    value = snapshot.get("knowledge_roles_contract_version")
    return enabled(snapshot) and type(value) is int and value == 1


class KnowledgeIntent(Intent):
    intent: Literal["subscription_status", "invitation_diagnosis", "knowledge_question"]
    knowledge_topic: KnowledgeTopic | None

    @model_validator(mode="after")
    def topic_boundary(self):
        if (self.intent == "knowledge_question") != (self.knowledge_topic is not None):
            raise ValueError("Only knowledge questions select a knowledge topic")
        if self.intent == "knowledge_question" and self.invitation_email is not None:
            raise ValueError("Knowledge questions cannot select an invitation")
        return self


class KnowledgeRolesIntent(KnowledgeIntent):
    knowledge_topic: RolesTopic | None

class KnowledgeSearchIntent(KnowledgeRolesIntent):
    knowledge_topic: SearchTopic | None


class KnowledgeAnswerResult(StrictModel):
    schema_version: Literal[5] = 5
    resolution_code: Literal["KNOWLEDGE_ANSWERED", "EVIDENCE_REQUIRED", "FACTS_INCOMPLETE"]
    customer: Customer | None
    intent: Literal["knowledge_question"] = "knowledge_question"
    knowledge_topic: SearchTopic
    answer_mode: Literal["verified_excerpt"] = "verified_excerpt"
    evidence_refs: Annotated[list[EvidenceRef], Field(max_length=5)]
    action_receipts: Annotated[list, Field(max_length=0)]
    observation_refs: Annotated[list[ObservationRef], Field(max_length=1)]
    reply_text: Annotated[str, Field(min_length=1, max_length=4000)]

    @model_validator(mode="after")
    def answer_boundary(self):
        refs = self.evidence_refs
        if len({ref.evidence_id for ref in refs}) != len(refs) or len({ref.generation for ref in refs}) > 1:
            raise ValueError("Expected unique evidence from one generation")
        if self.resolution_code == "KNOWLEDGE_ANSWERED":
            if not refs or self.customer is None or self.knowledge_topic == "unsupported":
                raise ValueError("Answers require a customer, a supported topic and evidence")
        elif refs:
            raise ValueError("Unverified answers cannot publish citations")
        if any(ref.tool_id != "get_customer" for ref in self.observation_refs):
            raise ValueError("Knowledge answers only observe customer identity")
        return self


# Section-relative, one-based line selections. The complete section hash must
# match the trusted registry before any line is quoted. Policy line nine records
# an earlier implementation stage and is deliberately not an answer to policy.
SELECTIONS = {
    "seat_capacity": (("invitation.capacity", "seat_capacity", (2, 3, 4)),
                      ("policy.temporary_seats.v1", "temporary_seats_policy", (3, 4, 10))),
    "temporary_seats_policy": (("policy.temporary_seats.v1", "temporary_seats_policy", (2, 3, 4, 5, 6, 7, 8, 10)),),
    "invitation_diagnosis": (("invitation.overview", "invitation_diagnosis", (2,)),),
    "invitation_roles": (("invitation.roles", "invitation_roles", (2, 3, 4, 5)),),
}
QUERIES = {
    "seat_capacity": "基础席位与临时席位的区别，有效容量与到期，临时席位规则 seats days",
    "temporary_seats_policy": "临时席位规则 申请条件 数量 天数 审批 冷却期 到期",
    "invitation_diagnosis": "邀请失败只读诊断 排障 订阅 容量 域名 权限 有效期 服务状态",
    "unsupported": "未支持的知识主题",
    "invitation_roles": "邀请权限与审批边界 客户 invitation_permission 普通成员 管理员 主管 自批 客户口头同意",
}


# Reviewed general knowledge topics; no question identifiers or expected answers.
SELECTIONS.update({'domain_policy': (('domain-boundary', 'domain_policy', (2,)), ('invitation.other_blockers', 'domain_policy', (2, 3, 4, 5, 6, 7, 8)), ('policy.temporary_seats.v1', 'domain_policy', (2, 3, 4, 5, 6, 7, 8, 10)), ('unknown-fields', 'domain_policy', (2,))), 'invitation_lifecycle': (('invitation.other_blockers', 'invitation_lifecycle', (2, 3, 4, 5, 6, 7, 8)), ('policy.temporary_seats.v1', 'invitation_lifecycle', (2, 3, 4, 5, 6, 7, 8, 10)), ('invitation.capacity', 'invitation_lifecycle', (2, 3, 4)), ('invitation-status', 'invitation_lifecycle', (2,)), ('unknown-fields', 'invitation_lifecycle', (2,))), 'service_health_rules': (('invitation.other_blockers', 'service_health_rules', (2, 3, 4, 5, 6, 7, 8)), ('policy.temporary_seats.v1', 'service_health_rules', (2, 3, 4, 5, 6, 7, 8, 10)), ('service-observation', 'service_health_rules', (2,)), ('invitation.overview', 'service_health_rules', (2, 3, 4))), 'service_unknown': (('unknown-fields', 'service_unknown', (2,)), ('service-observation', 'service_unknown', (2,)), ('manual-handoff', 'service_unknown', (2,))), 'knowledge_governance': (('reference-history', 'knowledge_governance', (2,)), ('knowledge-publish', 'knowledge_governance', (2,)), ('access-revocation', 'knowledge_governance', (2,)))})
QUERIES.update({'domain_policy': '邮箱域名边界 点边界 子域名显式允许 evil-example.com 其他阻断 策略缺失 unknown 临时席位不能更改域名', 'invitation_lifecycle': '邀请有效期 created_at expires_at 到期等号 旧邀请 pending joined revoked 不自动重新邀请 容量 未知时间字段', 'service_health_rules': '邀请服务 degraded outage 订阅active区别 服务状态观察 带时间 historical incident 临时席位不能修复服务', 'service_unknown': '服务未知状态 无法读取状态 服务恢复时间 unknown 缺失事实 人工交接 不猜恢复时间', 'knowledge_governance': '知识版本发布 staged 向量词法索引 失败保留旧版 历史引用定位 当前有效版本 资料撤权 当前访问权限'})

SERVICE_CHECK_GUIDANCE = "排查时分别读取订阅状态和带时间的服务观察：先核对订阅是否为 active，再独立核对当前服务观察。二者不能互为依据，缺失的记录须保留未知。\n"


def build_answer(customer, topic, evidence: EvidenceBundle, observation_refs):
    """Select all required claims or return an evidence-free handoff."""
    refs, excerpts = [], []
    complete = evidence.ok and customer is not None and topic in SELECTIONS
    if topic=='document_search':
        # This is a retrieval result, not a semantic answer: quote actual,
        # currently authorized chunks without interpreting document instructions.
        complete=evidence.ok and customer is not None and bool(evidence.refs)
        if complete:
            if len(evidence.refs)!=len(evidence.context):complete=False
            else:
                for ref,context in list(zip(evidence.refs,evidence.context))[:3]:
                    if context.evidence_id!=ref.evidence_id or hashlib.sha256(context.text.encode()).hexdigest()!=ref.content_sha256:
                        complete=False;break
                    refs.append(ref);excerpts.append(context.text[:900])
    if complete and topic!='document_search':
        by_section = {}
        for ref, context in zip(evidence.refs, evidence.context):
            by_section.setdefault(ref.section_id, []).append((ref, context.text))
        for section, support, lines in SELECTIONS[topic]:
            candidates = by_section.get(section, [])
            if len(candidates) != 1:
                complete = False
                break
            ref, body = candidates[0]
            reviewed = reviewed_sections().get((section, ref.content_sha256), {})
            if (support not in reviewed.get("supported_topics", [])
                    or hashlib.sha256(body.encode("utf-8")).hexdigest() != ref.content_sha256
                    or len(body.splitlines()) < max(lines)):
                complete = False
                break
            refs.append(ref)
            excerpts.append("\n".join(body.splitlines()[line - 1] for line in lines))
    # The quoted source remains verbatim. This fixed capability notice comes
    # from the registered command boundary, not model-generated business claims.
    notice = ""
    if complete and topic in {'domain_policy','invitation_lifecycle','service_health_rules'}:
        notice = "操作边界：当前 Agent 仅支持受控临时席位授予，不能修改域名策略或创建、重发邀请；此类操作请交有权限的人员另行核查处理。\n"
    if complete and topic == 'service_health_rules':
        notice += "外部 Issue 只能作线索，须结合当前客户的实时内部事实，不能单凭条目确认故障根因。\n"
        notice += SERVICE_CHECK_GUIDANCE
    if complete and topic == 'service_unknown':
        notice = "没有可靠观测或排期时，应明确说明未知，不宣称服务正常，也不编造恢复时间。\n"
    if complete and topic == 'knowledge_governance':
        notice = "当前回答仅使用当前租户、当前账号有权访问的有效版本；历史版本只能在当前授权仍有效时核对，不能替代当前规则。\n"
    reason = "KNOWLEDGE_ANSWERED" if complete else "FACTS_INCOMPLETE" if customer is None else "EVIDENCE_REQUIRED"
    prefix="当前账号可见的知识检索片段（每段最多900字符，仅展示原文，不代表已完成业务操作）：\n" if topic=='document_search' else "根据当前可见的知识原文：\n"
    reply = (notice + prefix + "\n\n".join(excerpts) if complete else
             "无法从当前可见的完整知识证据确认此问题，已转人工核查。")
    return KnowledgeAnswerResult(resolution_code=reason, customer=customer, knowledge_topic=topic,
        evidence_refs=refs if complete else [], action_receipts=[], observation_refs=observation_refs, reply_text=reply)


def verify_projection(result, evidence, *, customer_id):
    """Rebuild completed checkpoints instead of trusting saved success/text."""
    value = KnowledgeAnswerResult.model_validate(result)
    if value.customer is not None and value.customer.customer_id != customer_id:
        raise ValueError("Knowledge customer binding changed")
    rebuilt = build_answer(value.customer, value.knowledge_topic, evidence, value.observation_refs)
    canonical = rebuilt.model_dump(mode="json")
    saved = value.model_dump(mode="json")
    if canonical == saved:
        return rebuilt
    if value.knowledge_topic == "service_health_rules" and SERVICE_CHECK_GUIDANCE in canonical["reply_text"]:
        # An in-flight checkpoint from the previous version can still publish
        # its exact, independently reconstructed wording.
        canonical["reply_text"] = canonical["reply_text"].replace(SERVICE_CHECK_GUIDANCE, "", 1)
    if canonical != saved:
        raise ValueError("Knowledge projection is not supported by current evidence")
    return value
