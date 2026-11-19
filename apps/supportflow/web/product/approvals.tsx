import { useEffect, useState } from "react";
import { useInfiniteQuery, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Actor, ApprovalDecisionResult, ApprovalView, ApprovalsPage } from "./contracts";
import { describeError, type SessionApi } from "./api";
import { ApprovalClient, validReason, type ApprovalState } from "./approval_client";
import { ProposalView } from "./proposal_view";

const statusLabels: Record<string, string> = { pending: "待决定", approved: "已批准", rejected: "已拒绝", expired: "已到期", cancelled: "已取消" };
const runLabels: Record<string, string> = { awaiting_approval: "等待审批", resume_queued: "等待恢复", queued: "等待执行",
  running: "正在处理", handoff: "转人工", expired: "已到期", cancelled: "已取消", rejected: "已拒绝", failed: "处理失败", succeeded: "处理完成" };
const terminal = new Set(["succeeded", "handoff", "rejected", "expired", "failed", "cancelled"]);

function DecisionForm({ snapshot, api }: { snapshot: ApprovalView; api: SessionApi }) {
  const cache = useQueryClient();
  const [state, setState] = useState<ApprovalState>({ approval: snapshot, phase: "idle", request: null,
    error: null, ambiguous: false, refreshing: false });
  const [reason, setReason] = useState("");
  const [client] = useState(() => new ApprovalClient(snapshot, {
    key: () => crypto.randomUUID(),
    post: (path, body) => api.post<ApprovalDecisionResult>(path, body),
    snapshot: (path, signal) => api.get<ApprovalView>(path, signal),
    received: row => {
      cache.setQueryData(["approval", row.approval_id], row);
      void cache.invalidateQueries({ queryKey: ["approvals"] });
      void cache.invalidateQueries({ queryKey: ["runs"] });
      void cache.invalidateQueries({ queryKey: ["tickets"] });
    },
  }, setState));
  useEffect(() => () => client.dispose(), [client]);
  useEffect(() => { client.acceptSnapshot(snapshot); }, [client, snapshot]);
  const row = state.approval;
  if (state.phase === "denied") return <p className="error" role="alert">{state.error}</p>;
  const blocked = !row.can_decide || row.status !== "pending" ||
    ["submitting", "accepted", "conflict", "closed", "denied"].includes(state.phase);
  return <>
    <p aria-label="审批状态">审批：{statusLabels[row.status] ?? row.status} · 运行：{runLabels[row.run_status] ?? row.run_status}</p>
    {row.decision !== null && <section aria-label="已保存决定">
      <h3>已保存的决定</h3><p>{row.decision === "approve" ? "批准" : "拒绝"} · 版本 {row.version}</p>
      <p className="preserve-space">{row.reason || "未填写意见"}</p>
      <p>决定人：{row.decided_by} · {row.decided_at}</p>
    </section>}
    <p role="note">批准后原任务进入执行前核验，实际授予结果以运行回执为准。</p>
    {row.can_decide && row.status === "pending" && <section aria-label="审批决定">
      <label className="approval-reason">审批意见（可留空，最多1000个字符）
        <textarea aria-label="审批意见" value={reason} disabled={blocked || state.ambiguous}
          onChange={event => setReason(event.target.value)} rows={4}/>
      </label>
      <div className="actions">
        <button type="button" disabled={blocked || !validReason(reason) || (state.ambiguous && state.request?.decision !== "approve")}
          onClick={() => void client.submit("approve", reason)}>批准提案</button>
        <button type="button" disabled={blocked || !validReason(reason) || (state.ambiguous && state.request?.decision !== "reject")}
          onClick={() => void client.submit("reject", reason)}>拒绝提案</button>
      </div>
    </section>}
    {state.error && <p className="error" role="alert">{state.error}</p>}
    {state.phase === "submitting" && <p role="status">正在提交决定…</p>}
    {!row.can_decide && row.status === "pending" && <p>当前提案不可由本账号决定；请核对是否本人申请、已到期或运行版本不支持。</p>}
    <button type="button" disabled={state.refreshing || state.phase === "submitting"} onClick={() => void client.refresh()}>
      {state.refreshing ? "正在读取…" : "重新读取审批"}</button>
    <ProposalView proposal={row.proposal} approvalAvailable/>
  </>;
}

