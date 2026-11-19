import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "antd";
import { useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { DiagnosisResult, RagDiagnosisResult, RecoveredRagDiagnosisResult, CapacityDiagnosisResult, GrantPolicyReply, EvidenceRef, ExternalIssuesView, RunDetail, RunInput, RunAccepted, RunsPage } from "./contracts";
import { ApiError, describeError, SessionApi } from "./api";
import { RunEventClient, type EventHistory, type EventView } from "./event_client";
import type { PublicRunEvent } from "./event_projection";
import { CitationClient, type CitationState } from "./citation_client";
import { ClarificationForm } from "./clarification_form";
import { ProposalView } from "./proposal_view";
import { GrantEffects } from "./grant_effects";
import { CancelRun } from "./cancel_run";
import { emptyGrant, requestFor, runFields, type GrantDraft } from "./run_request";

export const statusLabels: Record<RunDetail["status"], string> = {
  queued: "等待任务调度", running: "正在处理", awaiting_input: "等待补充信息",
  awaiting_approval: "等待审批", resume_queued: "等待继续处理", succeeded: "已完成",
  handoff: "已转人工", rejected: "已拒绝", expired: "已过期", failed: "处理失败",
  cancelled: "已取消", needs_reconciliation: "等待结果核对",
};
const openStatuses = new Set(["queued", "running", "awaiting_input", "awaiting_approval", "resume_queued", "needs_reconciliation"]);
const pageSize = 10;
const reasonLabels: Record<string, string> = {
  CUSTOMER_REQUIRED: "请先确认客户身份，再提交诊断请求。",
  DIAGNOSTIC_TOOLS_PENDING: "客户信息已核实，邀请及订阅状态仍需人工核查。",
  EVIDENCE_REQUIRED: "当前仅完成事实核查，缺少排障知识证据，需要人工继续确认。",
  AUTHORIZATION_REVOKED: "当前客户访问权限已变更，处理已停止。",
  APPROVAL_AUTHORIZATION_REVOKED: "审批人的当前权限已变更，本次申请未执行，请重新核对。",
  EXECUTION_NOT_IMPLEMENTED: "提案已批准并完成授权复核，本次未授予席位，请转人工继续处理。",
  BUSINESS_VERSION_CHANGED: "订阅已发生变化，原提案未执行，请重新发起申请和审批。",
  POLICY_INVALID: "当前规则已变更或无法核实，原提案未执行，请重新核对。",
  PROPOSAL_EXPIRED: "提案已失效，本次未执行，请重新发起申请和审批。",
  BUDGET_EXHAUSTED: "本次处理已达到时间限制，请转人工继续核查。",
  MODEL_ATTEMPTS_EXHAUSTED: "本次处理已达到模型调用上限，请转人工继续核查。",
  TOOL_ROUNDS_EXHAUSTED: "本次处理已达到工具调用上限，请转人工继续核查。",
  RECOVERY_EXHAUSTED: "自动恢复未能完成处理，请转人工继续核查。",
  GRAPH_VERSION_MISMATCH: "此请求使用的处理流程已变更，请转人工继续核查。",
  MODEL_AUTH_ERROR: "模型服务暂时不可用，请联系管理员。",
  MODEL_QUOTA_ERROR: "模型服务暂时不可用，请联系管理员。",
  MODEL_RATE_LIMIT: "模型服务繁忙，本次请求已转人工。",
  MODEL_TIMEOUT: "模型服务响应超时，本次请求已转人工。",
  MODEL_NETWORK_ERROR: "暂时无法连接模型服务，本次请求已转人工。",
  MODEL_UPSTREAM_ERROR: "模型服务发生异常，本次请求已转人工。",
  MODEL_PROTOCOL_ERROR: "模型返回内容未通过核验，本次请求已转人工。",
  MODEL_INPUT_BUDGET: "请求内容超过模型处理限制，本次请求已转人工。",
};

const stageLabels: Record<string, string> = { extract: "识别核查请求", observe: "读取授权范围内的事实",
  extract_intent: "识别核查请求", read_facts: "读取授权范围内的事实", retrieve_evidence: "检索排障知识证据",
  verify_diagnosis: "核验事实与证据", summarize: "核验核查结论", finished: "事实核查结束" };
const toolLabels: Record<string, string> = { get_customer: "客户信息", get_subscription: "订阅状态",
  get_invitation: "邀请记录", get_service_status: "服务状态", search_knowledge: "排障知识证据",
  list_external_issues: "GitHub 外部线索" };
function eventLabel(event: PublicRunEvent): string {
  const payload = event.payload;
  if (event.type === "stage.changed") return stageLabels[String(payload.stage)] ?? "处理阶段已变更";
  if (event.type === "tool.started") return `开始读取${toolLabels[String(payload.tool_id)] ?? "只读事实"}`;
  if (event.type === "tool.finished") return `${toolLabels[String(payload.tool_id)] ?? "只读事实"}${payload.ok === true ? "读取完成" : "读取未完成"}`;
  if (event.type === "message.delta") return "回复内容更新";
  if (event.type === "message.final") return "回复内容已保存";
  if (event.type === "clarification.required") return "等待补充必要信息";
  if (event.type === "approval.required") return "提案已保存，等待审批";
  if (event.type === "approval.decided") return payload.decision === "approve" ? "主管已批准，等待执行前复核" : "主管已拒绝提案";
  if (event.type === "action.completed") return "已保存授予回执";
  if (typeof payload.status === "string" && payload.status in statusLabels) return statusLabels[payload.status as RunDetail["status"]];
  return "运行记录已更新";
}

function RunTimeline({ view }: { view: EventView }) {
  return <section aria-label="运行时间线">
    <h4>运行时间线</h4>
    {view.historyLoading && <p role="status">正在读取历史事件…</p>}
    {view.historyError && <p role="note">{view.historyError}</p>}
    <ol aria-label="运行事件" data-run-id={view.projection.snapshot.run_id}>
      {view.projection.events.map(event => <li key={event.seq} data-seq={event.seq} data-event-type={event.type}
        data-run-id={event.run_id}>
        <span>{eventLabel(event)}</span> <time dateTime={event.occurred_at}>{new Date(event.occurred_at).toLocaleTimeString("zh-CN")}</time>
      </li>)}
    </ol>
  </section>;
}

const findingLabels: Record<string, string> = {
  CAPACITY_EXHAUSTED: "当前没有剩余席位", PLAN_NOT_ACTIVE: "订阅当前未生效", DOMAIN_NOT_ALLOWED: "邮箱域名不在允许范围",
  INVITATION_EXPIRED: "邀请已过期", INVITATION_NOT_YET_VALID: "邀请尚未生效", INVITATION_PERMISSION_DENIED: "当前不具备邀请权限",
  SERVICE_UNAVAILABLE: "邀请服务当前不可用", ALREADY_JOINED: "该成员已加入", INVITATION_REVOKED: "原邀请已撤销",
  KNOWLEDGE_EVIDENCE_MISSING: "缺少当前有效的排障知识证据", DOMAIN_POLICY_UNKNOWN: "邮箱域名规则尚未确认",
  INVITATION_PERMISSION_UNKNOWN: "邀请权限尚未确认", INVITATION_NOT_FOUND: "未找到对应邀请记录",
  INVITATION_UNAVAILABLE: "邀请记录暂时无法读取", INVITATION_STATUS_UNKNOWN: "邀请状态无法确认",
  SUBSCRIPTION_NOT_FOUND: "未找到订阅记录", SUBSCRIPTION_UNAVAILABLE: "订阅记录暂时无法读取",
  SERVICE_STATUS_UNKNOWN: "邀请服务状态尚未确认", PLAN_STATUS_UNKNOWN: "订阅状态无法确认", CUSTOMER_UNAVAILABLE: "客户信息暂时无法读取",
};
const factLabels: Record<string, string> = { active: "已生效", inactive: "未生效", expired: "已过期",
  pending: "待接受", joined: "已加入", revoked: "已撤销", operational: "正常", degraded: "服务降级", outage: "服务中断" };
const confirmed = (value: boolean | null) => value === true ? "是" : value === false ? "否" : "尚未确认";
function DiagnosisFacts({ result }: { result: DiagnosisResult | RagDiagnosisResult | RecoveredRagDiagnosisResult | CapacityDiagnosisResult | GrantPolicyReply }) {
  const { subscription, invitation, service_status: service } = result.facts;
  return <section aria-label="只读事实">
    <h4>只读事实</h4>
    <p>核查基准时间：<time dateTime={result.facts.as_of}>{new Date(result.facts.as_of).toLocaleString("zh-CN")}</time></p>
    {subscription ? <dl className="metadata">
      <div><dt>订阅状态</dt><dd>{factLabels[subscription.plan_status] ?? "尚未确认"}</dd></div>
      <div><dt>基础席位</dt><dd>{subscription.base_seats}</dd></div>
      <div><dt>当前有效临时席位</dt><dd>{subscription.effective_temporary}</dd></div>
      <div><dt>已用席位</dt><dd aria-label="已用席位">{subscription.used_seats}</dd></div>
      <div><dt>有效席位总量</dt><dd aria-label="有效席位总量">{subscription.capacity}</dd></div>
      <div><dt>剩余席位</dt><dd aria-label="剩余席位">{subscription.remaining}</dd></div>
      <div><dt>是否超额</dt><dd>{confirmed(subscription.over_capacity)}</dd></div>
    </dl> : <p>本次未取得订阅事实。</p>}
    {result.intent === "invitation_diagnosis" && (invitation ? <dl className="metadata">
      <div><dt>邀请邮箱</dt><dd aria-label="邀请邮箱">{invitation.email}</dd></div>
      <div><dt>邀请状态</dt><dd>{factLabels[invitation.status] ?? "尚未确认"}</dd></div>
      <div><dt>邀请到期时间</dt><dd>{new Date(invitation.expires_at).toLocaleString("zh-CN")}</dd></div>
      <div><dt>仍在有效期内</dt><dd>{confirmed(invitation.within_validity)}</dd></div>
      <div><dt>域名获准</dt><dd>{confirmed(invitation.domain_allowed)}</dd></div>
      <div><dt>允许邀请</dt><dd>{confirmed(invitation.invitation_permission)}</dd></div>
    </dl> : <p>本次未取得对应邀请记录。</p>)}
    {service && <p>邀请服务：{factLabels[service.status] ?? "尚未确认"}</p>}
    <h4>确认阻断因素</h4>
    {result.blocking_codes.length === 0 && <p>本次核查未确认阻断因素。</p>}
    <ul aria-label="确认阻断因素">{result.blocking_codes.map(code => <li key={code} data-code={code}>{findingLabels[code] ?? "存在待核查因素"}</li>)}</ul>
    <h4>尚未确认的事项</h4>
    {result.unknown_codes.length === 0 && <p>本次只读查询没有待确认事项。</p>}
    <ul aria-label="尚未确认的事项">{result.unknown_codes.map(code => <li key={code} data-code={code}>{findingLabels[code] ?? "有信息尚未确认"}</li>)}</ul>
    {result.resolution_code === "EVIDENCE_REQUIRED" && <p className="run-reason" role="note">当前缺少排障知识证据，需要人工继续核查。</p>}
    {result.resolution_code === "FACTS_INCOMPLETE" && <p className="run-reason" role="note">仍缺少必要的实时事实，需要人工继续核查。</p>}
    {result.resolution_code === "DIAGNOSIS_COMPLETE" && <p role="note">只读诊断已完成；本次没有扩容、发送邀请或修复服务。</p>}
    <p>本次仅核查事实。</p>
  </section>;
}

function ExternalClues({ observation }: { observation: ExternalIssuesView }) {
  return <section aria-label="GitHub 外部线索">
    <h4>GitHub 外部线索</h4>
    <p>{observation.repository} · <time dateTime={observation.observed_at}>
      {new Date(observation.observed_at).toLocaleString("zh-CN")}</time></p>
    <p role="note">外部报告仅供排查。当前服务状态以内部核查事实为准。</p>
    {!observation.ok && <p role="status">暂时无法读取 GitHub 线索。</p>}
    {observation.ok && observation.issues.length === 0 && <p>本次读取没有发现 Issue。</p>}
    {observation.issues.length > 0 && <ul>
      {observation.issues.map(issue => <li key={issue.number}>
        <a href={issue.url} target="_blank" rel="noreferrer">#{issue.number} {issue.title}</a>
        {" · "}{issue.state === "closed" ? "已关闭" : "开放"}
        {issue.closed_at && <> · <time dateTime={issue.closed_at}>
          {new Date(issue.closed_at).toLocaleString("zh-CN")}</time></>}
      </li>)}
    </ul>}
    {observation.has_more && <p>当前展示有界读取范围内的线索。</p>}
  </section>;
}

function KnowledgeCitations({ api, actorId, runId, refs }: {
  api: SessionApi; actorId: string; runId: string; refs: EvidenceRef[];
}) {
  const [state, setState] = useState<CitationState>({ ref: null, loading: false, value: null, error: null });
  const client = useRef<CitationClient | null>(null);
  useEffect(() => {
    const current = new CitationClient((path, signal) => api.get(path, signal), setState);
    client.current = current;
    return () => { current.close(); if (client.current === current) client.current = null; };
  }, [api, actorId, runId]);
  return <section aria-label="知识引用">
    <h4>知识引用</h4>
    {refs.length === 0 ? <p>本次没有可展示的知识引用。</p> : <ul>
      {refs.map(ref => <li key={ref.evidence_id} data-evidence-id={ref.evidence_id}>
        <button type="button" onClick={() => { void client.current?.open(ref); }}
          aria-label={`查看引用 ${ref.section_id}`}>{ref.title} · 第 {ref.start_line}–{ref.end_line} 行</button>
      </li>)}
    </ul>}
    {state.ref && <section aria-label="引用详情">
      <div className="section-heading"><h4>{state.ref.title}</h4>
        <button type="button" onClick={() => client.current?.close()}>关闭引用</button></div>
      {state.loading && <p role="status">正在核验权限并读取引用…</p>}
      {state.error && <p className="error" role="alert">{state.error}</p>}
      {state.value && <>
        <p>章节：{state.value.section_id}；文档版本：<span className="mono">{state.value.version_id}</span>；索引代：{state.value.generation}</p>
        <p>以下是本次引用对应的历史版本正文；重新检索时会检查当前有效版本。</p>
        <ol start={state.value.start_line} aria-label="引用正文" className="citation-lines">
          {state.value.content.split("\n").map((line, index) => <li key={index}><span style={{ whiteSpace: "pre-wrap" }}>{line}</span></li>)}
        </ol>
      </>}
    </section>}
  </section>;
}

function submitError(error: unknown) {
  if (!(error instanceof ApiError)) return "尚未收到提交确认。保留当前内容后重试，可取回原请求。";
  if (error.code === "IDEMPOTENCY_CONFLICT") return "此请求已关联其他内容，请刷新并检查已受理的请求。";
  if (error.status === 409) return "当前会话已有未结束的请求，请先查看运行状态。";
  if (error.status === 429) return "等待队列已满，请稍后重试当前请求。";
  if (error.status === 503) return "服务暂时无法确认受理结果，请稍后重试当前请求。";
  return describeError(error);
}

const runKey = (actorId: string, runId?: string) => ["supportflow", actorId, "run", runId];
const runListKey = (actorId: string, threadId: string) => ["supportflow", actorId, "runs", threadId];
const accessDenied = (error: unknown) => error instanceof ApiError && [401, 403, 404].includes(error.status);

export function useRunReader(api: SessionApi, actorId: string, runId?: string, scopeError?: unknown) {
  const cache = useQueryClient();
  const [eventView, setEventView] = useState<EventView | null>(null);
  const [streamRevision, setStreamRevision] = useState(0);
  const subscription = useRef<RunEventClient | null>(null);
  const detail = useQuery({
    queryKey: runKey(actorId, runId), enabled: Boolean(runId),
    queryFn: ({ signal }) => api.get<RunDetail>(`/runs/${encodeURIComponent(runId!)}`, signal),
  });
  const deniedSnapshot = accessDenied(detail.error) || accessDenied(scopeError);
  const snapshot = detail.isFetchedAfterMount && detail.data?.run_id === runId ? detail.data : undefined;
  const ready = Boolean(snapshot && !deniedSnapshot);
  useEffect(() => {
    setEventView(null);
    if (!ready || !snapshot) return;
    const path = `/runs/${encodeURIComponent(snapshot.run_id)}`;
    const client = new RunEventClient(snapshot, {
      snapshot: signal => api.get<RunDetail>(path, signal),
      history: (after, signal) => api.get<EventHistory>(`${path}/events/history?after=${after}&limit=100`, signal),
      open: url => new EventSource(url),
      schedule: callback => {
        const timer = window.setTimeout(callback, 1000);
        return () => window.clearTimeout(timer);
      },
    }, view => {
      setEventView(view);
      const observed = view.projection.snapshot;
      if (view.denied || openStatuses.has(observed.status)) return;
      // An observed terminal snapshot can update the thread's cached rows
      // without restarting the event subscription or refetching the list.
      cache.setQueriesData<RunsPage>({ queryKey: runListKey(actorId, observed.thread_id) }, previous =>
        previous && { ...previous, items: previous.items.map(item =>
          item.run_id === observed.run_id && item.version <= observed.version && item.last_event_seq <= observed.last_event_seq
            ? observed : item) });
    });
    subscription.current = client;
    client.start();
    return () => {
      client.dispose();
      if (subscription.current === client) subscription.current = null;
    };
    // New snapshots enter the current reader below instead of restarting it.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, actorId, cache, runId, ready, streamRevision]);
  useEffect(() => {
    if (snapshot) subscription.current?.acceptSnapshot(snapshot);
  }, [snapshot]);

  const view = eventView?.projection.snapshot.run_id === runId ? eventView : null;
  const denied = deniedSnapshot || Boolean(view?.denied);
  const run = ready && !denied ? view?.projection.snapshot ?? snapshot : undefined;

  function applyActionSnapshot(updated: RunDetail) {
    if (updated.run_id !== runId) return;
    cache.setQueryData<RunDetail>(runKey(actorId, runId), previous => {
      if (previous && (updated.version < previous.version || updated.last_event_seq < previous.last_event_seq)) {
        return previous;
      }
      return { ...updated, execution_configuration: updated.execution_configuration ?? previous?.execution_configuration };
    });
    void cache.invalidateQueries({ queryKey: runListKey(actorId, updated.thread_id) });
  }

  function refresh() {
    void detail.refetch().then(result => {
      if (view?.denied && result.isSuccess) setStreamRevision(previous => previous + 1);
    });
  }

  return { detail, view, denied, run, applyActionSnapshot, refresh };
}

export function RunPanel({ api, actorId, run, view, canCreate, onSnapshot, linkToDetail = false }: {
  api: SessionApi; actorId: string; run: RunDetail; view: EventView | null; canCreate: boolean;
  onSnapshot(snapshot: RunDetail): void; linkToDetail?: boolean;
}) {
  const reply = run.result?.reply_text;
  const savedMessage = view?.projection.messages.find(item => item.final && item.message_id === `${run.run_id}:reply` && item.text === reply);
  return <article className="run-detail" aria-label="当前运行" data-run-id={run.run_id}>
    <p className="run-status" role="status">{statusLabels[run.status]}</p>
    {view && <p role="status" aria-label="事件连接状态">
      {view.connection === "live" ? "正在接收运行进展" :
        view.connection === "unsupported" ? "事件版本暂不支持，已读取服务器快照。请更新页面后继续查看。" :
        view.connection === "error" ? "运行连接暂时中断，正在重试读取。后台任务继续处理。" :
        view.connection === "closed" ? "运行连接已关闭。" : "正在连接运行进展…"}
    </p>}
    <dl className="metadata">
      <div><dt>运行编号</dt><dd className="mono" aria-label="运行编号">{run.run_id}</dd></div>
      <div><dt>提交时间</dt><dd>{new Date(run.created_at).toLocaleString("zh-CN")}</dd></div>
    </dl>
    {linkToDetail && <a className="run-detail-link" href={`/runs/${encodeURIComponent(run.run_id)}`}>查看运行详情</a>}
    <p className="run-message">{run.message}</p>
    <CancelRun key={`${actorId}:${run.run_id}`} api={api} run={run} canCreate={canCreate} onSnapshot={onSnapshot}/>
    {run.status === "awaiting_input" && run.pending_input && <ClarificationForm
      key={`${actorId}:${run.run_id}:${run.pending_input.input_id}:${run.pending_input.version}`}
      api={api} actorId={actorId} run={run} canSubmit={canCreate} onSnapshot={onSnapshot}/>}
    {run.proposal && <ProposalView proposal={run.proposal}>
      <KnowledgeCitations key={`proposal:${actorId}:${run.run_id}:${run.proposal.proposal_id}`}
        api={api} actorId={actorId} runId={run.run_id} refs={run.proposal.evidence_refs}/>
    </ProposalView>}
    {view?.projection.messages.filter(item => item.message_id !== savedMessage?.message_id).map(item =>
      <p key={item.message_id} data-message-id={item.message_id} data-final={String(item.final)} aria-label="回复消息">{item.text}</p>)}
    {run.result ? <section className="run-result" aria-label="核查结果">
      <h4>核查结果</h4>
      <p aria-label="处理说明" data-message-id={savedMessage?.message_id} data-final="true">{reply}</p>
      {run.result.customer && <dl className="metadata">
        <div><dt>客户编号</dt><dd aria-label="核实客户编号">{run.result.customer.customer_code}</dd></div>
        <div><dt>客户名称</dt><dd aria-label="核实客户名称">{run.result.customer.display_name}</dd></div>
      </dl>}
      {"facts" in run.result && <DiagnosisFacts result={run.result}/>}
      {run.result.schema_version === 5 && <p className="muted">{run.result.knowledge_topic === 'document_search' ? '知识检索 · 当前账号可见片段' : '知识问答 · 当前可见原文摘录'}</p>}
      {run.result.schema_version === 6 && <p className="muted">仅订阅容量诊断 · 其他邀请原因未核查</p>}
      {run.result.schema_version === 7 && <p className="muted">当前已有空闲席位 · 本次无需临时扩容</p>}
      {run.external_issues && <ExternalClues observation={run.external_issues}/>}
      {run.result.schema_version === 4 && <GrantEffects result={run.result}/>}
      {(run.result.schema_version === 3 || run.result.schema_version === 4 || run.result.schema_version === 5 || run.result.schema_version === 6 || run.result.schema_version === 7 || run.result.schema_version === 8 || run.result.schema_version === 9) && <KnowledgeCitations key={`${actorId}:${run.run_id}`}
        api={api} actorId={actorId} runId={run.run_id} refs={run.result.evidence_refs}/>}
    </section> : run.reason_code && reasonLabels[run.reason_code] &&
      <p className="run-reason" role="note">{reasonLabels[run.reason_code]}</p>}
    {view && <RunTimeline view={view}/>}
  </article>;
}

export function Runs({ api, actorId, threadId, canCreate }: {
  api: SessionApi; actorId: string; threadId: string; canCreate: boolean;
}) {
  const cache = useQueryClient();
  const [message, setMessage] = useState("");
  const [grant, setGrant] = useState<GrantDraft>(emptyGrant);
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState<string | null>(null);
  const pendingRequest = useRef<RunInput | null>(null);
  const listKey = runListKey(actorId, threadId);
  const latest = useQuery({
    queryKey: [...listKey, 0],
    queryFn: ({ signal }) => api.get<RunsPage>(
      `/threads/${encodeURIComponent(threadId)}/runs?limit=${pageSize}&offset=0`, signal),
  });
  const older = useQuery({
    queryKey: [...listKey, offset], enabled: offset > 0,
    queryFn: ({ signal }) => api.get<RunsPage>(
      `/threads/${encodeURIComponent(threadId)}/runs?limit=${pageSize}&offset=${offset}`, signal),
  });
  const runs = offset === 0 ? latest : older;
  const runId = selected ?? runs.data?.items[0]?.run_id;
  const reader = useRunReader(api, actorId, runId, runs.error);
  const { detail, run: visibleRun } = reader;
  const submit = useMutation({
    mutationFn: (body: RunInput) => api.post<RunAccepted>(`/threads/${encodeURIComponent(threadId)}/runs`, body),
    onSuccess: async result => {
      setSelected(result.run_id);
      setOffset(0);
      await cache.invalidateQueries({ queryKey: listKey });
    },
    onError: error => {
      if (error instanceof ApiError && error.status === 409) {
        void cache.invalidateQueries({ queryKey: listKey });
      }
    },
  });
  const newestKnown = (run: RunDetail) => visibleRun?.run_id === run.run_id && visibleRun.version >= run.version ? visibleRun : run;
  const activeRun = latest.data?.items.some(run => openStatuses.has(newestKnown(run).status));
  const chars = Array.from(message).length;
  const parsedRequest = runFields(message, grant);
  const submitLabel = submit.isError ? "重试提交" : grant.requested ? "提交临时席位申请" : "提交诊断请求";
  function send(event: FormEvent) {
    event.preventDefault();
    if (submit.isPending || activeRun || !parsedRequest.ok) return;
    // Keep an ambiguous submission's exact body and key until its content changes.
    const body = requestFor(parsedRequest.fields, pendingRequest.current, () => crypto.randomUUID());
    pendingRequest.current = body;
    submit.mutate(body);
  }
  function edit(value: string) {
    setMessage(value);
    pendingRequest.current = null;
    submit.reset();
  }
  function editGrant(value: Partial<GrantDraft>) {
    setGrant(previous => ({ ...previous, ...value }));
    pendingRequest.current = null;
    submit.reset();
  }
  function refresh() {
    void cache.invalidateQueries({ queryKey: listKey });
    if (runId) reader.refresh();
  }
  return <section className="runs" aria-label="诊断请求">
    <div className="section-heading"><h3>诊断请求</h3>
      <button type="button" onClick={refresh} disabled={runs.isFetching || detail.isFetching}>刷新状态</button></div>
    {canCreate && !activeRun && <form className="run-form" onSubmit={send}>
      <label>诊断请求内容<textarea value={message} rows={4} disabled={submit.isPending}
        onChange={event => edit(event.target.value)} placeholder="描述客户遇到的问题与需要核查的内容" /></label>
      {chars > 8000 && <p className="error" role="alert">请求内容最多 8000 个字符。</p>}
      <label className="grant-request-toggle"><input type="checkbox" checked={grant.requested} disabled={submit.isPending}
        onChange={event => editGrant({ requested: event.target.checked })} />明确申请临时席位</label>
      {grant.requested && <fieldset disabled={submit.isPending}>
        <legend>临时席位申请</legend>
        <p>仅提交申请。服务端核验规则和实时事实后可能生成待审批提案；本页不会批准或授予席位。</p>
        <label>申请邀请邮箱<input type="email" maxLength={254} value={grant.invitation_email}
          onChange={event => editGrant({ invitation_email: event.target.value })} placeholder="未知可留空后补充" /></label>
        <label>申请临时席位数（1–20）<input type="text" inputMode="numeric" maxLength={2} value={grant.seats}
          onChange={event => editGrant({ seats: event.target.value })} placeholder="未知可留空" /></label>
        <label>申请天数（1–7）<input type="text" inputMode="numeric" maxLength={1} value={grant.days}
          onChange={event => editGrant({ days: event.target.value })} placeholder="未知可留空" /></label>
        {!parsedRequest.ok && message.trim() && <p className="error" role="alert">{parsedRequest.message}</p>}
      </fieldset>}
      <Button type="primary" htmlType="submit" loading={submit.isPending}
        aria-label={submitLabel} aria-busy={submit.isPending}
        disabled={!parsedRequest.ok || latest.isPending || latest.isError}>
        {submitLabel}</Button>
    </form>}
    {submit.isError && !activeRun && <p className="error" role="alert">{submitError(submit.error)}</p>}
    {runs.isPending && <p role="status">正在读取运行记录…</p>}
    {runs.isError && <p className="error" role="alert">{describeError(runs.error)}</p>}
    {runs.data?.items.length === 0 && <p className="run-empty">此会话尚未提交诊断请求。</p>}
    {runs.data && runs.data.items.length > 0 && <>
      <label className="run-picker">运行记录<select value={runId} onChange={event => setSelected(event.target.value)}>
        {runs.data.items.map(run => <option key={run.run_id} value={run.run_id}>
          {new Date(run.created_at).toLocaleString("zh-CN")} · {statusLabels[newestKnown(run).status]}
        </option>)}
      </select></label>
      {detail.isPending && <p role="status">正在读取请求详情…</p>}
      {detail.isError && <p className="error" role="alert">{describeError(detail.error)}</p>}
      {reader.view?.denied && <p className="error" role="alert">当前运行已不可访问，请重新确认登录和客户权限。</p>}
      {visibleRun && <RunPanel api={api} actorId={actorId} run={visibleRun} view={reader.view}
        canCreate={canCreate} onSnapshot={reader.applyActionSnapshot} linkToDetail/>}
      {(offset > 0 || runs.data.has_more) && <div className="run-pagination">
        <button type="button" disabled={offset === 0} onClick={() => { setOffset(offset - pageSize); setSelected(null); }}>较新记录</button>
        <button type="button" disabled={!runs.data.has_more} onClick={() => { setOffset(offset + pageSize); setSelected(null); }}>较早记录</button>
      </div>}
    </>}
  </section>;
}
