import { useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import { Button } from "antd";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import type { Actor, CustomersPage, Thread, ThreadInput, Ticket, TicketDetail, TicketInput, TicketsPage } from "./contracts";
import { describeError, SessionApi } from "./api";
import { Runs } from "./runs";

type Customer = CustomersPage["items"][number];
const pageSize = 10;
const ticketQuery = (id: string) => ["ticket", id];
const locationTicket = () => new URL(location.href).searchParams.get("ticket");
const statusText = (status: string) => status === "open" ? "待处理" : status;
const customerName = (customers: Customer[], id?: string | null) =>
  id ? (customers.find(item => item.customer_id === id)?.display_name ?? id) : "未关联客户";

export function ReadError({ error, retry }: { error: unknown; retry: () => void }) {
  return <div className="error-box"><p role="alert">{describeError(error)}</p><button onClick={retry}>重新加载</button></div>;
}

function Dialog({ name, onClose, busy = false, children, drawer = false }: {
  name: string; onClose: () => void; busy?: boolean; children: ReactNode; drawer?: boolean;
}) {
  const ref = useRef<HTMLDialogElement>(null);
  useEffect(() => {
    const dialog = ref.current!;
    dialog.showModal();
    return () => dialog.close();
  }, []);
  return <dialog ref={ref} className={drawer ? "drawer" : "modal"} aria-label={name}
    onCancel={event => { event.preventDefault(); if (!busy) onClose(); }}>
    <div className="dialog-top"><span>{name}</span><button type="button" aria-label="关闭" disabled={busy}
      className="close-button" onClick={onClose}>×</button></div>
    {children}
  </dialog>;
}

function NewTicket({ api, customers, initialCustomer, onCreated, onClose, moreCustomers }: {
  api: SessionApi; customers: Customer[]; initialCustomer: string; moreCustomers: ReactNode;
  onCreated: (ticket: Ticket) => void; onClose: () => void;
}) {
  const [title, setTitle] = useState("");
  const [customer, setCustomer] = useState(initialCustomer);
  const create = useMutation({
    mutationFn: (body: TicketInput) => api.post<Ticket>("/tickets", body),
  });
  function submit(event: FormEvent) {
    event.preventDefault();
    if (!title.trim() || create.isPending) return;
    create.mutate({ title: title.trim(), customer_id: customer || null }, { onSuccess: onCreated });
  }
  return <Dialog name="新建工单" onClose={onClose} busy={create.isPending}>
    <form className="dialog-body new-form" onSubmit={submit}>
      <label>工单标题<input required maxLength={200} autoFocus value={title} onChange={e => setTitle(e.target.value)} /></label>
      <label>关联客户<select aria-label="关联客户" value={customer} onChange={e => setCustomer(e.target.value)}>
        <option value="">暂不关联</option>
        {customers.map(c => <option key={c.customer_id} value={c.customer_id}>{c.display_name} · {c.customer_code}</option>)}
      </select></label>
      {moreCustomers}
      {create.isError && <p role="alert" className="error">{describeError(create.error)}</p>}
      <div className="actions"><button type="button" disabled={create.isPending} onClick={onClose}>取消</button>
        <Button type="primary" htmlType="submit" disabled={!title.trim()} loading={create.isPending}>创建工单</Button></div>
    </form>
  </Dialog>;
}

function Detail({ api, actorId, id, customers, moreCustomers, canCreateThread, canCreateRun, onClose }: {
  api: SessionApi; actorId: string; id: string; customers: Customer[]; moreCustomers: ReactNode;
  canCreateThread: boolean; canCreateRun: boolean; onClose: () => void;
}) {
  const cache = useQueryClient();
  const [firstCustomer, setFirstCustomer] = useState("");
  const detail = useQuery({ queryKey: ticketQuery(id),
    queryFn: ({ signal }) => api.get<TicketDetail>("/tickets/" + encodeURIComponent(id), signal) });
  const create = useMutation({
    mutationFn: (body: ThreadInput) => api.post<Thread>("/threads", body),
    onSuccess: async () => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: ticketQuery(id) }),
        cache.invalidateQueries({ queryKey: ["tickets"] }),
      ]);
    },
  });
  const ticket = detail.data;
  return <Dialog name="工单详情" onClose={onClose} drawer busy={create.isPending}>
    {detail.isPending ? <p className="dialog-body" role="status">正在加载工单…</p> :
      detail.isError ? <ReadError error={detail.error} retry={() => void detail.refetch()}/> :
      ticket && <div className="dialog-body">
        <span className="status"><i/>{statusText(ticket.status)}</span>
        <h2>{ticket.title}</h2>
        <dl className="metadata">
          <div><dt>工单编号</dt><dd className="mono">{ticket.ticket_id}</dd></div>
          <div><dt>客户</dt><dd>{customerName(customers, ticket.customer_id)}</dd></div>
          <div><dt>负责人</dt><dd>{ticket.assignee_id ?? "未分配"}</dd></div>
          <div><dt>版本</dt><dd>{ticket.version}</dd></div>
        </dl>
        <a href={`/workspace?ticket=${encodeURIComponent(ticket.ticket_id)}`}>在工作台继续</a>
        <section className="threads"><h3>关联会话</h3>
          {ticket.latest_thread_id
            ? <><p>最近关联的会话编号</p><code aria-label="最近关联会话 ID">{ticket.latest_thread_id}</code></>
            : <p>当前账号尚未在此工单创建会话。</p>}
          {canCreateThread && <>
            {!ticket.customer_id && <div className="first-binding">
              <label>首次确认客户<select value={firstCustomer} disabled={create.isPending} onChange={e => setFirstCustomer(e.target.value)}>
                <option value="">暂不绑定</option>
                {customers.map(c => <option key={c.customer_id} value={c.customer_id}>{c.display_name} · {c.customer_code}</option>)}
              </select></label>
              <p>创建会话时绑定所选客户，此工单之后不能改绑。</p>
              {moreCustomers}
            </div>}
            <Button loading={create.isPending} onClick={() => create.mutate({
              ticket_id: id, customer_id: ticket.customer_id ? null : firstCustomer || null,
            })}>创建关联会话</Button>
          </>}
          {create.isError && <p role="alert" className="error">{describeError(create.error)}</p>}
        </section>
        {ticket.latest_thread_id && <Runs key={ticket.latest_thread_id} api={api} actorId={actorId}
          threadId={ticket.latest_thread_id} canCreate={canCreateRun}/>}
      </div>}
  </Dialog>;
}

