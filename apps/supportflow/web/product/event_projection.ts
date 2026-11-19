import type { PublicEvent, RunDetail } from "./contracts";

export type PublicRunEvent = PublicEvent & { spec_version: null };
export type RunMessage = { message_id: string; text: string; final: boolean };
export type Recovery = "sequence_gap" | "unsupported_schema" | "unsupported_spec" | "invalid_event";
export type RunProjection = {
  snapshot: RunDetail; cursor: number; version: number; events: PublicRunEvent[];
  messages: RunMessage[]; recovery: Recovery | null; snapshotRequired: boolean;
};
const statuses = new Set<string>(["queued", "running", "awaiting_input", "awaiting_approval",
  "resume_queued", "succeeded", "handoff", "rejected", "expired", "failed", "cancelled", "needs_reconciliation"]);
const stateEvents = new Set(["run.accepted", "run.started", "run.resumed", "run.recovering",
  "run.status_changed", "run.completed", "run.failed", "run.cancelled"]);
const record = (value: unknown): value is Record<string, unknown> =>
  value !== null && typeof value === "object" && !Array.isArray(value);
const counter = (value: unknown): value is number => Number.isSafeInteger(value) && Number(value) >= 0;

export function initialProjection(snapshot: RunDetail): RunProjection {
  return { snapshot, cursor: snapshot.last_event_seq, version: snapshot.version,
    events: [], messages: [], recovery: null, snapshotRequired: false };
}

function recover(state: RunProjection, recovery: Recovery): RunProjection {
  return { ...state, recovery, snapshotRequired: true };
}

function eventShape(value: unknown): value is PublicRunEvent {
  return record(value) && value.schema_version === 1 &&
    (value.app === undefined || value.app === "supportflow") && typeof value.run_id === "string" &&
    counter(value.seq) && value.seq > 0 && typeof value.type === "string" &&
    counter(value.status_version) && value.spec_version === null &&
    typeof value.occurred_at === "string" && Number.isFinite(Date.parse(value.occurred_at)) && record(value.payload);
}

function messagesFrom(events: PublicRunEvent[]): RunMessage[] {
  const messages = new Map<string, RunMessage>();
  for (const event of events) {
    if (event.type !== "message.delta" && event.type !== "message.final") continue;
    const id = event.payload.message_id;
    const text = event.type === "message.final" ? event.payload.text : event.payload.delta;
    if (typeof id !== "string" || typeof text !== "string") continue;
    const previous = messages.get(id);
    if (event.type === "message.final") messages.set(id, { message_id: id, text, final: true });
    else if (!previous?.final) messages.set(id, { message_id: id, text: (previous?.text ?? "") + text, final: false });
  }
  return [...messages.values()];
}

/** Pure projection: an event cannot create a request, invoke a tool or apply to a different run. */
export function applyEvent(state: RunProjection, value: unknown): RunProjection {
  if (!record(value)) return recover(state, "invalid_event");
  if (value.run_id !== state.snapshot.run_id) return state;
  if (counter(value.seq) && value.seq <= state.cursor) return state;
  if (state.recovery) return state;
  if (value.schema_version !== 1) return recover(state, "unsupported_schema");
  if (value.type === "event.unsupported") return recover(state, "unsupported_schema");
  if (value.spec_version !== null) return recover(state, "unsupported_spec");
  if (!eventShape(value)) return recover(state, "invalid_event");
  if (value.seq !== state.cursor + 1) return recover(state, "sequence_gap");
  if ((value.type === "message.delta" || value.type === "message.final") &&
      (typeof value.payload.message_id !== "string" ||
       typeof value.payload[value.type === "message.final" ? "text" : "delta"] !== "string")) {
    return recover(state, "invalid_event");
  }
  let snapshot = { ...state.snapshot, last_event_seq: value.seq };
  let snapshotRequired = state.snapshotRequired || value.status_version > state.version;
  if (stateEvents.has(value.type) && value.status_version >= state.version) {
    if (typeof value.payload.status !== "string" || !statuses.has(value.payload.status)) return recover(state, "invalid_event");
    // Metadata can announce a newer version before its status event arrives.
    // Only a version whose status is already known can conflict with that status.
    if (value.status_version === state.snapshot.version && value.payload.status !== state.snapshot.status) return recover(state, "invalid_event");
    snapshot = { ...snapshot, status: value.payload.status as RunDetail["status"], version: value.status_version,
      reason_code: typeof value.payload.reason_code === "string" ? value.payload.reason_code : snapshot.reason_code };
    snapshotRequired = true;
  }
  const events = [...state.events, value];
  return { ...state, snapshot, cursor: value.seq, version: Math.max(state.version, value.status_version),
    events, messages: messagesFrom(events), snapshotRequired };
}

/** Snapshot state and cursor are accepted together. A late response cannot roll either backwards. */
export function applySnapshot(state: RunProjection, snapshot: RunDetail): RunProjection {
  if (snapshot.run_id !== state.snapshot.run_id || snapshot.version < state.version || snapshot.last_event_seq < state.cursor) return state;
  if (snapshot.version === state.snapshot.version && snapshot.status !== state.snapshot.status) return state;
  return { ...state, snapshot, cursor: snapshot.last_event_seq, version: snapshot.version,
    recovery: null, snapshotRequired: false };
}

/** Historical reads enrich the timeline only; replay never changes current status or executes work. */
export function mergeHistory(state: RunProjection, values: unknown[]): RunProjection {
  const events = new Map(state.events.map(event => [event.seq, event]));
  for (const value of values) {
    if (!eventShape(value) || value.run_id !== state.snapshot.run_id || value.seq > state.cursor) continue;
    if (!events.has(value.seq)) events.set(value.seq, value);
  }
  const ordered = [...events.values()].sort((left, right) => left.seq - right.seq);
  return { ...state, events: ordered, messages: messagesFrom(ordered) };
}
