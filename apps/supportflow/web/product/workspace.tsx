import { useEffect, useRef, useState } from "react";
import { Button } from "antd";
import { useInfiniteQuery, useMutation, useQuery, useQueryClient } from "@tanstack/react-query";
import { describeError, type SessionApi } from "./api";
import type { Actor, CustomersPage, Thread, ThreadInput, TicketDetail, TicketsPage } from "./contracts";
import { Runs } from "./runs";
import { ReadError } from "./tickets";
import "./workbench.css";

const pageSize = 25;
const locationTicket = () => new URL(window.location.href).searchParams.get("ticket") ?? "";
const selectedTicketKey = (actorId: string, ticketId: string) => ["workspace", actorId, "ticket", ticketId];
const ticketPath = (ticketId: string) => `/tickets/${encodeURIComponent(ticketId)}`;
const nextOffset = (page: { has_more: boolean; limit: number; offset: number }) =>
  page.has_more ? page.offset + page.limit : undefined;

function WorkspaceTicket({ api, actor, ticket }: { api: SessionApi; actor: Actor; ticket: TicketDetail }) {
  const cache = useQueryClient();
  const creating = useRef(false);
  const create = useMutation({
    mutationFn: (body: ThreadInput) => api.post<Thread>("/threads", body),
    onSuccess: async () => {
      await Promise.all([
        cache.invalidateQueries({ queryKey: selectedTicketKey(actor.actor_id, ticket.ticket_id) }),
        cache.invalidateQueries({ queryKey: ["ticket", ticket.ticket_id] }),
      ]);
    },
    onSettled: () => { creating.current = false; },
  });
  function createThread() {
    if (creating.current) return;
    creating.current = true;
    create.mutate({ ticket_id: ticket.ticket_id, customer_id: null });
  }
  return <section className="ticket-panel workbench-panel" aria-label="所选工单">
    <div className="section-heading">
      <h2>{ticket.title}</h2>
      <a href={`/tickets?ticket=${encodeURIComponent(ticket.ticket_id)}`}>打开工单</a>
    </div>
    <dl className="metadata">
      <div><dt>工单编号</dt><dd className="mono">{ticket.ticket_id}</dd></div>
      {ticket.latest_thread_id && <div><dt>关联会话</dt><dd className="mono" aria-label="最近关联会话 ID">{ticket.latest_thread_id}</dd></div>}
    </dl>
    {ticket.latest_thread_id ? <Runs key={ticket.latest_thread_id} api={api} actorId={actor.actor_id}
      threadId={ticket.latest_thread_id} canCreate={actor.capabilities.includes("runs:create")}/> :
      <div className="workbench-thread-empty">
        <p>当前账号尚未在此工单创建会话。</p>
        {actor.capabilities.includes("threads:create") && <Button type="primary" loading={create.isPending}
          onClick={createThread}>创建关联会话</Button>}
        {create.isError && <p className="error" role="alert">{describeError(create.error)}</p>}
      </div>}
  </section>;
}

