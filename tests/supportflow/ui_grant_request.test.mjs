import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";
import { stripTypeScriptTypes } from "node:module";
import test from "node:test";

const source = await readFile(new URL("../../apps/supportflow/web/product/run_request.ts", import.meta.url), "utf8");
const { emptyGrant, runFields, requestFor } = await import("data:text/javascript;base64," +
  Buffer.from(stripTypeScriptTypes(source, { mode: "transform" })).toString("base64"));

test("legacy and unchecked forms omit all action fields", () => {
  assert.deepEqual(runFields("只查询订阅", emptyGrant()), { ok: true, fields: { message: "只查询订阅" } });
  assert.deepEqual(runFields("只查询订阅", { requested: false, seats: "10", days: "3", invitation_email: "member@example.com" }),
    { ok: true, fields: { message: "只查询订阅" } });
});
test("explicit request binds exact numbers and normalized email; unknowns stay omitted", () => {
  assert.deepEqual(runFields("申请", { requested: true, seats: "10", days: "3", invitation_email: "Member@Example.com" }),
    { ok: true, fields: { message: "申请", requested_action: "grant_temporary_seats", seats: 10, days: 3, invitation_email: "member@example.com" } });
  assert.deepEqual(runFields("申请", { ...emptyGrant(), requested: true }),
    { ok: true, fields: { message: "申请", requested_action: "grant_temporary_seats" } });
});
test("numeric drafts do not coerce decimals/exponents/signs or accept invalid ASCII email", () => {
  for (const value of ["1.0", "1e1", "+1", "0", "21", " 1 ", "true"])
    assert.equal(runFields("申请", { ...emptyGrant(), requested: true, seats: value }).ok, false, value);
  for (const value of ["成员@example.com", "a@bad_domain.com", ".a@example.com", "a..b@example.com"])
    assert.equal(runFields("申请", { ...emptyGrant(), requested: true, invitation_email: value }).ok, false, value);
});
test("lost responses retain exact request; editing any action field changes its key", () => {
  let keys = 0;
  const key = () => `key-${++keys}`;
  const fields = runFields("申请", { requested: true, seats: "10", days: "3", invitation_email: "member@example.com" }).fields;
  const original = requestFor(fields, null, key);
  assert.equal(requestFor(fields, original, key), original);
  assert.equal(keys, 1);
  for (const change of [{ seats: 11 }, { days: 4 }, { invitation_email: "other@example.com" }])
    assert.notEqual(requestFor({ ...fields, ...change }, original, key).client_request_id, original.client_request_id);
  assert.notEqual(requestFor({ message: "申请" }, original, key).client_request_id, original.client_request_id);
});
