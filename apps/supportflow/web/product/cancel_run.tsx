import { useEffect, useRef, useState } from "react";
import type { CancelInput, CancelResult, RunDetail } from "./contracts";
import { ApiError, describeError, SessionApi } from "./api";

export function CancelRun({ api, run, canCreate, onSnapshot }: {
  api: SessionApi; run: RunDetail; canCreate: boolean; onSnapshot: (run: RunDetail) => void;
}) {
  const pending = useRef<{ runId: string; body: CancelInput } | null>(null);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);
  useEffect(() => { pending.current = null; setError(null); }, [run.run_id]);
  const terminal = ["succeeded", "handoff", "rejected", "expired", "failed", "cancelled"].includes(run.status);
  async function cancel() {
    if (busy) return;
    const request = pending.current?.runId === run.run_id ? pending.current : {
      runId: run.run_id, body: { expected_version: run.version, client_request_id: crypto.randomUUID() },
    };
    pending.current = request;
    setBusy(true); setError(null);
    try {
      const snapshot = await api.post<CancelResult>(`/runs/${encodeURIComponent(request.runId)}/cancel`, request.body);
      pending.current = null;
      onSnapshot(snapshot);
    } catch (cause) {
      if (cause instanceof ApiError && cause.status === 409) {
        // A definite conflict was not accepted; refresh before a new request.
        pending.current = null;
        setError("运行状态已更新，请刷新后再取消。");
      } else {
        setError(cause instanceof ApiError ? describeError(cause) : "尚未确认取消请求是否受理。重试会沿用原请求键。");
      }
    } finally { setBusy(false); }
  }
  return <section aria-label="取消请求">
    {run.cancel_requested && !terminal && <p role="note">已请求取消，等待后台安全点确认。已发出的授予仍需核对实际结果。</p>}
    {canCreate && !terminal && !run.cancel_requested && <button type="button" disabled={busy}
      aria-busy={busy} onClick={() => { void cancel(); }}>{busy ? "正在请求取消…" : "请求取消"}</button>}
    {error && <p className="error" role="alert">{error}</p>}
  </section>;
}
