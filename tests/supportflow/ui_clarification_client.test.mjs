import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes, createRequire } from "node:module";
import { fileURLToPath, pathToFileURL } from "node:url";
import test from "node:test";

const product = new URL("../../apps/supportflow/web/product/", import.meta.url);
const source = await readFile(new URL("clarification_client.ts", product), "utf8");
const { ClarificationClient, answersFromDraft, validAnswers } = await import("data:text/javascript;base64," +
  Buffer.from(stripTypeScriptTypes(source, { mode: "transform" })).toString("base64"));
const fixture = JSON.parse(await readFile(new URL("../../evals/fixtures/supportflow/clarification_proposal/cases.json", import.meta.url), "utf8"));
const input = { input_id: "input_clarification_a_1", version: 1, prompt: "请选择本次核查的客户。",
  required_fields: ["customer_id"], expires_at: "2026-11-03T04:15:00Z" };
const waiting = { run_id: "run_clarification_a", version: 41, status: "awaiting_input", pending_input: input,
  proposal: null, last_event_seq: 12 };
const answer = { customer_id: fixture.trusted_context.customer_id };
function harness(initial = waiting) {
  const posts = [], reads = [], states = [], received = [];
  let nextKey = 0;
  const client = new ClarificationClient(initial, {
    key: () => "controlled-key-" + (++nextKey),
    post: (path, body) => new Promise((resolve, reject) => posts.push({ path, body, resolve, reject })),
    snapshot: (path, signal) => new Promise((resolve, reject) => reads.push({ path, signal, resolve, reject })),
    received: snapshot => received.push(snapshot),
  }, state => states.push(state));
  return { client, posts, reads, states, received };
}
const tick = () => new Promise(resolve => setImmediate(resolve));

test("frozen answer fields reject unknown keys and preserve strict JSON numbers", () => {
  for (const row of fixture.unit_cases.strict_answers.valid) assert.equal(validAnswers(row.required_fields, row.answers), true);
  for (const row of fixture.unit_cases.strict_answers.invalid) {
    // JSON.parse erases the lexical 1.0/1 distinction. Python verifies this wire
    // boundary; form tests below reject decimal text before JSON serialization.
    if (row.id.includes("_float_")) continue;
    assert.equal(validAnswers(row.required_fields, row.answers), false, row.id);
  }
  for (const text of ["1.0", "1e1", "+1", " 1 ", "", "0", "21", "NaN"]) {
    assert.equal(answersFromDraft(["seats"], { seats: text }).ok, false, text);
  }
  assert.deepEqual(answersFromDraft(["seats", "days"], { seats: "10", days: "3" }), { ok: true, answers: { seats: 10, days: 3 } });
  assert.equal(validAnswers(["customer_id", "customer_id"], answer), false);
  assert.equal(validAnswers(["arbitrary_model_field"], { arbitrary_model_field: "value" }), false);
});

test("submits the current input version, one POST on double click, then same-run GET", async () => {
  const h = harness();
  const first = h.client.submit(answer), second = h.client.submit(answer);
  assert.equal(h.posts.length, 1);
  assert.equal(h.posts[0].path, "/runs/run_clarification_a/clarifications");
  assert.deepEqual(h.posts[0].body, { input_id: input.input_id, answers: answer, expected_version: 1, client_request_id: "controlled-key-1" });
  assert.notEqual(h.posts[0].body.expected_version, waiting.version);
  h.posts[0].resolve({ run_id: waiting.run_id, status: "resume_queued" });
  await Promise.all([first, second]);
  assert.equal(h.states.at(-1).phase, "accepted");
  assert.equal(h.reads[0].path, "/runs/" + waiting.run_id);
  h.reads[0].resolve({ ...waiting, version: 42, status: "resume_queued", pending_input: null });
  await tick();
  assert.equal(h.received.length, 1);
  assert.equal(h.posts.length, 1);
});

test("lost response and 503 retain an immutable exact body/key until explicit retry", async () => {
  for (const failure of [new Error("network"), { status: 503, code: "DEPENDENCY_UNAVAILABLE" }]) {
    const h = harness();
    const first = h.client.submit(answer); h.posts[0].reject(failure); await first;
    const body = JSON.stringify(h.posts[0].body);
    assert.equal(h.client.edit(), false);
    assert.equal(h.states.at(-1).ambiguous, true);
    assert.equal(h.reads.length, 0);
    const again = h.client.submit(answer);
    assert.equal(JSON.stringify(h.posts[1].body), body);
    h.posts[1].resolve({ run_id: waiting.run_id, status: "succeeded" }); await again;
    assert.equal(h.states.at(-1).phase, "accepted");
    h.client.dispose();
  }
});

test("queue full preserves answers/input version/key and does not auto-submit", async () => {
  const h = harness();
  let pending = h.client.submit(answer); h.posts[0].reject({ status: 429 }); await pending;
  assert.match(h.states.at(-1).error, /队列已满/);
  assert.equal(h.states.at(-1).ambiguous, false);
  assert.equal(h.reads.length, 0);
  pending = h.client.submit(answer);
  assert.deepEqual(h.posts[1].body, h.posts[0].body);
  h.posts[1].reject({ status: 429 }); await pending;
  assert.equal(h.client.edit(), true);
  pending = h.client.submit({ customer_id: "cus_a_02" });
  assert.notEqual(h.posts[2].body.client_request_id, h.posts[0].body.client_request_id);
  h.posts[2].reject({ status: 422 }); await pending;
});

