"""Pure diagnosis rules; these functions neither mutate business data nor propose writes."""
import re


def normalize_domain(value):
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("ASCII domain required")
    value = value.strip().lower()
    if not 1 <= len(value) <= 253 or not all(re.fullmatch(r"[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?", label)
                                           for label in value.split(".")):
        raise ValueError("Invalid domain")
    return value


def normalize_email(value):
    if not isinstance(value, str) or not value.isascii():
        raise ValueError("ASCII email required")
    value = value.strip().lower()
    if len(value) > 254 or value.count("@") != 1:
        raise ValueError("Invalid email")
    local, domain = value.split("@")
    if (not 1 <= len(local) <= 64 or not re.fullmatch(r"[a-z0-9.!#$%&'*+/=?^_`{|}~-]+", local)
            or local.startswith(".") or local.endswith(".") or ".." in local):
        raise ValueError("Invalid email")
    return local + "@" + normalize_domain(domain)


def domain_allowed(domain, allowed_domains, allow_subdomains):
    value = normalize_domain(domain)
    if allowed_domains is None:
        return None
    if not isinstance(allowed_domains, list) or len(allowed_domains) > 50 or type(allow_subdomains) not in (bool, type(None)):
        raise ValueError("Invalid domain policy")
    allowed = [normalize_domain(item) for item in allowed_domains]
    if value in allowed:
        return True
    if not allowed:
        return False
    if allow_subdomains is None:
        return None
    return allow_subdomains and any(value.endswith("." + item) for item in allowed)


def subscription_facts(subscription, now):
    from .contracts import SubscriptionFacts
    temporary = subscription.temporary_seats if (subscription.temporary_expires_at is not None
                                                 and now < subscription.temporary_expires_at) else 0
    capacity = subscription.base_seats + temporary
    return SubscriptionFacts(**subscription.model_dump(), subscription_active=subscription.plan_status == "active",
        effective_temporary=temporary, capacity=capacity, remaining=max(capacity-subscription.used_seats, 0),
        over_capacity=subscription.used_seats > capacity)


def invitation_facts(invitation, policy, now, history=()):
    from .contracts import InvitationFacts
    return InvitationFacts(**invitation.model_dump(), history=list(history), within_validity=invitation.created_at <= now < invitation.expires_at,
        domain_allowed=domain_allowed(invitation.email_domain, policy.allowed_domains, policy.allow_subdomains),
        invitation_permission=policy.invitation_permission)


def invitation_codes(invitation, now):
    blocking, unknown = [], []
    if now < invitation.created_at:
        blocking.append("INVITATION_NOT_YET_VALID")
    if now >= invitation.expires_at:
        blocking.append("INVITATION_EXPIRED")
    if invitation.status == "joined":
        blocking.append("ALREADY_JOINED")
    elif invitation.status == "revoked":
        blocking.append("INVITATION_REVOKED")
    elif invitation.status != "pending":
        unknown.append("INVITATION_STATUS_UNKNOWN")
    return sorted(blocking), sorted(unknown)


def diagnose(intent, facts, errors):
    """Knowledge evidence is deliberately unavailable in this implementation slice."""
    if intent == "subscription_status":
        if facts.subscription is not None:
            return "succeeded", "NO_ACTION_REQUIRED", [], []
        code = "SUBSCRIPTION_NOT_FOUND" if errors.get("get_subscription") == "NOT_FOUND" else "SUBSCRIPTION_UNAVAILABLE"
        return "handoff", "FACTS_INCOMPLETE", [], [code]
    blocking, unknown = [], ["KNOWLEDGE_EVIDENCE_MISSING"]
    subscription = facts.subscription
    if subscription is None:
        unknown.append("SUBSCRIPTION_NOT_FOUND" if errors.get("get_subscription") == "NOT_FOUND" else "SUBSCRIPTION_UNAVAILABLE")
    else:
        if not subscription.subscription_active:
            blocking.append("PLAN_NOT_ACTIVE")
            if subscription.plan_status not in {"inactive", "cancelled", "suspended"}:
                unknown.append("PLAN_STATUS_UNKNOWN")
        if subscription.remaining == 0:
            blocking.append("CAPACITY_EXHAUSTED")
    invitation = facts.invitation
    if invitation is None:
        unknown.append("INVITATION_NOT_FOUND" if errors.get("get_invitation") == "NOT_FOUND" else "INVITATION_UNAVAILABLE")
    else:
        known, missing = invitation_codes(invitation, facts.as_of)
        blocking.extend(known)
        unknown.extend(missing)
        if invitation.domain_allowed is False:
            blocking.append("DOMAIN_NOT_ALLOWED")
        elif invitation.domain_allowed is None:
            unknown.append("DOMAIN_POLICY_UNKNOWN")
        if invitation.invitation_permission is False:
            blocking.append("INVITATION_PERMISSION_DENIED")
        elif invitation.invitation_permission is None:
            unknown.append("INVITATION_PERMISSION_UNKNOWN")
    service = facts.service_status
    if service is None or service.status not in {"operational", "degraded", "outage"}:
        unknown.append("SERVICE_STATUS_UNKNOWN")
    elif service.status in {"degraded", "outage"}:
        blocking.append("SERVICE_UNAVAILABLE")
    return "handoff", "EVIDENCE_REQUIRED", sorted(set(blocking)), sorted(set(unknown))


LABELS = {
    "CAPACITY_EXHAUSTED": "当前没有可用席位", "PLAN_NOT_ACTIVE": "订阅不是有效 active 状态",
    "DOMAIN_NOT_ALLOWED": "邮箱域名未获允许", "INVITATION_EXPIRED": "邀请已到期",
    "INVITATION_NOT_YET_VALID": "邀请尚未进入有效区间", "INVITATION_PERMISSION_DENIED": "邀请权限被禁止",
    "SERVICE_UNAVAILABLE": "邀请服务存在故障", "ALREADY_JOINED": "此邀请记录显示用户已加入",
    "INVITATION_REVOKED": "此邀请已撤销",
}


