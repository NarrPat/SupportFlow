import { useEffect, useRef, useState, type FormEvent } from "react";
import { createRoot } from "react-dom/client";
import { QueryClient, QueryClientProvider } from "@tanstack/react-query";
import { Button, ConfigProvider } from "antd";
import { SessionApi, describeError } from "./api";
import type { Actor } from "./contracts";
import { Tickets } from "./tickets";
import { Approvals } from "./approvals";
import { Evaluation } from "./evaluation";
import { Knowledge } from "./knowledge";
import { RunDetail, RunIndex } from "./run_detail";
import { Workspace } from "./workspace";
import "./style.css";

export function Brand() {
  return <span className="brand"><span className="brand-mark" aria-hidden="true"><i/><i/><i/></span>SupportFlow</span>;
}

const labels: Record<string, string> = { support: "客服", supervisor: "主管", admin: "管理员" };

const navigation = [
  ["/workspace", "工作台"], ["/tickets", "工单"], ["/knowledge", "知识"],
  ["/approvals", "审批"], ["/runs", "运行"], ["/evaluation", "评测"],
] as const;

function ProductView({ pathname, actor, api }: { pathname: string; actor: Actor; api: SessionApi }) {
  switch (pathname) {
    case "/workspace": return <Workspace api={api} actor={actor}/>;
    case "/tickets": return <Tickets api={api} actor={actor}/>;
    case "/knowledge": return <Knowledge api={api} actor={actor}/>;
    case "/approvals": return <Approvals api={api} actor={actor}/>;
    case "/runs": return <RunIndex api={api} actor={actor}/>;
    case "/evaluation": return <Evaluation api={api} actor={actor}/>;
  }
  const match = /^\/runs\/([^/]+)$/.exec(pathname);
  if (match) {
    try {
      const runId = decodeURIComponent(match[1]);
      if (runId && !runId.includes("/") && runId.length <= 128) {
        return <RunDetail key={runId} runId={runId} api={api} actor={actor}/>;
      }
    } catch {
      // A malformed URL is handled by the same navigation fallback.
    }
  }
  return <main className="wrap route-missing"><h1>页面未找到</h1><a href="/workspace">返回工作台</a></main>;
}

function Session({ restart, channel, message }: {
  restart: (notice?: string) => void; channel: BroadcastChannel | null; message: string;
}) {
  const [cache] = useState(() => new QueryClient({ defaultOptions: { queries: { retry: false } } }));
  const [api] = useState(() => new SessionApi(() => restart("登录已过期，请重新登录。")));
  const [actor, setActor] = useState<Actor | null>();
  const [error, setError] = useState("");
  const [busy, setBusy] = useState(false);
  const alive = useRef(true);
  useEffect(() => {
    api.restore().then(value => { if (alive.current) setActor(value); })
      .catch(err => { if (alive.current) setError(describeError(err)); });
    return () => { alive.current = false; api.dispose(); cache.clear(); };
  }, [api, cache]);

  async function login(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (busy) return;
    const form = new FormData(event.currentTarget);
    setBusy(true); setError("");
    try {
      const user = await api.login(String(form.get("username")).trim(), String(form.get("password")));
      if (alive.current) { setActor(user); channel?.postMessage("session-changed"); }
    } catch (err) { if (alive.current) setError(describeError(err)); }
    finally { if (alive.current) setBusy(false); }
  }
  async function logout() {
    if (busy) return;
    setBusy(true); setError("");
    try {
      await api.post("/auth/logout", {});
      if (!alive.current) return;
      history.replaceState(null, "", "/tickets");
      channel?.postMessage("session-changed");
      restart();
    } catch (err) { if (alive.current) { setError(describeError(err)); setBusy(false); } }
  }

  if (actor === undefined) return <main className="loading-page"><Brand/>{error
    ? <><p role="alert">{error}</p><button onClick={() => restart()}>重新连接</button></>
    : <p role="status">正在检查会话…</p>}</main>;
  if (actor === null) return <main className="signin">
    <div className="signin-card">
      <Brand/>
      <h1>登录客服工作台</h1>
      <p className="intro">使用客服账号查看客户工单与关联会话。</p>
      <form onSubmit={login}>
        <label>用户名<input required name="username" autoComplete="username" autoFocus /></label>
        <label>密码<input required name="password" type="password" autoComplete="current-password" /></label>
        {error ? <p className="error" role="alert">{error}</p> : message && <p role="status">{message}</p>}
        <Button htmlType="submit" type="primary" loading={busy} size="large" block>登录</Button>
      </form>
    </div>
  </main>;
  const pathname = location.pathname.replace(/\/$/, "") || "/tickets";
  return <QueryClientProvider client={cache}>
    <header className="topbar"><div className="topbar-inner">
      <Brand/><span className="top-separator" aria-hidden="true"/><span className="top-section">客服工作台</span>
      <div className="account"><span className="actor-name">{actor.username}</span><span className="role">{labels[actor.role] ?? actor.role}</span>
        <button className="logout" disabled={busy} onClick={logout}>退出登录</button></div>
    </div></header>
    {error && <p role="alert" className="error wrap">{error}</p>}
    <nav className="workspace-nav wrap" aria-label="工作台导航">
      {navigation.map(([href, label]) => <a key={href} href={href}
        aria-current={pathname === href || (href === "/runs" && pathname.startsWith("/runs/")) ? "page" : undefined}>{label}</a>)}
    </nav>
    <ProductView pathname={pathname} api={api} actor={actor}/>
  </QueryClientProvider>;
}

function App() {
  const [session, setSession] = useState({ revision: 0, notice: "" });
  const [channel] = useState(() => typeof BroadcastChannel === "undefined" ? null : new BroadcastChannel("supportflow-session"));
  function restart(notice = "") {
    setSession(old => ({ revision: old.revision + 1, notice }));
  }
  useEffect(() => {
    const change = () => restart();
    const show = (event: PageTransitionEvent) => { if (event.persisted) restart(); };
    channel?.addEventListener("message", change);
    window.addEventListener("pageshow", show);
    return () => { channel?.removeEventListener("message", change); window.removeEventListener("pageshow", show); };
  }, [channel]);
  return <ConfigProvider button={{ autoInsertSpace: false }} theme={{ token: { colorPrimary: "#126356", borderRadius: 7, controlHeight: 38,
    fontFamily: 'Inter, "Segoe UI", "Microsoft YaHei", sans-serif' } }}>
    <Session key={session.revision} restart={restart} channel={channel} message={session.notice}/>
  </ConfigProvider>;
}

createRoot(document.getElementById("root")!).render(<App/>);
