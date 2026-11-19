import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";

const source = await readFile(new URL("../../apps/supportflow/web/product/citation_client.ts", import.meta.url), "utf8");
const { CitationClient } = await import("data:text/javascript;base64," + Buffer.from(
  stripTypeScriptTypes(source, { mode: "transform" })).toString("base64"));
const fixture = JSON.parse(await readFile(new URL("../../evals/fixtures/supportflow/rag/cases.json", import.meta.url), "utf8"));
const section = fixture.reviewed_sections[0];
const ref = { evidence_id: "citation-a", document_id: "doc-a", version_id: "version-a", section_id: section.section_id,
  generation: 1, title: "邀请排障概览", source: "supportflow.knowledge", start_line: section.start_line,
  end_line: section.end_line, content_sha256: section.content_sha256 };
const response = { ...ref, content: section.text, document_sha256: "a".repeat(64) };
function harness() {
  const requests = [], states = [];
  const client = new CitationClient((path, signal) => new Promise((resolve, reject) => {
    requests.push({ path, signal, resolve, reject });
  }), state => states.push(state));
  return { client, requests, states };
}

test("opening the same historical reference performs a fresh authorized GET each time", async () => {
  const { client, requests, states } = harness();
  let pending = client.open(ref);
  assert.equal(requests[0].path, "/citations/citation-a?generation=1");
  requests[0].resolve(response); await pending;
  assert.equal(states.at(-1).value.content, section.text);
  pending = client.open(ref);
  assert.equal(requests.length, 2);
  assert.equal(states.at(-1).value, null);
  requests[1].resolve(response); await pending;
  assert.equal(states.at(-1).value.version_id, ref.version_id);
});

test("revocation replaces previously displayed text with an unavailable result", async () => {
  const { client, requests, states } = harness();
  let pending = client.open(ref); requests[0].resolve(response); await pending;
  pending = client.open(ref); requests[1].reject({ status: 404 }); await pending;
  assert.equal(states.at(-1).value, null);
  assert.match(states.at(-1).error, /不可访问/);
  assert.equal(states.at(-1).loading, false);
});

test("closing cancels the owned GET and rejects its late response", async () => {
  const { client, requests, states } = harness();
  const pending = client.open(ref);
  client.close();
  const count = states.length;
  assert.equal(requests[0].signal.aborted, true);
  requests[0].resolve(response); await pending;
  assert.equal(states.length, count);
  assert.deepEqual(states.at(-1), { ref: null, loading: false, value: null, error: null });
});

test("switching citations cannot restore a prior run's delayed body", async () => {
  const { client, requests, states } = harness();
  const first = client.open(ref);
  const other = { ...ref, evidence_id: "citation-b", version_id: "version-b" };
  const second = client.open(other);
  assert.equal(requests[0].signal.aborted, true);
  requests[1].resolve({ ...response, ...other }); await second;
  requests[0].resolve(response); await first;
  assert.equal(states.at(-1).value.evidence_id, "citation-b");
});

test("a response bound to another document, version, span, hash or generation is rejected", async () => {
  for (const change of [{ document_id: "foreign" }, { version_id: "old" }, { generation: 2 },
    { start_line: 999 }, { content_sha256: "b".repeat(64) }, { title: "another heading" }]) {
    const { client, requests, states } = harness();
    const pending = client.open(ref);
    requests[0].resolve({ ...response, ...change }); await pending;
    assert.equal(states.at(-1).value, null);
    assert.match(states.at(-1).error, /不一致/);
  }
});
