import type { RunInput } from "./contracts";

export type GrantDraft = { requested: boolean; seats: string; days: string; invitation_email: string };
export type RunFields = { message: string; requested_action?: "grant_temporary_seats";
  seats?: number; days?: number; invitation_email?: string };
export const emptyGrant = (): GrantDraft => ({ requested: false, seats: "", days: "", invitation_email: "" });

/** Empty optional fields are omitted for necessary clarification, never guessed. */
export function runFields(message: string, draft: GrantDraft):
  { ok: true; fields: RunFields } | { ok: false; message: string } {
  if (!message.trim() || Array.from(message).length > 8000) return { ok: false, message: "请填写 1–8000 字的请求内容。" };
  if (!draft.requested) return { ok: true, fields: { message } };
  const fields: RunFields = { message, requested_action: "grant_temporary_seats" };
  for (const name of ["seats", "days"] as const) {
    const text = draft[name];
    if (text === "") continue;
    const value = Number(text);
    if (!/^[1-9][0-9]*$/.test(text) || !Number.isSafeInteger(value) || value > (name === "seats" ? 20 : 7))
      return { ok: false, message: "席位数须为 1–20 的整数，天数须为 1–7 的整数；未知时可留空后补充。" };
    fields[name] = value;
  }
  if (draft.invitation_email !== "") {
    const email = draft.invitation_email.trim().toLowerCase();
    const parts = email.split("@");
    const local = parts[0] ?? "", domain = parts[1] ?? "";
    if (!/^[\x00-\x7f]+$/.test(email) || email.length > 254 || parts.length !== 2 ||
        !/^[a-z0-9.!#$%&'*+/=?^_`{|}~-]{1,64}$/.test(local) || local.startsWith(".") || local.endsWith(".") || local.includes("..") ||
        !domain || domain.length > 253 || !domain.split(".").every(label => /^[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?$/.test(label)))
      return { ok: false, message: "请填写有效的 ASCII 邀请邮箱，或留空后补充。" };
    fields.invitation_email = email;
  }
  return { ok: true, fields };
}

/** The complete structured request participates in retry identity. */
export function requestFor(fields: RunFields, previous: RunInput | null, key: () => string): RunInput {
  if (previous) {
    const { client_request_id: _key, ...priorFields } = previous;
    if (JSON.stringify(priorFields) === JSON.stringify(fields)) return previous;
  }
  return { ...fields, client_request_id: key() };
}