function Detail({ id, api, close }: { id: string; api: SessionApi; close: () => void }) {
  const detail = useQuery({ queryKey: ["approval", id],
    queryFn: ({ signal }) => api.get<ApprovalView>(`/approvals/${encodeURIComponent(id)}`, signal),
    refetchInterval: query => query.state.data?.status === "approved" && !terminal.has(query.state.data.run_status) ? 1000 : false,
  });
  return <section className="approval-detail" aria-label="审批详情">
    <div className="dialog-top"><h2>审批详情</h2><button onClick={close}>返回列表</button></div>
    {detail.isPending ? <p role="status">正在读取审批…</p> : detail.isError ?
      <><p role="alert">{describeError(detail.error)}</p><button onClick={() => void detail.refetch()}>重新加载</button></> : <>
        <p className="mono">审批编号：{detail.data.approval_id}</p>
        <DecisionForm key={id} snapshot={detail.data} api={api}/>
      </>}
  </section>;
}

export function Approvals({ api, actor }: { api: SessionApi; actor: Actor }) {
  const [selected, setSelected] = useState(() => new URL(location.href).searchParams.get("approval"));
  const [filter, setFilter] = useState("pending");
  const list = useInfiniteQuery({ queryKey: ["approvals", actor.actor_id, filter], initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => api.get<ApprovalsPage>(`/approvals?limit=10&offset=${pageParam}${filter ? `&status=${filter}` : ""}`, signal),
    getNextPageParam: last => last.has_more ? last.offset + last.limit : undefined,
    enabled: actor.role === "supervisor" && !selected,
  });
  function select(id: string | null) {
    const url = new URL(location.href); id ? url.searchParams.set("approval", id) : url.searchParams.delete("approval");
    history.replaceState(null, "", url); setSelected(id);
  }
  if (actor.role !== "supervisor") return <main className="wrap"><h1>审批</h1><p role="alert">当前账号没有审批权限。</p></main>;
  return <main className="wrap approvals-page"><h1>审批</h1>
    {selected ? <Detail key={`${actor.actor_id}:${selected}`} id={selected} api={api} close={() => select(null)}/> : <>
      <p>查看当前客户权限范围内的服务端提案；主管不能批准本人发起的申请。</p>
      <label>审批状态 <select aria-label="筛选审批状态" value={filter} onChange={event => setFilter(event.target.value)}>
        <option value="pending">待决定</option><option value="approved">已批准</option><option value="rejected">已拒绝</option>
        <option value="expired">已到期</option><option value="cancelled">已取消</option><option value="">全部</option>
      </select></label>
      {list.isPending ? <p role="status">正在读取审批列表…</p> : list.isError ? <>
        <p role="alert">{describeError(list.error)}</p><button onClick={() => void list.refetch()}>重新加载</button>
      </> : <>
        {list.data.pages.every(page => page.items.length === 0) && <p role="status">当前筛选下没有可见提案。</p>}
        <ul className="approval-list">{list.data.pages.flatMap(page => page.items).map(row => <li key={row.approval_id}>
          <button className="approval-open" onClick={() => select(row.approval_id)}>
            <strong>{row.proposal.customer_id} · 临时席位 {row.proposal.seats} 个／{row.proposal.days} 天</strong>
            <span>{statusLabels[row.status]} · 审批版本 {row.version}</span><span className="mono">{row.approval_id}</span>
          </button>
        </li>)}</ul>
        {list.hasNextPage && <button disabled={list.isFetchingNextPage} onClick={() => void list.fetchNextPage()}>加载更多</button>}
        <button onClick={() => void list.refetch()}>刷新列表</button>
      </>}
    </>}
  </main>;
}
