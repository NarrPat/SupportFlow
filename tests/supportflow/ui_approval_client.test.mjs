import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";

const source = await readFile(new URL("../../apps/supportflow/web/product/approval_client.ts", import.meta.url), "utf8");
const { ApprovalClient, validReason } = await import("data:text/javascript;base64," +
  Buffer.from(stripTypeScriptTypes(source, { mode: "transform" })).toString("base64"));
const approval = { approval_id: "approval-1", run_id: "run-1", status: "pending", version: 1,
  decision: null, reason: null, decided_by: null, decided_at: null, can_decide: true,
  run_status: "awaiting_approval", proposal: { proposal_id: "proposal-1", action_id: "action-1" } };
const result = { approval_id: "approval-1", run_id: "run-1", status: "resume_queued", approval_status: "approved",
  version: 2, decision: "approve", reason: "同意", decided_by: "supervisor-2", decided_at: "2026-11-07T00:01:00Z" };
const tick = () => new Promise(resolve => setImmediate(resolve));
function harness(initial = approval) {
  const posts = [], reads = [], states = [], received = []; let serial = 0;
  const client = new ApprovalClient(initial, {
    key: () => `key-${++serial}`,
    post: (path, body) => new Promise((resolve, reject) => posts.push({ path, body, resolve, reject })),
    snapshot: (path, signal) => new Promise((resolve, reject) => reads.push({ path, signal, resolve, reject })),
    received: row => received.push(row),
  }, state => states.push(state));
  return { client, posts, reads, states, received };
}

test("reason follows frozen Unicode boundaries without trimming", () => {
  for (const value of ["", "  ", "同意\n核对", "😀".repeat(1000)]) assert.equal(validReason(value), true);
  for (const value of [null, 1, "a".repeat(1001), "\0", "\ud800"]) assert.equal(validReason(value), false);
});
test("double click sends once using approval version, then a fresh authorized GET", async () => {
  const h = harness(); const first = h.client.submit("approve", "同意");
  const second = h.client.submit("approve", "同意");
  assert.strictEqual(first, second); assert.equal(h.posts.length, 1);
  assert.deepEqual(h.posts[0].body, { decision: "approve", reason: "同意", expected_version: 1, client_request_id: "key-1" });
  h.posts[0].resolve(result); await first; await tick();
  assert.equal(h.reads.length, 1); assert.equal(h.reads[0].path, "/approvals/approval-1");
  assert.equal(h.states.at(-1).approval.status, "approved");
  assert.equal(h.states.at(-1).approval.run_status, "resume_queued");
  h.reads[0].resolve({ ...approval, status: "approved", version: 2, can_decide: false, run_status: "handoff" }); await tick();
  assert.equal(h.received.length, 1);
});
test("429 preserves original body and key, while changed reason starts a new request", async () => {
  const h = harness(); let work = h.client.submit("approve", "  同意  ");
  h.posts[0].reject({ status: 429 }); await work;
  work = h.client.submit("approve", "  同意  ");
  assert.strictEqual(h.posts[1].body, h.posts[0].body);
  h.posts[1].reject({ status: 429 }); await work;
  work = h.client.submit("reject", "新意见");
  assert.equal(h.posts[2].body.client_request_id, "key-2");
  h.posts[2].reject({ status: 422 }); await work;
});
test("ambiguous delivery forbids edited decision and permits the identical retry", async () => {
  const h = harness(); let work = h.client.submit("approve", "同意");
  h.posts[0].reject(new Error("connection lost")); await work;
  assert.equal(h.states.at(-1).ambiguous, true);
  await h.client.submit("reject", "改为拒绝"); assert.equal(h.posts.length, 1);
  work = h.client.submit("approve", "同意"); assert.strictEqual(h.posts[1].body, h.posts[0].body);
  h.posts[1].reject({ status: 403 }); await work;
  await h.client.submit("approve", "同意"); assert.equal(h.posts.length, 2);
  assert.equal(h.states.at(-1).phase, "denied");
});
test("409 reloads current approval and cannot blindly resubmit the stale version", async () => {
  const h = harness(); const work = h.client.submit("approve", "同意");
  h.posts[0].reject({ status: 409 }); await work; await tick();
  assert.equal(h.reads.length, 1);
  await h.client.submit("approve", "同意"); assert.equal(h.posts.length, 1);
  h.reads[0].resolve({ ...approval, status: "rejected", version: 2, can_decide: false }); await tick();
  assert.equal(h.states.at(-1).phase, "closed");
});
test("wrong-approval acceptance cannot close or update the current object", async () => {
  const h = harness(); const work = h.client.submit("approve", "同意");
  h.posts[0].resolve({ ...result, approval_id: "other" }); await work;
  assert.equal(h.states.at(-1).ambiguous, true); assert.equal(h.received.length, 0);
});
test("switch or dispose discards late POST and aborts the owned read", async () => {
  const h = harness(); const work = h.client.submit("approve", "同意"); h.client.dispose();
  const count = h.states.length; h.posts[0].resolve(result); await work;
  assert.equal(h.states.length, count); assert.equal(h.received.length, 0);
  const other = harness(); const refresh = other.client.refresh();
  other.client.dispose(); assert.equal(other.reads[0].signal.aborted, true);
  other.reads[0].resolve({ ...approval, version: 2 }); await refresh;
  assert.equal(other.received.length, 0);
});
test("server can_decide false, older and foreign snapshots never authorize a POST", async () => {
  const h = harness({ ...approval, can_decide: false });
  await h.client.submit("approve", ""); assert.equal(h.posts.length, 0);
  assert.equal(h.client.acceptSnapshot({ ...approval, approval_id: "other" }), false);
  const current = harness({ ...approval, version: 2 });
  assert.equal(current.client.acceptSnapshot(approval), false);
});