export function Tickets({ actor, api }: { actor: Actor; api: SessionApi }) {
  const cache = useQueryClient();
  const [filter, setFilter] = useState("");
  const [offset, setOffset] = useState(0);
  const [selected, setSelected] = useState(locationTicket);
  const [creating, setCreating] = useState(false);
  useEffect(() => {
    const pop = () => { setSelected(locationTicket()); setCreating(false); };
    window.addEventListener("popstate", pop);
    return () => window.removeEventListener("popstate", pop);
  }, []);
  function showTicket(id: string | null) {
    history.pushState(null, "", id ? "/tickets?ticket=" + encodeURIComponent(id) : "/tickets");
    setSelected(id);
  }
  function changeFilter(value: string) { setFilter(value); setOffset(0); }
  const customers = useInfiniteQuery({
    queryKey: ["customers"], initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => api.get<CustomersPage>("/customers?limit=50&offset=" + pageParam, signal),
    getNextPageParam: page => page.has_more ? page.offset + page.limit : undefined,
  });
  const customerList = customers.data?.pages.flatMap(page => page.items) ?? [];
  const moreCustomers = customers.hasNextPage ? <button type="button" disabled={customers.isFetchingNextPage}
    onClick={() => void customers.fetchNextPage()}>加载更多客户</button> : null;
  const tickets = useQuery({ queryKey: ["tickets", filter, offset], queryFn: ({ signal }) => {
    const params = new URLSearchParams({ limit: String(pageSize), offset: String(offset) });
    if (filter) params.set("customer_id", filter);
    return api.get<TicketsPage>("/tickets?" + params, signal);
  } });
  const rows = tickets.data?.items ?? [];
  return <main className="wrap tickets-page">
    <div className="page-heading"><div><p className="breadcrumb">客服工作台<span aria-hidden="true"> / </span>工单</p>
      <h1>工单</h1></div>
      {actor.capabilities.includes("tickets:create") && <Button type="primary" size="large" onClick={() => setCreating(true)}>
        <span aria-hidden="true" className="plus">+</span>新建工单</Button>}
    </div>
    <section className="ticket-panel" aria-label="工单列表">
      <div className="toolbar"><label className="filter">筛选客户<select value={filter} onChange={e => changeFilter(e.target.value)}>
        <option value="">全部可见客户</option>
        {customerList.map(c => <option key={c.customer_id} value={c.customer_id}>{c.display_name} · {c.customer_code}</option>)}
      </select></label>{moreCustomers}</div>
      {customers.isError && <ReadError error={customers.error} retry={() => void customers.refetch()}/>}
      {tickets.isPending ? <p role="status" className="empty">正在读取工单…</p> :
        tickets.isError ? <ReadError error={tickets.error} retry={() => void tickets.refetch()}/> :
        rows.length === 0 ? <div className="empty"><h2>暂无工单</h2><p>{filter ? "当前客户没有可见工单。" : "当前账号没有可见工单。"}</p></div> :
        <table><thead><tr><th scope="col">工单标题</th><th scope="col">客户</th><th scope="col">状态</th></tr></thead>
          <tbody>{rows.map(t => <tr key={t.ticket_id}><td><button className="ticket-link" onClick={() => showTicket(t.ticket_id)}>{t.title}</button>
              <small className="record-id">{t.ticket_id}</small></td><td>{customerName(customerList, t.customer_id)}</td>
            <td><span className="status"><i/>{statusText(t.status)}</span></td></tr>)}</tbody></table>}
      <div className="pagination"><span>第 {Math.floor(offset / pageSize) + 1} 页</span>
        <button disabled={!offset} onClick={() => setOffset(offset - pageSize)}>上一页</button>
        <button disabled={!tickets.data?.has_more} onClick={() => setOffset(offset + pageSize)}>下一页</button></div>
    </section>
    {creating && <NewTicket api={api} customers={customerList} initialCustomer={filter} moreCustomers={moreCustomers}
      onClose={() => setCreating(false)} onCreated={ticket => {
        setCreating(false); changeFilter(ticket.customer_id ?? "");
        void cache.invalidateQueries({ queryKey: ["tickets"] }); showTicket(ticket.ticket_id);
      }}/>}
    {selected && <Detail key={selected} api={api} actorId={actor.actor_id} id={selected} customers={customerList} moreCustomers={moreCustomers}
      canCreateThread={actor.capabilities.includes("threads:create")} canCreateRun={actor.capabilities.includes("runs:create")}
      onClose={() => showTicket(null)}/>}
  </main>;
}
