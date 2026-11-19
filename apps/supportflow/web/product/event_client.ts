import type { RunDetail, RunEventsPage } from "./contracts";
import { applyEvent, applySnapshot, initialProjection, mergeHistory, type RunProjection } from "./event_projection";

export type EventHistory = RunEventsPage;
type Stream = { onmessage: ((event: MessageEvent<string>) => void) | null;
  onopen: ((event: Event) => void) | null; onerror: ((event: Event) => void) | null;
  addEventListener(type: string, callback: EventListener): void; close(): void };
export type EventView = { projection: RunProjection; connection: "connecting" | "live" | "reconnecting" | "closed" | "unsupported" | "error";
  historyLoading: boolean; historyError: string | null; denied: boolean };
export type ReadTransport = {
  snapshot(signal: AbortSignal): Promise<RunDetail>;
  history(after: number, signal: AbortSignal): Promise<EventHistory>;
  open(url: string): Stream;
  schedule(callback: () => void): () => void;
};
const eventTypes = ["run.accepted", "run.started", "run.resumed", "run.recovering", "run.status_changed",
  "run.completed", "run.failed", "run.cancelled", "stage.changed", "tool.started", "tool.finished", "message.delta", "message.final",
  "clarification.required", "approval.required", "approval.decided", "action.completed", "event.unsupported"];
const accessDenied = (error: unknown) => typeof error === "object" && error !== null &&
  "status" in error && [401, 403, 404].includes(Number(error.status));

/** One component owns one stream. This transport deliberately exposes no write method. */
export class RunEventClient {
  private view: EventView;
  private stream?: Stream;
  private readonly abort = new AbortController();
  private disposed = false;
  private refreshing = false;
  private recovering = false;
  private cancelTimer?: () => void;
  private historyGeneration = 0;
  constructor(snapshot: RunDetail, private transport: ReadTransport, private changed: (view: EventView) => void) {
    this.view = { projection: initialProjection(snapshot), connection: "connecting", historyLoading: false, historyError: null, denied: false };
  }
  start() { this.emit(); this.connect(); void this.loadHistory(); }
  acceptSnapshot(snapshot: RunDetail) {
    if (this.disposed || this.view.denied) return;
    const before = this.view.projection.cursor;
    this.view = { ...this.view, projection: applySnapshot(this.view.projection, snapshot) };
    this.emit();
    if (this.view.projection.cursor > before) void this.loadHistory();
  }
  dispose() {
    this.disposed = true;
    this.cancelTimer?.(); this.stream?.close(); this.stream = undefined;
    this.abort.abort();
  }
  private emit() { if (!this.disposed) this.changed(this.view); }
  private deny() {
    this.cancelTimer?.();
    this.stream?.close();
    this.stream = undefined;
    this.abort.abort();
    this.historyGeneration++;
    this.view = { ...this.view, connection: "closed", denied: true, historyLoading: false };
    this.emit();
  }
  private connect() {
    if (this.disposed || this.view.denied) return;
    this.stream?.close();
    const stream = this.transport.open(`/api/v1/runs/${encodeURIComponent(this.view.projection.snapshot.run_id)}/events?after=${this.view.projection.cursor}`);
    this.stream = stream;
    const current = () => !this.disposed && this.stream === stream;
    stream.onopen = () => { if (current()) { this.view = { ...this.view, connection: "live" }; this.emit(); } };
    const receive = (event: MessageEvent<string>) => {
      if (!current()) return;
      let value: unknown;
      try { value = JSON.parse(event.data); } catch { value = null; }
      this.view = { ...this.view, projection: applyEvent(this.view.projection, value) };
      this.emit();
      if (this.view.projection.recovery) void this.recover();
      else if (this.view.projection.snapshotRequired) void this.refresh();
    };
    stream.onmessage = receive;
    for (const name of eventTypes) stream.addEventListener(name, event => receive(event as MessageEvent<string>));
    stream.addEventListener("access.revoked", () => {
      if (!current()) return;
      this.deny();
    });
    stream.addEventListener("reset.required", () => { if (current()) void this.recover(); });
    stream.addEventListener("stream.error", () => { if (current()) void this.recover(); });
    stream.onerror = () => { if (current()) void this.recover(); };
  }
  private async refresh() {
    if (this.refreshing || this.disposed || this.view.denied || this.recovering) return;
    this.refreshing = true;
    try {
      const snapshot = await this.transport.snapshot(this.abort.signal);
      this.acceptSnapshot(snapshot);
    } catch (error) {
      if (this.disposed || this.view.denied) return;
      if (accessDenied(error)) this.deny();
      else void this.recover();
    }
    finally {
      this.refreshing = false;
      // A newer terminal event may arrive while this GET was reading an older
      // version. Its rejection must not strand the UI without the persisted result.
      if (!this.disposed && !this.view.denied && !this.recovering && this.view.projection.snapshotRequired && !this.view.projection.recovery) {
        this.cancelTimer = this.transport.schedule(() => { void this.refresh(); });
      }
    }
  }
  private async recover() {
    if (this.recovering || this.disposed || this.view.denied) return;
    this.recovering = true;
    this.stream?.close(); this.stream = undefined;
    const unsupported = this.view.projection.recovery === "unsupported_schema" || this.view.projection.recovery === "unsupported_spec";
    this.view = { ...this.view, connection: "reconnecting" }; this.emit();
    try {
      const snapshot = await this.transport.snapshot(this.abort.signal);
      if (this.disposed || this.view.denied) return;
      this.acceptSnapshot(snapshot);
      if (unsupported) {
        // Do not loop on a future protocol. Show its authoritative snapshot and ask for a compatible client.
        this.view = { ...this.view, connection: "unsupported" }; this.emit();
      } else {
        await this.loadHistory();
        if (!this.disposed && !this.view.denied) this.cancelTimer = this.transport.schedule(() => this.connect());
      }
    } catch (error) {
      if (this.disposed || this.view.denied) return;
      if (accessDenied(error)) this.deny();
      else {
        this.view = { ...this.view, connection: "error" }; this.emit();
        this.cancelTimer = this.transport.schedule(() => { void this.recover(); });
      }
    } finally { this.recovering = false; }
  }
  private async loadHistory() {
    if (this.disposed || this.view.denied) return;
    const generation = ++this.historyGeneration;
    this.view = { ...this.view, historyLoading: true, historyError: null }; this.emit();
    let after = 0;
    try {
      for (let page = 0; page < 20; page++) {
        const history = await this.transport.history(after, this.abort.signal);
        if (this.disposed || generation !== this.historyGeneration) return;
        this.view = { ...this.view, projection: mergeHistory(this.view.projection, history.items) };
        this.emit();
        if (!history.has_more) return;
        if (!Number.isSafeInteger(history.next_after) || history.next_after <= after) throw new Error("History cursor did not advance");
        after = history.next_after;
      }
      this.view = { ...this.view, historyError: "事件记录较多，当前仅展示已读取部分。" };
    } catch (error) {
      if (this.disposed || generation !== this.historyGeneration) return;
      if (accessDenied(error)) this.deny();
      else this.view = { ...this.view, historyError: "部分历史事件暂时无法读取或已过留存期限；当前状态以服务器快照为准。" };
    } finally {
      if (!this.disposed && generation === this.historyGeneration) {
        this.view = { ...this.view, historyLoading: false }; this.emit();
      }
    }
  }
}
