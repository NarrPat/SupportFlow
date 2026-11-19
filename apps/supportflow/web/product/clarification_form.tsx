import { useEffect, useRef, useState, type FormEvent } from "react";
import { Button } from "antd";
import { useInfiniteQuery } from "@tanstack/react-query";
import type { CustomersPage, RunAccepted, RunDetail } from "./contracts";
import { describeError, SessionApi } from "./api";
import { answersFromDraft, ClarificationClient, initialClarification, type AnswerDraft, type AnswerField } from "./clarification_client";

const labels: Record<AnswerField, string> = { customer_id: "本次核查客户", invitation_email: "邀请邮箱",
  seats: "临时席位数（1–20）", days: "临时席位天数（1–7）" };

export function ClarificationForm({ api, actorId, run, canSubmit, onSnapshot }: {
  api: SessionApi; actorId: string; run: RunDetail; canSubmit: boolean; onSnapshot(snapshot: RunDetail): void;
}) {
  const [refusal, setRefusal] = useState("");
  const [draft, setDraft] = useState<AnswerDraft>({});
  const [state, setState] = useState(() => initialClarification(run));
  const [validation, setValidation] = useState<string | null>(null);
  const client = useRef<ClarificationClient | null>(null);
  const receive = useRef(onSnapshot);
  receive.current = onSnapshot;
  const input = run.pending_input;
  useEffect(() => {
    const current = new ClarificationClient(run, {
      key: () => crypto.randomUUID(),
      post: (path, body) => api.post<RunAccepted>(path, body),
      snapshot: (path, signal) => api.get<RunDetail>(path, signal),
      received: snapshot => receive.current(snapshot),
    }, setState);
    client.current = current;
    setState(initialClarification(run));
    return () => { current.dispose(); if (client.current === current) client.current = null; };
    // The component is keyed by actor/run/input/version; ordinary snapshots do not reset a retained request.
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [api, actorId, run.run_id, input?.input_id, input?.version, canSubmit]);
  useEffect(() => { client.current?.acceptSnapshot(run); }, [run]);
  const needsCustomer = input?.required_fields.includes("customer_id") ?? false;
  const customers = useInfiniteQuery({
    queryKey: ["supportflow", actorId, "clarification-customers"], initialPageParam: 0,
    enabled: canSubmit && needsCustomer && run.status === "awaiting_input" && state.phase !== "denied",
    queryFn: ({ pageParam, signal }) => api.get<CustomersPage>(`/customers?limit=50&offset=${pageParam}`, signal),
    getNextPageParam: page => page.has_more ? page.offset + page.limit : undefined,
  });
  const options = customers.data?.pages.flatMap(page => page.items) ?? [];
  if (state.phase === "denied") return <p className="error" role="alert">{state.error}</p>;
  if (!input || run.status !== "awaiting_input" || state.phase === "closed") return null;
  const parsed = answersFromDraft(input.required_fields, draft);
  const busy = state.phase === "submitting";
  const blocked = ["submitting", "conflict", "accepted"].includes(state.phase);
  const retainedRetry = state.phase === "error" && state.request !== null;
  const customerReady = !needsCustomer || (options.some(option => option.customer_id === draft.customer_id) && !customers.isError);
  const editingDisabled = blocked || state.ambiguous;
  const label = retainedRetry ? "重试补充信息" : "提交补充信息";
  function edit(field: AnswerField, value: string) {
    if (!client.current?.edit()) return;
    setDraft(previous => ({ ...previous, [field]: value })); setValidation(null);
  }
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!canSubmit || blocked) return;
    if (!parsed.ok) { setValidation(parsed.message); return; }
    if (!retainedRetry && !customerReady) { setValidation("请从当前可见客户列表中选择客户。"); return; }
    setValidation(null); void client.current?.submit(parsed.answers);
  }
  return <section aria-label="补充必要信息">
    <h4>等待补充信息</h4>
    <p>{input.prompt}</p>
    <p>等待截止：<time dateTime={input.expires_at}>{input.expires_at}</time>。有效性以服务端核验为准。</p>
    <p>提交后继续当前运行，不会新建请求。只补充下列要求的字段。</p>
    {!canSubmit ? <p role="note">当前账号不能提交补充信息。</p> : <form onSubmit={submit}>
      <fieldset disabled={editingDisabled}>
        <legend>本次需要的信息</legend>
        {input.required_fields.map(field => field === "customer_id" ? <label key={field}>{labels.customer_id}
          <select aria-label={labels.customer_id} value={draft.customer_id ?? ""} required
            onChange={event => edit("customer_id", event.target.value)}>
            <option value="">请选择客户</option>
            {options.map(customer => <option key={customer.customer_id} value={customer.customer_id}>
              {customer.display_name} · {customer.customer_code}
            </option>)}
          </select>
        </label> : field === "invitation_email" ? <label key={field}>{labels.invitation_email}
          <input type="email" required maxLength={254} value={draft.invitation_email ?? ""}
            onChange={event => edit("invitation_email", event.target.value)} />
        </label> : (field === "seats" || field === "days") ? <label key={field}>{labels[field]}
          <input type="text" inputMode="numeric" required pattern="[1-9][0-9]*" maxLength={2}
            value={draft[field] ?? ""} onChange={event => edit(field, event.target.value)} />
        </label> : <p key={field} role="alert">当前补充字段暂不支持，请重新读取运行记录。</p>)}
      </fieldset>
      {needsCustomer && <>
        {customers.isPending && <p role="status">正在读取可见客户…</p>}
        {customers.isError && <p className="error" role="alert">{describeError(customers.error)}
          <button type="button" onClick={() => void customers.refetch()}>重新读取客户</button></p>}
        {customers.hasNextPage && <button type="button" disabled={customers.isFetchingNextPage || editingDisabled}
          onClick={() => void customers.fetchNextPage()}>加载更多可见客户</button>}
      </>}
      {validation && <p className="error" role="alert">{validation}</p>}
      {state.error && <p className="error" role="alert">{state.error}</p>}
      {state.phase === "accepted" && <p role="status">补充信息已受理，正在读取同一运行的后续状态。</p>}
      <Button htmlType="submit" type="primary" loading={busy} aria-label={label} aria-busy={busy}
        disabled={blocked || !parsed.ok || (!retainedRetry && !customerReady)}>{label}</Button>
      <label>无法提供信息的原因
        <input aria-label="无法提供信息的原因" value={refusal} maxLength={500} disabled={editingDisabled}
          onChange={event => { if (client.current?.edit()) setRefusal(event.target.value); }} />
      </label>
      <Button htmlType="button" disabled={blocked || !refusal.trim() || (state.ambiguous && state.request?.refusal_reason !== refusal)}
        onClick={() => void client.current?.refuse(refusal)}>无法提供，转人工</Button>
      <button type="button" disabled={state.refreshing || busy} onClick={() => void client.current?.refresh()}>
        重新读取运行状态</button>
    </form>}
  </section>;
}
