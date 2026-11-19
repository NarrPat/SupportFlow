import type { ClarificationSubmit, PendingInput, RunAccepted, RunDetail } from "./contracts";

export type AnswerField = "customer_id" | "invitation_email" | "seats" | "days";
export type Answers = { customer_id?: string; invitation_email?: string; seats?: number; days?: number };
export type AnswerDraft = Partial<Record<AnswerField, string>>;
export type ClarificationState = {
  run: RunDetail; phase: "idle" | "submitting" | "error" | "conflict" | "accepted" | "closed" | "denied";
  request: ClarificationSubmit | null; error: string | null; ambiguous: boolean; refreshing: boolean;
};
type Transport = {
  post(path: string, body: ClarificationSubmit): Promise<RunAccepted>;
  snapshot(path: string, signal: AbortSignal): Promise<RunDetail>;
  received(snapshot: RunDetail): void;
  key(): string;
};
const fields: readonly AnswerField[] = ["customer_id", "invitation_email", "seats", "days"];
const statuses = new Set<string>(["queued", "running", "awaiting_input", "awaiting_approval", "resume_queued",
  "succeeded", "handoff", "rejected", "expired", "failed", "cancelled", "needs_reconciliation"]);
const object = (value: unknown): value is Record<string, unknown> => value !== null && typeof value === "object" && !Array.isArray(value);
function validFields(required: readonly string[]): required is readonly AnswerField[] {
  return required.length > 0 && new Set(required).size === required.length && required.every(field => fields.includes(field as AnswerField));
}
const boundedString = (value: unknown, max: number): value is string =>
  typeof value === "string" && value.length > 0 && value.length <= max && value.trim() === value && !value.includes("\0");

/** This is a UX boundary. Current authorization and strict JSON types are checked again by the server. */
export function validAnswers(required: readonly string[], answers: unknown): answers is Answers {
  if (!validFields(required) || !object(answers) || Object.keys(answers).length !== required.length ||
      Object.keys(answers).some(field => !required.includes(field as AnswerField))) return false;
  return required.every(field => {
    const value = answers[field];
    if (field === "customer_id") return boundedString(value, 128);
    if (field === "invitation_email") return boundedString(value, 254) && /^[^\s@]+@[^\s@]+$/.test(value);
    return typeof value === "number" && Number.isSafeInteger(value) && value >= 1 && value <= (field === "seats" ? 20 : 7);
  });
}

/** Parse numeric form text deliberately: Number('1.0') must not quietly accept a decimal entry. */
export function answersFromDraft(required: readonly string[], draft: AnswerDraft):
  { ok: true; answers: Answers } | { ok: false; message: string } {
  if (!validFields(required)) return { ok: false, message: "当前补充信息格式暂不支持，请重新读取运行记录。" };
  const answers: Answers = {};
  for (const field of required) {
    const value = draft[field] ?? "";
    if (field === "seats" || field === "days") {
      if (!/^[1-9][0-9]*$/.test(value)) return { ok: false, message: "席位数和天数必须填写不带小数的正整数。" };
      answers[field] = Number(value);
    } else answers[field] = value;
  }
  return validAnswers(required, answers) ? { ok: true, answers } :
    { ok: false, message: "请补齐当前要求的信息；席位为 1–20，天数为 1–7。" };
}

export function initialClarification(run: RunDetail): ClarificationState {
  return { run, phase: "idle", request: null, error: null, ambiguous: false, refreshing: false };
}
const errorStatus = (error: unknown) => object(error) && typeof error.status === "number" ? error.status : null;

