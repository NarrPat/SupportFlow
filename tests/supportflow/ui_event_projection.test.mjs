// Expected projections are frozen JSON, not values computed by production code.
// Strip types in memory with the already pinned Node runtime; no bundler/server is started.
import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import { test } from "node:test";
const fixture = JSON.parse(await readFile(new URL("../../evals/fixtures/supportflow/event_ui/cases.json", import.meta.url), "utf8"));
const root = new URL("../../apps/supportflow/web/product/", import.meta.url);
function moduleUrl(source) {
  const js = stripTypeScriptTypes(source, { mode: "transform" });
  return "data:text/javascript;base64," + Buffer.from(js).toString("base64");
}
const projectionUrl = moduleUrl(await readFile(new URL("event_projection.ts", root), "utf8"));
const { initialProjection, applyEvent, applySnapshot, mergeHistory } = await import(projectionUrl);
const clientSource = (await readFile(new URL("event_client.ts", root), "utf8")).replace('from "./event_projection"', `from "${projectionUrl}"`);
const { RunEventClient } = await import(moduleUrl(clientSource));
function envelope(event) {
  return { schema_version: fixture.envelope.supported_major, spec_version: fixture.envelope.spec_version,
    occurred_at: fixture.envelope.timestamp, ...event };
}
for (const item of fixture.projection_cases) {
  test(item.id, () => {
    let projection = initialProjection(structuredClone(fixture.base_snapshot));
    for (const event of item.events) projection = applyEvent(projection, envelope(event));
    if (item.snapshot) projection = applySnapshot(projection, { ...fixture.base_snapshot, ...item.snapshot });
    for (const event of item.after_snapshot_events ?? []) projection = applyEvent(projection, envelope(event));
    const actual = { cursor: projection.cursor, version: projection.version, status: projection.snapshot.status,
      event_seqs: projection.events.map(event => event.seq), recovery: projection.recovery,
      snapshot_required: projection.snapshotRequired };
    if ("messages" in item.expected) actual.messages = projection.messages;
    assert.deepEqual(actual, item.expected);
  });
}
test("history is read-only, deduplicated and ordered independently from current status", () => {
  const snapshot = { ...fixture.base_snapshot, version: 3, last_event_seq: 5, status: "handoff" };
  const values = [
    envelope({ run_id: "run-ui-a", seq: 5, type: "message.final", status_version: 3, payload: { message_id: "reply-a", text: "final" } }),
    envelope({ run_id: "run-ui-a", seq: 1, type: "run.accepted", status_version: 1, payload: { status: "queued" } }),
    envelope({ run_id: "run-ui-b", seq: 2, type: "run.status_changed", status_version: 99, payload: { status: "succeeded" } }),
  ];
  const projection = mergeHistory(initialProjection(snapshot), [...values, values[0]]);
  assert.deepEqual(projection.snapshot, snapshot);
  assert.equal(projection.cursor, 5);
  assert.deepEqual(projection.events.map(event => event.seq), [1, 5]);
  assert.deepEqual(projection.messages, [{ message_id: "reply-a", text: "final", final: true }]);
});
test("malformed message cannot replace final text", () => {
  const projection = applyEvent(initialProjection(fixture.base_snapshot), envelope({ run_id: "run-ui-a", seq: 3,
    type: "message.final", status_version: 2, payload: { message_id: "reply-a", text: { unsafe: true } } }));
  assert.equal(projection.recovery, "invalid_event");
  assert.equal(projection.cursor, 2);
  assert.deepEqual(projection.messages, []);
});
test("equal state version cannot rewrite status", () => {
  const initial = initialProjection(fixture.base_snapshot);
  const event = applyEvent(initial, envelope({ run_id: "run-ui-a", seq: 3,
    type: "run.status_changed", status_version: 2, payload: { status: "queued" } }));
  assert.equal(event.recovery, "invalid_event");
  assert.equal(event.snapshot.status, "running");
  assert.equal(event.cursor, 2);
  const snapshot = applySnapshot(initial, { ...fixture.base_snapshot, status: "queued", last_event_seq: 3 });
  assert.equal(snapshot.snapshot.status, "running");
  assert.equal(snapshot.cursor, 2);
});

