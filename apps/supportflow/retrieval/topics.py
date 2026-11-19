"""Bounded retrieval of complete reviewed topic evidence.

Each search uses the ordinary authorized hybrid retriever and consumes its run
budget. No SQL shortcut, corpus text lookup, evaluation ID or gold is used here.
Missing evidence or a generation change fails closed; final load_refs rechecks
all selected references before publication.
"""
from apps.supportflow.domain.contracts import EvidenceBundle
from apps.supportflow.domain.knowledge_answers import QUERIES,SELECTIONS
from apps.supportflow.knowledge.reviewed import reviewed_sections

TOPICS=frozenset({'domain_policy','invitation_lifecycle','service_health_rules','service_unknown','knowledge_governance'})
MAX_SEARCHES=5  # one customer read plus at most five searches fits the bounded tool budget
FACETS={
 'domain-boundary':'邮箱域名边界 点边界 子域名需要显式配置 evil-example.com',
 'invitation.other_blockers':'邀请其他阻断 active 域名 允许列表 权限 有效区间 created_at expires_at 服务状态',
 'policy.temporary_seats.v1':'临时席位申请规则 v1 grant_temporary_seats seats days 审批 冷却期',
 'unknown-fields':'未知事实处理 缺失字段 unknown 不填零 不默认允许',
 'invitation.capacity':'容量阻断 基础席位 临时席位 有效容量 剩余席位',
 'invitation-status':'邀请状态释义 pending joined revoked 旧状态 当前请求',
 'service-observation':'服务状态观察 带时间 historical incident 已关闭 当前恢复',
 'invitation.overview':'邀请排障概览 诊断 当前客户 订阅 邀请记录 服务状态 实时工具事实',
 'manual-handoff':'人工交接信息 已核实事实 缺失项 引用 未知结果',
 'reference-history':'历史引用定位 旧版本 当前回答 有效知识版本',
 'knowledge-publish':'知识版本发布 staged 向量 词法 索引 原子发布 导入失败 旧版',
 'access-revocation':'资料撤权 历史引用 当前访问权限 正文',
}

def retrieve_topic(topic,search):
    if topic not in TOPICS:
        raise ValueError('Unregistered reviewed topic')
    required=[row[0] for row in SELECTIONS[topic]]
    selected={};generation=None;queried=set();registry=reviewed_sections()
    query=QUERIES[topic]
    for _ in range(MAX_SEARCHES):
        bundle=EvidenceBundle.model_validate(search(query))
        if not bundle.ok:
            return {'evidence_refs':[],'retrieval_error':bundle.error_code}
        if generation is not None and bundle.generation!=generation:
            return {'evidence_refs':[],'retrieval_error':'EVIDENCE_REQUIRED'}
        generation=bundle.generation
        for ref in bundle.refs:
            reviewed=registry.get((ref.section_id,ref.content_sha256),{})
            if ref.section_id in required and topic in reviewed.get('supported_topics',[]):
                selected[ref.section_id]=ref.model_dump(mode='json')
        missing=[section for section in required if section not in selected]
        if not missing:
            return {'evidence_refs':[selected[section] for section in required],'retrieval_error':None}
        remaining=[section for section in missing if section not in queried]
        if not remaining:
            break
        section=remaining[0];queried.add(section);query=FACETS[section]
    return {'evidence_refs':[],'retrieval_error':'EVIDENCE_REQUIRED'}