/** One actor/run/input owns one client. No timer, implicit retry, new-run or cancel operation exists here. */
export class ClarificationClient {
  private state: ClarificationState;
  private readonly input: PendingInput | null;
  private disposed = false;
  private inFlight: Promise<void> | null = null;
  private read: AbortController | null = null;
  private readSequence = 0;
  constructor(run: RunDetail, private transport: Transport, private changed: (state: ClarificationState) => void) {
    this.state = initialClarification(run);
    this.input = run.pending_input ?? null;
  }
  private update(values: Partial<ClarificationState>) {
    if (this.disposed) return;
    this.state = { ...this.state, ...values }; this.changed(this.state);
  }
  private currentInput() {
    const pending = this.state.run.pending_input;
    return this.state.run.status === "awaiting_input" && this.input && pending &&
      pending.input_id === this.input.input_id && pending.version === this.input.version;
  }
  acceptSnapshot(snapshot: RunDetail): boolean {
    if (this.disposed || snapshot.run_id !== this.state.run.run_id || snapshot.version < this.state.run.version ||
        snapshot.last_event_seq < this.state.run.last_event_seq ||
        (snapshot.version === this.state.run.version && snapshot.status !== this.state.run.status)) return false;
    this.update({ run: snapshot });
    if (!this.currentInput()) this.update({ phase: "closed", request: null, ambiguous: false, error: null });
    else if (this.state.phase === "conflict") this.update({ phase: "idle", request: null, ambiguous: false,
      error: "已读取当前等待信息，请核对内容后再提交。" });
    return true;
  }
  /** Editing after a definite rejection uses a new key; ambiguous delivery keeps the original body intact. */
  edit(): boolean {
    if (this.disposed || !this.currentInput() || this.state.ambiguous ||
        ["submitting", "accepted", "conflict", "closed", "denied"].includes(this.state.phase)) return false;
    this.update({ phase: "idle", request: null, error: null }); return true;
  }
  submit(answers: Answers): Promise<void> {
    if (this.inFlight) return this.inFlight;
    if (this.disposed || !this.currentInput() || ["accepted", "conflict", "closed", "denied"].includes(this.state.phase)) return Promise.resolve();
    if (!validAnswers(this.input!.required_fields, answers)) {
      this.update({ phase: "error", error: "请只填写当前要求的有效信息。" }); return Promise.resolve();
    }
    const prior = this.state.request;
    const same = prior && Object.keys(answers).every(key => answers[key as AnswerField] === prior.answers[key as AnswerField]);
    if (this.state.ambiguous && !same) return Promise.resolve();
    const body: ClarificationSubmit = same ? prior : Object.freeze({ input_id: this.input!.input_id,
      answers: Object.freeze({ ...answers }), expected_version: this.input!.version, client_request_id: this.transport.key() });
    this.update({ phase: "submitting", request: body, error: null });
    const work = this.send(body);
    this.inFlight = work;
    void work.finally(() => { if (this.inFlight === work) this.inFlight = null; });
    return work;
  }
  refuse(reason: string): Promise<void> {
    if (this.inFlight) return this.inFlight;
    if (this.disposed || !this.currentInput() || ["accepted", "conflict", "closed", "denied"].includes(this.state.phase)) return Promise.resolve();
    if (!reason.trim() || reason.length > 500) return Promise.resolve();
    const prior = this.state.request;
    const same = prior?.refusal_reason === reason;
    if (this.state.ambiguous && !same) return Promise.resolve();
    const body: ClarificationSubmit = same ? prior! : Object.freeze({ input_id: this.input!.input_id,
      answers: Object.freeze({}), refusal_reason: reason, expected_version: this.input!.version, client_request_id: this.transport.key() });
    this.update({ phase: "submitting", request: body, error: null });
    const work = this.send(body); this.inFlight = work;
    void work.finally(() => { if (this.inFlight === work) this.inFlight = null; });
    return work;
  }
  private async send(body: ClarificationSubmit) {
    try {
      const accepted = await this.transport.post(`/runs/${encodeURIComponent(this.state.run.run_id)}/clarifications`, body);
      if (this.disposed || !this.currentInput()) return;
      if (accepted.run_id !== this.state.run.run_id || !statuses.has(accepted.status)) {
        this.update({ phase: "error", ambiguous: true, error: "受理响应与当前运行不一致，请读取状态后核对。" }); return;
      }
      this.update({ phase: "accepted", ambiguous: false, error: null });
      void this.refresh();
    } catch (error) {
      if (this.disposed || !this.currentInput()) return;
      const status = errorStatus(error);
      if (status === 401 || status === 403 || status === 404) {
        this.update({ phase: "denied", request: null, ambiguous: false, error: "当前运行已不可操作，请重新确认登录和客户权限。" });
      } else if (status === 409) {
        this.update({ phase: "conflict", ambiguous: false, error: "等待信息或请求版本已变更，正在读取当前状态。" });
        void this.refresh();
      } else if (status === 429) {
        this.update({ phase: "error", ambiguous: false, error: "等待队列已满，已保留当前信息；请稍后重试。" });
      } else if (status === 422) {
        this.update({ phase: "error", ambiguous: false, error: "补充信息未通过核验，请检查当前要求的字段。" });
      } else this.update({ phase: "error", ambiguous: true,
        error: "尚未确认补充信息是否受理。请重试原提交，或重新读取状态；不要另建运行。" });
    }
  }
  async refresh(): Promise<void> {
    if (this.disposed || this.state.phase === "denied") return;
    this.read?.abort();
    const controller = new AbortController(), sequence = ++this.readSequence;
    this.read = controller; this.update({ refreshing: true });
    try {
      const snapshot = await this.transport.snapshot(`/runs/${encodeURIComponent(this.state.run.run_id)}`, controller.signal);
      if (this.disposed || controller.signal.aborted || sequence !== this.readSequence) return;
      if (this.acceptSnapshot(snapshot)) this.transport.received(snapshot);
    } catch (error) {
      if (this.disposed || controller.signal.aborted || sequence !== this.readSequence) return;
      if ([401, 403, 404].includes(errorStatus(error) ?? 0)) {
        this.update({ phase: "denied", request: null, ambiguous: false, error: "当前运行已不可访问，请重新确认权限。" });
      } else this.update({ error: this.state.phase === "accepted"
        ? "补充信息已受理，暂时无法读取后续状态；无需再次提交。" : "暂时无法读取当前状态，请稍后重新读取。" });
    } finally {
      if (!this.disposed && sequence === this.readSequence) this.update({ refreshing: false });
    }
  }
  dispose() { this.disposed = true; this.readSequence++; this.read?.abort(); }
}
