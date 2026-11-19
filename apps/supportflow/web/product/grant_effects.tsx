import type { GrantExecutionResult } from "./contracts";

const verificationLabels = {
  verified_current: "当前订阅与回执一致",
  verified_historical: "已确认历史授予，当前订阅已有后续变化",
  mismatch: "当前订阅与回执存在矛盾，等待核对",
  pending: "等待只读核验",
};

export function GrantEffects({ result }: { result: GrantExecutionResult }) {
  return <section aria-label="临时席位效果" data-effect-status={result.effect_status}>
    <h4>临时席位效果</h4>
    {result.effect_status === "unknown" && <p role="note">写入结果尚未确认，需按原动作核对回执。请勿重复申请。</p>}
    {result.effect_status === "none" && <p>本动作没有已确认的授予回执。</p>}
    <p>动作编号：<span className="mono" aria-label="动作编号">{result.action_id}</span></p>
    {result.action_receipts.map(receipt => <dl className="metadata" key={receipt.action_id} aria-label="授予回执">
      <div><dt>回执编号</dt><dd className="mono" aria-label="回执编号">{receipt.action_id}</dd></div>
      <div><dt>临时席位数</dt><dd aria-label="已授予临时席位">{receipt.seats}</dd></div>
      <div><dt>授予天数</dt><dd aria-label="授予天数">{receipt.days}</dd></div>
      <div><dt>业务授予时间</dt><dd><time dateTime={receipt.committed_business_at}>{new Date(receipt.committed_business_at).toLocaleString("zh-CN")}</time></dd></div>
      <div><dt>临时额度到期</dt><dd aria-label="临时额度到期"><time dateTime={receipt.temporary_expires_at}>{new Date(receipt.temporary_expires_at).toLocaleString("zh-CN")}</time></dd></div>
      <div><dt>授予时订阅版本</dt><dd>{receipt.before_version} → {receipt.after_version}</dd></div>
    </dl>)}
    {result.verification && <p aria-label="效果核验">{verificationLabels[result.verification.status]}</p>}
    {result.verification?.subscription && <dl className="metadata" aria-label="写后订阅事实">
      <div><dt>当前订阅版本</dt><dd>{result.verification.subscription.business_version}</dd></div>
      <div><dt>当前基础席位</dt><dd>{result.verification.subscription.base_seats}</dd></div>
      <div><dt>当前已用席位</dt><dd>{result.verification.subscription.used_seats}</dd></div>
      <div><dt>存储的临时席位</dt><dd>{result.verification.subscription.temporary_seats}</dd></div>
    </dl>}
    {result.effect_status === "committed" && <p>回执记录历史授予。取消本次处理不会撤回已提交的临时额度；额度按回执时间到期。</p>}
  </section>;
}
