import type { ApprovalDecisionResult, ApprovalDecisionSubmit, ApprovalView } from "./contracts";

type Decision = "approve" | "reject";
type Phase = "idle" | "submitting" | "accepted" | "error" | "conflict" | "denied" | "closed";
export type ApprovalState = {
  approval: ApprovalView; phase: Phase; request: ApprovalDecisionSubmit | null;
  error: string | null; ambiguous: boolean; refreshing: boolean;
};
type Transport = {
  post(path: string, body: ApprovalDecisionSubmit): Promise<ApprovalDecisionResult>;
  snapshot(path: string, signal: AbortSignal): Promise<ApprovalView>;
  received(value: ApprovalView): void;
  key(): string;
};
const runStatuses = new Set(["queued", "running", "awaiting_input", "awaiting_approval", "resume_queued",
  "succeeded", "handoff", "rejected", "expired", "failed", "cancelled", "needs_reconciliation"]);
const statusOf = (error: unknown): number | null => error !== null && typeof error === "object" &&
  "status" in error && typeof error.status === "number" ? error.status : null;

/** Count Unicode codepoints like the server; UTF-16 length would reject valid emoji reasons. */
export function validReason(value: unknown): value is string {
  if (typeof value !== "string" || value.includes("\0")) return false;
  let count = 0;
  for (const character of value) {
    const codepoint = character.codePointAt(0)!;
    if (++count > 1000 || (codepoint >= 0xd800 && codepoint <= 0xdfff)) return false;
  }
  return true;
}

/** One signed-in actor and approval own this request lifetime; no automatic POST retry exists. */
export class ApprovalClient {
  private state: ApprovalState;
  private disposed = false;
  private inFlight: Promise<void> | null = null;
  private read: AbortController | null = null;
  private readSequence = 0;
  constructor(approval: ApprovalView, private transport: Transport, private changed: (state: ApprovalState) => void) {
    this.state = { approval, phase: "idle", request: null, error: null, ambiguous: false, refreshing: false };
  }
  private update(values: Partial<ApprovalState>) {
    if (this.disposed) return;
    this.state = { ...this.state, ...values }; this.changed(this.state);
  }
  acceptSnapshot(value: ApprovalView): boolean {
    const prior = this.state.approval;
    if (this.disposed || value.approval_id !== prior.approval_id || value.run_id !== prior.run_id ||
        value.version < prior.version || (value.version === prior.version && value.status !== prior.status)) return false;
    this.update({ approval: value });
    if (value.status !== "pending" || !value.can_decide)
      this.update({ phase: "closed", request: null, ambiguous: false });
    else if (this.state.phase === "conflict")
      this.update({ phase: "idle", request: null, error: "已读取当前审批，请核对后再提交。", ambiguous: false });
    return true;
  }
  submit(decision: Decision, reason: string): Promise<void> {
    if (this.inFlight) return this.inFlight;
    const { approval, phase, request: prior, ambiguous } = this.state;
    if (this.disposed || !approval.can_decide || approval.status !== "pending" ||
        ["accepted", "conflict", "denied", "closed"].includes(phase)) return Promise.resolve();
    if (!["approve", "reject"].includes(decision) || !validReason(reason)) {
      this.update({ phase: "error", error: "决定须为批准或拒绝，意见最多1000个字符且不能含无效字符。" });
      return Promise.resolve();
    }
    const same = prior && prior.decision === decision && prior.reason === reason && prior.expected_version === approval.version;
    if (ambiguous && !same) return Promise.resolve();
    const body = same ? prior : Object.freeze({ decision, reason, expected_version: approval.version,
      client_request_id: this.transport.key() });
    this.update({ phase: "submitting", request: body, error: null });
    const work = this.send(body); this.inFlight = work;
    void work.finally(() => { if (this.inFlight === work) this.inFlight = null; });
    return work;
  }
  private async send(body: ApprovalDecisionSubmit) {
    try {
      const prior = this.state.approval;
      const result = await this.transport.post(`/approvals/${encodeURIComponent(prior.approval_id)}/decisions`, body);
      if (this.disposed) return;
      if (result.approval_id !== prior.approval_id || result.run_id !== prior.run_id ||
          result.decision !== body.decision || !Number.isSafeInteger(result.version) || result.version < prior.version ||
          result.approval_status !== (body.decision === "approve" ? "approved" : "rejected") || !runStatuses.has(result.status)) {
        this.update({ phase: "error", ambiguous: true, error: "受理结果与当前审批不一致，请重新读取状态核对。" }); return;
      }
      this.update({ phase: "accepted", ambiguous: false, error: null, approval: {
        ...prior, status: result.approval_status, version: result.version, decision: result.decision, reason: result.reason,
        decided_by: result.decided_by, decided_at: result.decided_at, run_status: result.status, can_decide: false,
      } });
      void this.refresh();
    } catch (error) {
      if (this.disposed) return;
      const status = statusOf(error);
      if (status === 401 || status === 403 || status === 404)
        this.update({ phase: "denied", ambiguous: false, request: null, error: "当前审批已不可操作，请重新确认身份和客户权限。" });
      else if (status === 409) {
        this.update({ phase: "conflict", ambiguous: false, error: "审批状态或版本已变化，正在重新读取。" });
        void this.refresh();
      } else if (status === 429)
        this.update({ phase: "error", ambiguous: false, error: "待执行队列已满，意见已保留；请稍后重试原提交。" });
      else if (status === 422)
        this.update({ phase: "error", ambiguous: false, error: "审批字段未通过核验，请检查后重新提交。" });
      else this.update({ phase: "error", ambiguous: true,
        error: "尚未确认决定是否受理。请重试原提交或读取当前状态，确认前不能改为另一决定。" });
    }
  }
  async refresh(): Promise<void> {
    if (this.disposed || this.state.phase === "denied") return;
    this.read?.abort(); const read = new AbortController(), sequence = ++this.readSequence;
    this.read = read; this.update({ refreshing: true });
    try {
      const value = await this.transport.snapshot(`/approvals/${encodeURIComponent(this.state.approval.approval_id)}`, read.signal);
      if (this.disposed || read.signal.aborted || sequence !== this.readSequence) return;
      if (this.acceptSnapshot(value)) this.transport.received(value);
    } catch (error) {
      if (this.disposed || read.signal.aborted || sequence !== this.readSequence) return;
      if ([401, 403, 404].includes(statusOf(error) ?? 0))
        this.update({ phase: "denied", request: null, ambiguous: false, error: "当前审批已不可访问，请重新确认权限。" });
      else this.update({ error: "暂时无法读取审批，请稍后重新读取；已受理的决定无需重复提交。" });
    } finally {
      if (!this.disposed && sequence === this.readSequence) this.update({ refreshing: false });
    }
  }
  dispose() { this.disposed = true; this.read?.abort(); }
}
