import { useState } from "react";
import { useQuery } from "@tanstack/react-query";
import { ApiError, describeError, type SessionApi } from "./api";
import type { Actor, EvaluationReport, EvaluationReportsPage } from "./contracts";
import "./evaluation.css";

const PAGE_SIZE = 20;
const failureLabels = {
  product: "产品", infrastructure: "基础设施", fixture: "测试数据",
  oracle: "判定器", runner: "执行器", not_implemented: "未实现",
};

function ReportTime({ value }: { value: string }) {
  const time = new Date(value).toLocaleString("zh-CN", { hour12: false });
  return <time dateTime={value}>{time}</time>;
}

function ReportDetail({ api, actor, id, close }: {
  api: SessionApi; actor: Actor; id: string; close: () => void;
}) {
  const report = useQuery({
    queryKey: ["evaluation-report", actor.tenant_id, actor.actor_id, actor.role, id],
    queryFn: ({ signal }) => api.get<EvaluationReport>(`/eval/reports/${encodeURIComponent(id)}`, signal),
  });
  const detail = report.data;
  return <section className="ticket-panel evaluation-detail" aria-label="评测报告详情">
    <div className="section-heading"><h2>{detail && !report.isFetching && !report.isError ? detail.title : "评测报告详情"}</h2>
      <button type="button" onClick={close}>返回报告列表</button>
    </div>
    {report.isPending || report.isFetching ? <p role="status">正在读取评测报告…</p> : report.isError ? <>
      <p role="alert">{report.error instanceof ApiError && report.error.status === 404
        ? "评测报告不可见或已移除。" : describeError(report.error)}</p>
      <button type="button" onClick={() => void report.refetch()}>重新加载</button>
    </> : detail && <>
      <dl className="metadata">
        <div><dt>实验编号</dt><dd>{detail.experiment_id}</dd></div>
        <div><dt>生成时间</dt><dd><ReportTime value={detail.generated_at}/></dd></div>
        <div><dt>测量范围</dt><dd>{detail.scope}</dd></div>
      </dl>
      {detail.experiment_url && <a href={detail.experiment_url} target="_blank" rel="noreferrer">查看实验记录</a>}
      <section aria-label="基线配置">
        <h3>基线配置</h3>
        {detail.baseline ? <dl className="metadata">
          <div><dt>实验臂</dt><dd>{detail.baseline.arm}</dd></div>
          <div><dt>模型</dt><dd>{detail.baseline.model_id}</dd></div>
          <div><dt>图版本</dt><dd>{detail.baseline.graph_version}</dd></div>
          <div><dt>源码版本</dt><dd>{detail.baseline.source_revision}</dd></div>
        </dl> : <p>报告未发布基线配置。</p>}
      </section>
      <section aria-label="评测指标">
        <h3>评测指标</h3>
        {detail.metrics.length ? <table aria-label="指标分子和分母">
          <thead><tr><th scope="col">指标</th><th scope="col">分子</th><th scope="col">分母</th></tr></thead>
          <tbody>{detail.metrics.map((metric, index) => <tr key={index}>
            <td>{metric.label}</td><td>{metric.numerator ?? "待实测"}</td><td>{metric.denominator ?? "待实测"}</td>
          </tr>)}</tbody>
        </table> : <p>报告未发布指标。</p>}
      </section>
      <section aria-label="失败分类">
        <h3>失败分类</h3>
        {detail.failure_classes.length ? <ul>
          {detail.failure_classes.map((failure, index) => <li key={index}>{failureLabels[failure.kind]} {failure.count} 次</li>)}
        </ul> : <p>报告未列出失败分类。</p>}
      </section>
    </>}
  </section>;
}

export function Evaluation({ api, actor }: { api: SessionApi; actor: Actor }) {
  const [selected, setSelected] = useState<string | null>(null);
  const [offset, setOffset] = useState(0);
  const reports = useQuery({
    queryKey: ["evaluation-reports", actor.tenant_id, actor.actor_id, actor.role, offset],
    queryFn: ({ signal }) => api.get<EvaluationReportsPage>(`/eval/reports?limit=${PAGE_SIZE}&offset=${offset}`, signal),
    enabled: selected === null,
  });
  return <main className="wrap tickets-page evaluation-page">
    <div className="page-heading"><h1>评测</h1></div>
    {selected ? <ReportDetail key={selected} id={selected} api={api} actor={actor} close={() => setSelected(null)}/> :
      <section className="ticket-panel" aria-label="已发布报告">
        <div className="toolbar section-heading"><h2>已发布报告</h2>
          <button type="button" disabled={reports.isFetching} onClick={() => void reports.refetch()}>刷新报告</button>
        </div>
        {reports.isPending || reports.isFetching ? <div className="empty"><p role="status">正在读取评测报告…</p></div> : reports.isError ?
          <div className="empty"><p role="alert">{describeError(reports.error)}</p>
            <button type="button" onClick={() => void reports.refetch()}>重新加载</button></div> : <>
            {reports.data.items.length === 0 ? <div className="empty" role="status"><h2>暂无已发布的评测报告</h2></div> :
              <table aria-label="评测报告列表">
                <thead><tr><th scope="col">报告</th><th scope="col">实验编号</th><th scope="col">生成时间</th></tr></thead>
                <tbody>{reports.data.items.map(row => <tr key={row.report_id}>
                  <td><button className="ticket-link" type="button" onClick={() => setSelected(row.report_id)}>{row.title}</button></td>
                  <td>{row.experiment_id}</td><td><ReportTime value={row.generated_at}/></td>
                </tr>)}</tbody>
              </table>}
            {(offset > 0 || reports.data.has_more) && <div className="pagination" aria-label="报告分页">
              <span>第 {Math.floor(offset / PAGE_SIZE) + 1} 页</span>
              <button type="button" disabled={offset === 0} onClick={() => setOffset(current => current - PAGE_SIZE)}>上一页</button>
              <button type="button" disabled={!reports.data.has_more} onClick={() => setOffset(current => current + PAGE_SIZE)}>下一页</button>
            </div>}
          </>}
      </section>}
  </main>;
}
