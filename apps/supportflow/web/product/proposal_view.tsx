import type { ReactNode } from "react";
import type { ProposalView as Proposal } from "./contracts";

/** A persisted server proposal is evidence for review, not a browser write command. */
export function ProposalView({ proposal, children, approvalAvailable = false }: { proposal: Proposal; children?: ReactNode; approvalAvailable?: boolean }) {
  return <section aria-label="不可变提案">
    <h4>临时席位提案（只读）</h4>
    <p role="note">{approvalAvailable
      ? "以下为服务端保存的提案，参数不可修改。批准决定与业务授予是两个步骤。"
      : "以下为服务端保存的提案。主管可在审批页查看当前状态，是否可决定以服务器校验为准；本页不会批准、扩容或修改提案参数。"}</p>
    <dl className="metadata">
      <div><dt>客户编号</dt><dd>{proposal.customer_id}</dd></div>
      <div><dt>订阅编号</dt><dd>{proposal.subscription_id}</dd></div>
      <div><dt>申请临时席位</dt><dd aria-label="提案席位数">{proposal.seats}</dd></div>
      <div><dt>申请天数</dt><dd aria-label="提案天数">{proposal.days}</dd></div>
      <div><dt>规则版本</dt><dd>{proposal.policy_version}</dd></div>
      <div><dt>业务版本</dt><dd>{proposal.business_version}</dd></div>
      <div><dt>提案创建时间</dt><dd><time dateTime={proposal.created_at}>{proposal.created_at}</time></dd></div>
      <div><dt>提案到期时间</dt><dd><time dateTime={proposal.expires_at}>{proposal.expires_at}</time></dd></div>
    </dl>
    <p>有效期以服务端判断为准；如需调整参数，须重新形成提案。</p>
    <details><summary>提案标识与版本</summary><dl className="metadata">
      <div><dt>提案编号</dt><dd className="mono">{proposal.proposal_id}</dd></div>
      <div><dt>动作编号</dt><dd className="mono">{proposal.action_id}</dd></div>
      {"approval_id" in proposal && typeof proposal.approval_id === "string" &&
        <div><dt>审批引用编号</dt><dd className="mono">{proposal.approval_id}</dd></div>}
      <div><dt>提案序号</dt><dd>{proposal.proposal_seq}</dd></div>
      <div><dt>动作类型</dt><dd>{proposal.action_type}</dd></div>
      <div><dt>命令格式版本</dt><dd>{proposal.command_schema_version}</dd></div>
      <div><dt>参数摘要</dt><dd className="mono">{proposal.canonical_args_hash}</dd></div>
    </dl></details>
    {children ?? <section aria-label="提案依据"><h4>提案依据</h4>
      <ul>{proposal.evidence_refs.map(ref => <li key={ref.evidence_id}>
        {ref.title} · {ref.source} · 第 {ref.start_line}–{ref.end_line} 行 · 文档版本 {ref.version_id}
      </li>)}</ul></section>}
    <details><summary>事实观察记录</summary><ul>{proposal.observation_refs.map(ref =>
      <li key={`${ref.run_id}:${ref.execution_generation}:${ref.ordinal}`}>
        {ref.tool_id} · 运行 {ref.run_id} · 执行代 {ref.execution_generation} · 观察 {ref.ordinal}
      </li>)}</ul></details>
  </section>;
}