test("409 reads current state, preserves the UI draft, and cannot overwrite a newer snapshot", async () => {
  const h = harness();
  const pending = h.client.submit(answer); h.posts[0].reject({ status: 409 }); await pending;
  assert.equal(h.states.at(-1).phase, "conflict");
  assert.deepEqual(h.states.at(-1).request.answers, answer);
  const newer = { ...waiting, version: 43, status: "expired", pending_input: null };
  assert.equal(h.client.acceptSnapshot(newer), true);
  h.reads[0].resolve({ ...waiting, version: 42 }); await tick();
  assert.equal(h.states.at(-1).run.version, 43);
  assert.equal(h.received.length, 0);
  await h.client.submit(answer);
  assert.equal(h.posts.length, 1);
});

test("a new input or terminal snapshot closes the old form without a new run POST", async () => {
  for (const snapshot of [{ ...waiting, version: 42, pending_input: { ...input, version: 2 } },
    { ...waiting, version: 42, pending_input: { ...input, input_id: "second-input" } },
    { ...waiting, version: 42, status: "succeeded", pending_input: null }]) {
    const h = harness(); h.client.acceptSnapshot(snapshot); await h.client.submit(answer);
    assert.equal(h.posts.length, 0);
    assert.equal(h.states.at(-1).phase, "closed");
  }
});

test("foreign accepted IDs are rejected, never displayed as accepted", async () => {
  const h = harness();
  const pending = h.client.submit(answer); h.posts[0].resolve({ run_id: "foreign-run", status: "resume_queued" }); await pending;
  assert.notEqual(h.states.at(-1).phase, "accepted");
  assert.match(h.states.at(-1).error, /不一致/);
  assert.equal(h.received.length, 0);
});

test("authorization loss closes the form and ignores later GET/body data", async () => {
  const h = harness();
  const pending = h.client.submit(answer); h.posts[0].reject({ status: 404 }); await pending;
  assert.equal(h.states.at(-1).phase, "denied");
  assert.equal(h.states.at(-1).request, null);
  await h.client.submit(answer);
  assert.equal(h.posts.length, 1);
});

test("dispose ignores the delayed POST result and aborts only owned GET; never cancels the run", async () => {
  const h = harness();
  const pending = h.client.submit(answer); h.client.dispose();
  const count = h.states.length;
  h.posts[0].resolve({ run_id: waiting.run_id, status: "resume_queued" }); await pending;
  assert.equal(h.states.length, count); assert.equal(h.reads.length, 0);
  const other = harness(); const read = other.client.refresh(); other.client.dispose();
  assert.equal(other.reads[0].signal.aborted, true);
  other.reads[0].resolve({ ...waiting, version: 42 }); await read;
  assert.equal(other.received.length, 0); assert.equal(other.posts.length, 0);
});

test("proposal renders server values and escaped references without mutation controls", async () => {
  const filename = fileURLToPath(new URL("proposal_view.tsx", product));
  const require = createRequire(filename);
  const React = require("react"), { renderToStaticMarkup } = require("react-dom/server");
  // Existing Vite dependency: transform one TSX source in memory, no bundle/build/output files.
  const { transformSync } = await import(pathToFileURL(require.resolve("rolldown/utils")).href);
  const transformed = transformSync(filename, await readFile(filename, "utf8"), { lang: "tsx", jsx: { runtime: "automatic" } });
  assert.deepEqual(transformed.errors, []);
  const compiled = transformed.code.replaceAll('"react/jsx-runtime"', JSON.stringify(pathToFileURL(require.resolve("react/jsx-runtime")).href));
  const loaded = await import("data:text/javascript;base64," + Buffer.from(compiled).toString("base64"));
  const { tenant_id, ...canonical } = fixture.proposal_contract.canonical_example;
  const proposal = { ...canonical, proposal_id: "proposal-a", action_id: "action-a", proposal_seq: 1,
    canonical_args_hash: fixture.proposal_contract.canonical_sha256,
    evidence_refs: [{ evidence_id: "evidence-a", title: "<script>alert(1)</script>", source: "supportflow.knowledge", start_line: 1, end_line: 4, version_id: "doc-v1" }], observation_refs: [] };
  const before = JSON.stringify(proposal);
  const html = renderToStaticMarkup(React.createElement(loaded.ProposalView, { proposal }));
  assert.equal(JSON.stringify(proposal), before);
  for (const value of ["proposal-a", "action-a", "10", "3", canonical.created_at, canonical.expires_at, proposal.canonical_args_hash]) assert.ok(html.includes(value), value);
  // Approval now exists on its own scoped page. This shared proposal component
  // must still remain read-only; a historical "not implemented" banner is no
  // longer its contract. Keep the mutation/escaping checks below unchanged.
  assert.match(html, /只读/); assert.match(html, /本页不会批准、扩容或修改提案参数/);
  assert.equal(/<(button|input|form|script)\b/i.test(html), false);
  assert.ok(html.includes("&lt;script&gt;"));
  assert.equal(html.includes(tenant_id), false);
});

test("explicit refusal preserves one key on ambiguous delivery and never submits draft fields", async () => {
  const h = harness();
  const work = h.client.refuse("无法提供客户信息");
  assert.equal(h.client.refuse("无法提供客户信息"), work);
  assert.deepEqual(h.posts[0].body.answers, {});
  assert.equal(h.posts[0].body.refusal_reason, "无法提供客户信息");
  h.posts[0].reject(new Error("response lost")); await work;
  await h.client.submit(answer); await h.client.refuse("changed");
  assert.equal(h.posts.length, 1);
  const retry = h.client.refuse("无法提供客户信息");
  assert.equal(h.posts[1].body, h.posts[0].body);
  h.posts[1].resolve({run_id: waiting.run_id, status:"handoff"}); await retry;
  h.reads[0].resolve({...waiting, status:"handoff", version:42, last_event_seq:13, pending_input:null}); await tick();
  assert.equal(h.states.at(-1).phase, "closed"); h.client.dispose();
});