for (const status of ["awaiting_input", "awaiting_approval"]) {
  test(`${status} metadata before its status event accepts the persisted state`, () => {
    const metadata = envelope({ run_id: "run-ui-a", seq: 3,
      type: status === "awaiting_input" ? "clarification.required" : "approval.required",
      status_version: 3, payload: { version: 1 } });
    const pending = applyEvent(initialProjection(fixture.base_snapshot), metadata);
    assert.equal(pending.version, 3);
    assert.equal(pending.snapshot.version, 2);
    const fromEvent = applyEvent(pending, envelope({ run_id: "run-ui-a", seq: 4,
      type: "run.status_changed", status_version: 3, payload: { status } }));
    assert.equal(fromEvent.recovery, null);
    assert.equal(fromEvent.snapshot.status, status);
    const snapshot = { ...fixture.base_snapshot, status, version: 3, last_event_seq: 4 };
    const fromSnapshot = applySnapshot(pending, snapshot);
    assert.equal(fromSnapshot.snapshot.status, status);
    assert.equal(fromSnapshot.snapshotRequired, false);
  });
}

const settle = async () => { for (let i = 0; i < 8; i++) await Promise.resolve(); };
function harness(snapshot = { ...fixture.base_snapshot, last_event_seq: 5 }) {
  const streams = [], urls = [], schedules = [], views = [];
  let posts = 0, snapshotCalls = 0;
  const transport = {
    snapshot: async () => { snapshotCalls++; return structuredClone(snapshot); },
    history: async () => ({ items: [], next_after: 0, has_more: false, min_seq: 1, last_seq: snapshot.last_event_seq, snapshot_url: "/unused" }),
    open: url => {
      urls.push(url);
      const handlers = new Map();
      const stream = { onmessage: null, onopen: null, onerror: null, closes: 0,
        addEventListener: (name, callback) => handlers.set(name, callback), close() { this.closes++; }, handlers };
      streams.push(stream); return stream;
    },
    schedule: callback => { const item = { callback, cancelled: false }; schedules.push(item); return () => { item.cancelled = true; }; },
    post: () => { posts++; throw new Error("Events must never POST"); },
  };
  const client = new RunEventClient(structuredClone(fixture.base_snapshot), transport, view => views.push(structuredClone(view)));
  return { client, streams, urls, schedules, views, posts: () => posts, snapshotCalls: () => snapshotCalls };
}
test(fixture.client_cases[0].id, async () => {
  const expected = fixture.client_cases[0];
  const h = harness(); h.client.start(); await settle();
  assert.equal(h.urls[0], expected.expected_first_path);
  h.streams[0].onerror(); await settle();
  assert.equal(h.snapshotCalls(), 1);
  assert.equal(h.schedules.length, 1);
  h.schedules[0].callback(); await settle();
  assert.equal(h.urls[1], expected.expected_second_path);
  h.client.dispose();
  assert.equal(h.streams.reduce((sum, stream) => sum + stream.closes, 0), expected.expected_close_count);
  assert.equal(h.posts(), expected.expected_post_count);
});
test(fixture.client_cases[1].id, async () => {
  const h = harness(); h.client.start(); await settle();
  h.client.dispose(); const before = h.views.length;
  h.streams[0].onmessage({ data: JSON.stringify(envelope({ run_id: "run-ui-a", seq: 3,
    type: "run.status_changed", status_version: 3, payload: { status: "succeeded" } })) });
  h.streams[0].onerror(); h.streams[0].onopen(); await settle();
  assert.equal(h.views.length - before, fixture.client_cases[1].expected_projection_callbacks_after_dispose);
  assert.equal(h.posts(), fixture.client_cases[1].expected_post_count);
  assert.equal(h.snapshotCalls(), 0);
});
test("unknown major reads one snapshot and stops applying unsupported protocol", async () => {
  const h = harness(); h.client.start(); await settle();
  h.streams[0].onmessage({ data: JSON.stringify(envelope({ schema_version: 2, run_id: "run-ui-a", seq: 3,
    type: "run.status_changed", status_version: 3, payload: { status: "succeeded" } })) });
  await settle();
  assert.equal(h.snapshotCalls(), 1);
  assert.equal(h.views.at(-1).connection, "unsupported");
  assert.equal(h.views.at(-1).projection.snapshot.status, "running");
  assert.equal(h.schedules.length, 0); h.client.dispose();
});
test("access revoked hides cached resource without cancellation or reconnection", async () => {
  const h = harness(); h.client.start(); await settle();
  h.streams[0].handlers.get("access.revoked")({ data: "{}" });
  assert.equal(h.views.at(-1).denied, true);
  assert.equal(h.views.at(-1).connection, "closed");
  assert.equal(h.schedules.length, 0);
  assert.equal(h.posts(), 0); h.client.dispose();
});
test("history denial closes the reader and fences late snapshots", async () => {
  const h = harness();
  const views = [];
  let closeCount = 0;
  const client = new RunEventClient(structuredClone(fixture.base_snapshot), {
    snapshot: async () => structuredClone(fixture.base_snapshot),
    history: async () => { throw { status: 404 }; },
    open: () => ({ onmessage: null, onopen: null, onerror: null, addEventListener() {}, close() { closeCount++; } }),
    schedule: callback => { h.schedules.push(callback); return () => {}; },
  }, view => views.push(structuredClone(view)));
  client.start(); await settle();
  const last = views.at(-1);
  assert.equal(last.denied, true);
  assert.equal(last.connection, "closed");
  assert.equal(last.historyLoading, false);
  client.acceptSnapshot({ ...fixture.base_snapshot, version: 9, last_event_seq: 9 });
  assert.equal(views.at(-1), last);
  assert.equal(closeCount, 1);
  assert.equal(h.schedules.length, 0);
  client.dispose();
});
test("a terminal event during an older snapshot read schedules another read for its result", async () => {
  let finishFirst;
  let calls = 0;
  const views = [], streams = [], schedules = [];
  const result = { schema_version: 1, resolution_code: "DIAGNOSTIC_TOOLS_PENDING", customer: null, reply_text: "需要人工核查。" };
  const client = new RunEventClient({ ...fixture.base_snapshot, status: "queued", version: 1, last_event_seq: 1 }, {
    snapshot: () => {
      calls++;
      return calls === 1 ? new Promise(resolve => { finishFirst = resolve; }) :
        Promise.resolve({ ...fixture.base_snapshot, status: "handoff", version: 3, last_event_seq: 3, result });
    },
    history: async () => ({ items: [], next_after: 0, has_more: false, min_seq: 1, last_seq: 3, snapshot_url: "/unused" }),
    open: () => { const stream = { onmessage: null, onopen: null, onerror: null, addEventListener() {}, close() {} }; streams.push(stream); return stream; },
    schedule: callback => { schedules.push(callback); return () => {}; },
  }, view => views.push(structuredClone(view)));
  client.start();
  streams[0].onmessage({ data: JSON.stringify(envelope({ run_id: "run-ui-a", seq: 2, type: "run.started", status_version: 2, payload: { status: "running" } })) });
  streams[0].onmessage({ data: JSON.stringify(envelope({ run_id: "run-ui-a", seq: 3, type: "run.status_changed", status_version: 3, payload: { status: "handoff" } })) });
  assert.equal(calls, 1);
  finishFirst(structuredClone(fixture.base_snapshot)); await settle();
  assert.equal(views.at(-1).projection.snapshot.status, "handoff");
  assert.equal(views.at(-1).projection.snapshot.result, null);
  assert.equal(schedules.length, 1);
  schedules[0](); await settle();
  assert.equal(calls, 2);
  assert.deepEqual(views.at(-1).projection.snapshot.result, result);
  assert.equal(views.at(-1).projection.snapshotRequired, false);
  client.dispose();
});
