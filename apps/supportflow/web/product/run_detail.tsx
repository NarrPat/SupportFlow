import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import type { SessionApi } from "./api";
import type { Actor, RunExecutionConfiguration, RunsIndexPage, Thread } from "./contracts";
import { RunPanel, statusLabels, useRunReader } from "./runs";
import { ReadError } from "./tickets";
import "./workbench.css";

const pageSize = 10;

export function RunIndex({ api, actor }: { api: SessionApi; actor: Actor }) {
  const [offset, setOffset] = useState(0);
  const page = useQuery({
    queryKey: ["supportflow", actor.actor_id, "runs", offset],
    queryFn: ({ signal }) => api.get<RunsIndexPage>(`/runs?limit=${pageSize}&offset=${offset}`, signal),
  });
  const rows = page.data?.items ?? [];
  return <main className="wrap tickets-page">
    <div className="page-heading"><h1>运行</h1></div>
    <section className="ticket-panel" aria-label="运行列表">
      {page.isPending ? <p className="empty" role="status">正在读取运行列表…</p> :
        page.isError ? <ReadError error={page.error} retry={() => void page.refetch()}/> :
        rows.length === 0 ? <div className="empty"><h2>暂无可见运行</h2></div> :
        <div className="workbench-table"><table>
          <thead><tr><th scope="col">工单</th><th scope="col">状态</th><th scope="col">提交时间</th></tr></thead>
          <tbody>{rows.map(row => <tr key={row.run_id}>
            <td><a href={`/runs/${encodeURIComponent(row.run_id)}`}>{row.ticket_title}</a>
              <small className="record-id">{row.run_id}</small></td>
            <td>{statusLabels[row.status]}</td><td>{new Date(row.created_at).toLocaleString("zh-CN")}</td>
          </tr>)}</tbody>
        </table></div>}
      {(offset > 0 || page.data?.has_more) && <div className="pagination">
        <span>第 {Math.floor(offset / pageSize) + 1} 页</span>
        <button type="button" disabled={offset === 0} onClick={() => setOffset(offset - pageSize)}>上一页</button>
        <button type="button" disabled={!page.data?.has_more || page.isError} onClick={() => setOffset(offset + pageSize)}>下一页</button>
      </div>}
    </section>
  </main>;
}

function ExecutionConfiguration({ configuration }: { configuration: RunExecutionConfiguration }) {
  return <section className="ticket-panel workbench-panel" aria-label="执行配置">
    <h2>执行配置</h2>
    <dl className="metadata">
      {configuration.graph_version != null && <div><dt>图版本</dt><dd>{configuration.graph_version}</dd></div>}
      {configuration.model_mode != null && <div><dt>模型模式</dt><dd>{configuration.model_mode === "live" ? "真实模型" : "脚本模型"}</dd></div>}
      {configuration.model_id != null && <div><dt>模型</dt><dd>{configuration.model_id}</dd></div>}
      {configuration.max_output_tokens != null && <div><dt>输出上限</dt><dd>{configuration.max_output_tokens} tokens</dd></div>}
      {configuration.tool_round_limit != null && <div><dt>工具轮次上限</dt><dd>{configuration.tool_round_limit}</dd></div>}
      <div><dt>执行预算</dt><dd>{configuration.budget_ms} ms</dd></div>
    </dl>
  </section>;
}

function RunTicket({ api, actorId, threadId }: { api: SessionApi; actorId: string; threadId: string }) {
  const thread = useQuery({
    queryKey: ["supportflow", actorId, "run-context", threadId],
    queryFn: ({ signal }) => api.get<Thread>(`/threads/${encodeURIComponent(threadId)}`, signal),
  });
  if (thread.isError) return <ReadError error={thread.error} retry={() => void thread.refetch()}/>;
  if (!thread.isFetchedAfterMount || thread.isPending) return <p role="status">正在读取关联工单…</p>;
  if (thread.data?.thread_id !== threadId) return null;
  const ticketId = encodeURIComponent(thread.data.ticket_id);
  return <div className="section-heading">
    <p>关联工单 <code aria-label="关联工单编号">{thread.data.ticket_id}</code></p>
    <a href={`/workspace?ticket=${ticketId}`}>在工作台查看</a>
  </div>;
}

export function RunDetail({ api, actor, runId }: { api: SessionApi; actor: Actor; runId: string }) {
  const reader = useRunReader(api, actor.actor_id, runId);
  const { run, detail, denied } = reader;
  return <main className="wrap tickets-page">
    <div className="page-heading"><h1>运行详情</h1><a href="/runs">返回运行列表</a></div>
    {denied ? <div className="error-box"><p role="alert">当前运行不可见或已移除。</p>
      <button type="button" disabled={detail.isFetching} onClick={reader.refresh}>重新加载</button></div> :
      detail.isError ? <ReadError error={detail.error} retry={reader.refresh}/> :
      run ? <>
        <section className="ticket-panel workbench-panel" aria-label="运行快照">
          <RunTicket key={run.thread_id} api={api} actorId={actor.actor_id} threadId={run.thread_id}/>
          <RunPanel api={api} actorId={actor.actor_id} run={run} view={reader.view}
            canCreate={actor.capabilities.includes("runs:create")} onSnapshot={reader.applyActionSnapshot}/>
        </section>
        {run.execution_configuration && <ExecutionConfiguration configuration={run.execution_configuration}/>}
      </> : <p className="workbench-notice" role="status">正在读取运行详情…</p>}
  </main>;
}