export function Workspace({ api, actor }: { api: SessionApi; actor: Actor }) {
  const [ticketId, setTicketId] = useState(locationTicket);
  const [selectedCustomer, setCustomer] = useState("");
  useEffect(() => {
    const restore = () => { setTicketId(locationTicket()); setCustomer(""); };
    window.addEventListener("popstate", restore);
    return () => window.removeEventListener("popstate", restore);
  }, []);
  function selectTicket(id: string) {
    history.pushState(null, "", id ? `/workspace?ticket=${encodeURIComponent(id)}` : "/workspace");
    setTicketId(id);
  }
  const customers = useInfiniteQuery({
    queryKey: ["workspace", actor.actor_id, "customers"], initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => api.get<CustomersPage>(`/customers?limit=${pageSize}&offset=${pageParam}`, signal),
    getNextPageParam: nextOffset,
  });
  const detail = useQuery({
    queryKey: selectedTicketKey(actor.actor_id, ticketId), enabled: Boolean(ticketId),
    queryFn: ({ signal }) => api.get<TicketDetail>(ticketPath(ticketId), signal),
  });
  const ticket = detail.isFetchedAfterMount && !detail.isError && detail.data?.ticket_id === ticketId
    ? detail.data : undefined;
  const customerId = ticket?.customer_id ?? selectedCustomer;
  const tickets = useInfiniteQuery({
    queryKey: ["workspace", actor.actor_id, "tickets", customerId], enabled: Boolean(customerId), initialPageParam: 0,
    queryFn: ({ pageParam, signal }) => api.get<TicketsPage>(
      `/tickets?customer_id=${encodeURIComponent(customerId)}&limit=${pageSize}&offset=${pageParam}`, signal),
    getNextPageParam: nextOffset,
  });
  const customerOptions = customers.data?.pages.flatMap(page => page.items) ?? [];
  const selectedCustomerOutsidePage = Boolean(customerId && !customerOptions.some(customer => customer.customer_id === customerId));
  const ticketOptions = tickets.isError ? [] : tickets.data?.pages.flatMap(page => page.items) ?? [];
  const selectedTicketOutsidePage = ticket && !ticketOptions.some(item => item.ticket_id === ticket.ticket_id);
  return <main className="wrap tickets-page">
    <div className="page-heading"><h1>工作台</h1><a href="/tickets">查看全部工单</a></div>
    <section className="ticket-panel" aria-label="选择工单">
      {customers.isPending ? <p className="empty" role="status">正在读取可见客户…</p> :
        customers.isError ? <ReadError error={customers.error} retry={() => void customers.refetch()}/> :
        customerOptions.length === 0 ? <p className="empty">暂无可见客户。</p> :
        <div className="toolbar workbench-pickers">
          <label>选择客户<select value={customerId} onChange={event => { setCustomer(event.target.value); selectTicket(""); }}>
            <option value="">请选择客户</option>
            {selectedCustomerOutsidePage && <option value={customerId}>{customerId}</option>}
            {customerOptions.map(customer => <option key={customer.customer_id} value={customer.customer_id}>
              {customer.display_name} · {customer.customer_code}
            </option>)}
          </select></label>
          {customers.hasNextPage && <button type="button" disabled={customers.isFetchingNextPage}
            onClick={() => void customers.fetchNextPage()}>加载更多客户</button>}
          <label>选择工单<select value={ticketId} disabled={!customerId || tickets.isError || tickets.isPending}
            onChange={event => { setCustomer(customerId); selectTicket(event.target.value); }}>
            <option value="">请选择工单</option>
            {selectedTicketOutsidePage && <option value={ticket.ticket_id}>{ticket.title}</option>}
            {ticketOptions.map(item => <option key={item.ticket_id} value={item.ticket_id}>{item.title}</option>)}
          </select></label>
          {tickets.hasNextPage && !tickets.isError && <button type="button" disabled={tickets.isFetchingNextPage}
            onClick={() => void tickets.fetchNextPage()}>加载更多工单</button>}
        </div>}
      {customerId && tickets.isPending && <p className="workbench-notice" role="status">正在读取客户工单…</p>}
      {customerId && tickets.isError && <ReadError error={tickets.error} retry={() => void tickets.refetch()}/>}
      {customerId && tickets.isSuccess && ticketOptions.length === 0 &&
        <p className="workbench-notice">当前客户暂无可见工单。</p>}
      {!ticketId && !customerId && customers.isSuccess && customerOptions.length > 0 &&
        <p className="workbench-notice">请选择客户和工单。</p>}
    </section>
    {ticketId && (detail.isError ? <ReadError error={detail.error} retry={() => void detail.refetch()}/> :
      ticket ? <WorkspaceTicket key={ticket.ticket_id} api={api} actor={actor} ticket={ticket}/> :
      <p className="workbench-notice" role="status">正在读取所选工单…</p>)}
  </main>;
}
