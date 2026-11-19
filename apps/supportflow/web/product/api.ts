import type { Actor, CsrfToken, LoginInput, LoginResult } from "./contracts";

export class ApiError extends Error {
  constructor(public status: number, public code: string, public requestId?: string) {
    super(code);
  }
}

async function request<T>(path: string, init: RequestInit, signal: AbortSignal): Promise<T> {
  const response = await fetch("/api/v1" + path, {
    ...init, credentials: "same-origin", cache: "no-store", signal,
  });
  if (!response.ok) {
    const error = await response.json().catch(() => ({}));
    throw new ApiError(response.status, error.code ?? "HTTP_ERROR", error.request_id);
  }
  return response.status === 204 ? undefined as T : response.json() as Promise<T>;
}

export function describeError(error: unknown): string {
  if (!(error instanceof ApiError)) return "连接失败，请检查网络后重试。";
  const messages: Record<number, string> = {
    401: "登录已过期，请重新登录。", 403: "当前账号没有操作权限。",
    404: "工单不可见或已移除。", 409: "记录已变更，请刷新后再试。",
    422: "请检查填写的内容。", 429: "请求过于频繁，请稍后重试。",
  };
  const message = error.code === "INVALID_CREDENTIALS" ? "用户名或密码错误。" :
    error.code === "CSRF_FAILED" ? "会话校验已失效，请刷新页面后重试。" :
    messages[error.status] ?? "服务暂时不可用，请稍后重试。";
  return error.requestId ? message + " 请求编号 " + error.requestId : message;
}

// Each signed-in workspace owns its own lifetime, token and query cache.
export class SessionApi {
  private token = "";
  private controller = new AbortController();
  constructor(private expired: () => void) {}
  dispose() { this.controller.abort(); this.token = ""; }
  get<T>(path: string, signal?: AbortSignal): Promise<T> {
    const session = this.controller.signal;
    return this.guard(request<T>(path, {}, signal ? AbortSignal.any([session, signal]) : session));
  }
  post<T>(path: string, body: unknown): Promise<T> {
    return this.guard(request<T>(path, this.json(body), this.controller.signal));
  }
  upload<T>(path: string, body: FormData, key: string): Promise<T> {
    return this.guard(request<T>(path, { method: "POST", body,
      headers: { "X-CSRF-Token": this.token, "Idempotency-Key": key } }, this.controller.signal));
  }
  private json(body: unknown): RequestInit {
    return { method: "POST", headers: { "Content-Type": "application/json", "X-CSRF-Token": this.token },
      body: JSON.stringify(body) };
  }
  private async guard<T>(result: Promise<T>): Promise<T> {
    try { return await result; }
    catch (error) {
      if (!this.controller.signal.aborted && error instanceof ApiError && error.status === 401) this.expired();
      throw error;
    }
  }
  async restore(): Promise<Actor | null> {
    const signal = this.controller.signal;
    this.token = (await request<CsrfToken>("/auth/csrf", {}, signal)).csrf_token;
    try { return await request<Actor>("/me", {}, signal); }
    catch (error) {
      if (error instanceof ApiError && error.status === 401) return null;
      throw error;
    }
  }
  async login(username: string, password: string): Promise<Actor> {
    const signal = this.controller.signal;
    // Another tab can receive a new anonymous cookie while this form is open.
    this.token = (await request<CsrfToken>("/auth/csrf", {}, signal)).csrf_token;
    const credentials: LoginInput = { username, password };
    this.token = (await request<LoginResult>("/auth/login", this.json(credentials), signal)).csrf_token;
    return this.get<Actor>("/me");
  }
}