def reply_text(customer, intent, facts, blocking, unknown, resolution):
    if resolution == "CUSTOMER_REQUIRED":
        return "请先在工单中确认客户，再提交只读诊断。"
    if resolution == "INPUT_REQUIRED":
        return "请补充需要核查的 ASCII 邀请邮箱。本次未创建提案或重新邀请。"
    prefix = f"已核实客户 {customer.display_name}（{customer.customer_code}）。" if customer else "客户事实未能读取。"
    if facts.subscription is not None:
        sub = facts.subscription
        prefix += f"已用 {sub.used_seats} 席，有效容量 {sub.capacity} 席，剩余 {sub.remaining} 席；订阅状态为 {sub.plan_status}。"
    if intent == "subscription_status" and resolution == "NO_ACTION_REQUIRED":
        return prefix + "本次仅查询当前状态，未修改订阅。"
    if blocking:
        prefix += "已确认的阻断事实：" + "；".join(LABELS[code] for code in blocking) + "。"
    if unknown:
        prefix += "仍有缺失或不确定信息（" + "、".join(unknown) + "）。"
    return prefix + "缺少必要的排障知识证据，转人工继续核查；未创建提案、授予席位或重新邀请。"


def evidence_decision(intent, verified, bundle):
    """Reviewed support comes from the trusted retriever, never a model citation claim."""
    if intent == "subscription_status":
        return dict(verified)
    blocking = sorted(set(verified["blocking_codes"]))
    unknown = sorted(set(verified["unknown_codes"]) - {"KNOWLEDGE_EVIDENCE_MISSING"})
    complete = (bundle.ok and bool(bundle.refs) and "invitation_diagnosis" in bundle.supported_topics
                and set(blocking) <= set(bundle.supported_codes))
    if not complete:
        unknown = sorted({*unknown, "KNOWLEDGE_EVIDENCE_MISSING"})
        resolution = "EVIDENCE_REQUIRED"
    else:
        resolution = "FACTS_INCOMPLETE" if unknown else "DIAGNOSIS_COMPLETE"
    return {"resolution_code": resolution, "blocking_codes": blocking, "unknown_codes": unknown}


def rag_reply_text(customer, intent, facts, blocking, unknown, resolution):
    """Public prose contains live facts and fixed decisions, never document excerpts."""
    if resolution in {"CUSTOMER_REQUIRED", "INPUT_REQUIRED"} or intent == "subscription_status":
        return reply_text(customer, intent, facts, blocking, unknown, resolution)
    text = f"已核实客户 {customer.display_name}（{customer.customer_code}）。" if customer else "客户事实未能读取。"
    if facts.subscription is not None:
        sub = facts.subscription
        text += f"已用 {sub.used_seats} 席，有效容量 {sub.capacity} 席，剩余 {sub.remaining} 席；订阅状态为 {sub.plan_status}。"
    if facts.invitation is not None:
        inv = facts.invitation
        text += f"规范邮箱 {inv.email} 的当前邀请状态为 {inv.status}，有效区间为 {inv.created_at.isoformat()} 至 {inv.expires_at.isoformat()}（到期时刻不含），当前{'在' if inv.within_validity else '不在'}有效区间。"
        if inv.status == "pending":
            text += "已有待处理邀请；重复请求不代表新增邀请，本次不会自动重发。"
        for old in inv.history:
            validity = "已过期" if facts.as_of >= old.expires_at else "尚未生效" if facts.as_of < old.created_at else "仍在有效区间"
            text += f"旧记录 {old.invitation_id} 状态 {old.status}，{validity}，与上述当前记录分开判断。"
            if old.status == "revoked":
                text += "旧邀请撤销不是当前重复邀请的证明。"
        if inv.history:
            text += "仅列出最近五条旧记录，不代表全部历史。"
    if facts.service_status is not None:
        text += f"实时邀请服务状态为 {facts.service_status.status}。"
    if blocking:
        text += "已确认的阻断事实：" + "；".join(LABELS[code] for code in blocking) + "。"
    if unknown:
        text += "仍有缺失或不确定信息（" + "、".join(unknown) + "）。"
    if resolution == "DIAGNOSIS_COMPLETE":
        text += "只读诊断已完成，排障依据可通过下方引用查看。"
    elif resolution == "FACTS_INCOMPLETE":
        text += "仍缺必要的实时事实，转人工继续核查。"
    else:
        text += "缺少当前有效且支持本次诊断的知识证据，转人工继续核查。"
    return text + "本次未创建提案、授予席位、重新邀请或修复服务。"


def invalidate_rag_evidence(result):
    """Drop inaccessible metadata before a finalization or authorized HTTP projection."""
    import json
    from .contracts import RagDiagnosisResult
    value = dict(result)
    value.update(resolution_code="EVIDENCE_REQUIRED", evidence_refs=[],
                 unknown_codes=sorted({*value["unknown_codes"], "KNOWLEDGE_EVIDENCE_MISSING"}))
    contract=RagDiagnosisResult
    if value.get('schema_version')==8:
        from .recovered_diagnosis import RecoveredRagDiagnosisResult
        contract=RecoveredRagDiagnosisResult
    parsed = contract.model_validate_json(json.dumps(value))
    parsed.reply_text = rag_reply_text(parsed.customer, parsed.intent, parsed.facts, parsed.blocking_codes,
                                      parsed.unknown_codes, parsed.resolution_code)
    return parsed.model_dump(mode="json")
