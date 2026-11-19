"""Confirmed request -> trusted reads/policy -> immutable proposal -> interrupt.

The model never supplies eligibility, quantities or an approval decision. A
message-only request follows the existing read-only diagnosis. No business write
or approval decision is reachable from this graph.
"""
from datetime import datetime
import json
from typing import TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from apps.supportflow.domain import proposals
from apps.supportflow.domain import diagnosis_routing
from apps.supportflow.domain.contracts import EvidenceBundle
from apps.supportflow.domain.grant_contracts import GrantConfirmedFields
from apps.supportflow.jobs import proposal_waiting, waiting
from apps.supportflow.jobs.contracts import FragmentOutcome, StopFragment
from apps.supportflow.tools.readonly import ToolExecutor
from . import rag_graph

GRAPH_VERSION = "grant-proposal-v1"
POLICY_QUERY = "临时席位申请规则 grant_temporary_seats active 席位天数 冷却 主管审批 已提交领取回执"


class State(TypedDict, total=False):
    graph_version: str
    grant_requested: bool
    required_fields: list[str]
    customer_id: str
    observation_refs: list[dict]
    evidence_refs: list[dict]
    proposal_id: str
    outcome: dict
    authorization_id: str
    execution_outcome: dict


def config(run):
    return {"configurable": {"thread_id": run["run_id"]}, "recursion_limit": 12}


def _confirmed(run):
    fields = dict(run.get("confirmed_fields", {}))
    if diagnosis_routing.enabled(run["config_snapshot"]) and fields.get("requested_action") is None:
        # Read-only email answers are not grant commands. Validate the email,
        # then exclude it from the unchanged grant authorization DTO.
        diagnosis_routing.confirmed_email(run)
        fields.pop("invitation_email", None)
    customer = fields.get("customer_id") or run.get("customer_id")
    if customer is not None:
        fields["customer_id"] = customer
    return GrantConfirmedFields.model_validate(fields)


def build_graph(conn, run, gate, model, saver, *, approval_handler=None, execution_handler=None):
    executor = ToolExecutor(conn, run, gate, grant_subscription=True)

    def confirm(state):
        gate.check("grant.confirm.before")
        fields = _confirmed(run)
        if bool(fields.requested_action) != state["grant_requested"]:
            raise StopFragment("CONFIRMED_FIELDS_INVALID")
        required = state["required_fields"]
        if required:
            pending = waiting.prepare_input(conn, run, required, "请补齐本次申请或诊断需要的确认信息。")
            answer = interrupt({"kind": "clarification", "input_id": pending["input_id"], "version": pending["version"]})
            stored = waiting.read_resume(conn, run, input_id=pending["input_id"])
            if stored is None or answer != stored:
                raise StopFragment("CLARIFICATION_RESUME_INVALID")
            fields = _confirmed(run)
        if fields.customer_id is None or state["grant_requested"] and any(
                getattr(fields, name) is None for name in ("invitation_email", "seats", "days")):
            raise StopFragment("INPUT_REQUIRED")
        return {"customer_id": fields.customer_id}

    def diagnose(state):
        gate.check("grant.readonly.before")
        inner = {**run, "customer_id": state["customer_id"], "_diagnosis_checkpoint_id": run["run_id"] + ":diagnosis",
                 "config_snapshot": {**run["config_snapshot"], "graph_version": rag_graph.GRAPH_VERSION}}
        outcome = rag_graph.execute_fragment(conn, inner, gate, model=model, checkpointer=saver)
        if diagnosis_routing.enabled(run["config_snapshot"]) and outcome.reason_code == "INPUT_REQUIRED":
            # The first nested checkpoint has already persisted classification.
            # Resuming this outer node reuses it, then consumes the same interrupt.
            pending = waiting.prepare_input(conn, run, ["invitation_email"],
                "请提供本工单客户需要核查的邀请邮箱。", input_seq=2)
            answer = interrupt({"kind": "clarification", "input_id": pending["input_id"], "version": pending["version"]})
            stored = waiting.read_resume(conn, run, input_id=pending["input_id"])
            if stored is None or answer != stored or diagnosis_routing.confirmed_email(run) != stored["answers"]["invitation_email"]:
                raise StopFragment("CLARIFICATION_RESUME_INVALID")
            inner["_diagnosis_checkpoint_id"] = run["run_id"] + ":diagnosis:email"
            outcome = rag_graph.execute_fragment(conn, inner, gate, model=model, checkpointer=saver)
        return {"outcome": {"status": outcome.status, "reason_code": outcome.reason_code, "result": outcome.result}}

    def read(state):
        refs = []
        fields = _confirmed(run)
        for tool_id in ("get_customer", "get_subscription", "get_invitation", "get_service_status"):
            envelope, observed = executor.execute(tool_id, invitation_email=fields.invitation_email if tool_id == "get_invitation" else None)
            refs.extend(observed)
            if not envelope.ok:
                raise StopFragment("FACTS_INCOMPLETE")
        return {"observation_refs": refs}

    def retrieve(state):
        from apps.supportflow.retrieval.service import SearchService
        bundle = EvidenceBundle.model_validate_json(json.dumps(SearchService(conn, run, gate).search(POLICY_QUERY)))
        if not bundle.ok:
            raise StopFragment("EVIDENCE_REQUIRED")
        # Publish only the complete, code-pinned policy reference as proposal
        # support. Extra top-k candidates do not become citations by existing.
        from apps.supportflow.domain.grant_policy import load_policy_bundle
        pin = load_policy_bundle()
        refs = [ref.model_dump(mode="json") for ref in bundle.refs
                if ref.section_id == pin["section_id"] and ref.content_sha256 == pin["content_sha256"]]
        if not refs:
            raise StopFragment("EVIDENCE_REQUIRED")
        return {"evidence_refs": refs}

    def prepare(state):
        gate.check("grant.prepare.before")
        # Crash after proposal commit/before this node's checkpoint must not
        # re-read tools, renew times, consume another round or allocate new IDs.
        old = proposal_waiting.prepared(conn, run)
        if old is not None:
            with conn.transaction():
                proposal_waiting.validate_prepared(conn, run, old)
            return {"proposal_id": old["proposal_id"]}
        envelope, final_ref = executor.execute("get_subscription")
        if not envelope.ok:
            raise StopFragment("FACTS_INCOMPLETE")
        refs = [*state["observation_refs"], *final_ref]
        # The pure precheck gives a deterministic stop. The storage primitive
        # then repeats it from raw app rows under the actual publication locks.
        with conn.transaction():
            decision = proposal_waiting.evaluate_candidate(conn, run, evidence_refs=state["evidence_refs"], observation_refs=refs)
            from apps.supportflow.domain.grant_policy_reply import enabled as policy_reply_enabled
            if policy_reply_enabled(run['config_snapshot']) and decision.reason_code == 'CAPACITY_NOT_EXHAUSTED':
                from apps.supportflow.jobs.grant_policy_reply import prepare as prepare_reply
                outcome = prepare_reply(conn, run, state['evidence_refs'], refs)
                return {'outcome': {'status':outcome.status,'reason_code':outcome.reason_code,'result':outcome.result}}
        if decision.decision != "proposal":
            raise StopFragment(decision.reason_code or "EVIDENCE_REQUIRED")
        proposal = proposals.persist_prepared_proposal(conn, run, decision.command,
            evidence_refs=state["evidence_refs"], observation_refs=refs)
        return {"proposal_id": proposal["proposal_id"], "observation_refs": refs}

    def approval(state):
        gate.check("grant.wait.before")
        row = proposal_waiting.prepared(conn, run, proposal_id=state["proposal_id"])
        if row is None:
            raise StopFragment("CHECKPOINT_INCOMPATIBLE")
        answer = interrupt({"kind": "approval", "proposal_id": row["proposal_id"], "approval_id": row["approval_id"], "action_id": row["action_id"]})
        if approval_handler is not None:
            return approval_handler(conn, run, gate, row, answer)
        raise StopFragment("APPROVAL_RESUME_UNSUPPORTED")

    builder = StateGraph(State)
    for name, node in (("confirm", confirm), ("diagnose", diagnose), ("read", read), ("retrieve", retrieve), ("prepare", prepare), ("approval", approval)):
        builder.add_node(name, node)
    from apps.supportflow.domain.input_refusal import enabled as refusal_enabled
    def refusal_policy(state):
        # Separate checkpoint prevents replay of this retrieval when confirm resumes.
        if state["required_fields"] and state["grant_requested"] and refusal_enabled(run["config_snapshot"]):
            from apps.supportflow.retrieval.service import SearchService
            SearchService(conn, run, gate).search(POLICY_QUERY)
        return {}
    builder.add_node("refusal_policy", refusal_policy)
    builder.add_edge(START, "refusal_policy")
    builder.add_edge("refusal_policy", "confirm")
    builder.add_conditional_edges("confirm", lambda state: "read" if state["grant_requested"] else "diagnose")
    builder.add_edge("diagnose", END)
    builder.add_edge("read", "retrieve")
    builder.add_edge("retrieve", "prepare")
    builder.add_conditional_edges("prepare", lambda state: END if "outcome" in state else "approval")
    if execution_handler is None:
        builder.add_edge("approval", END)
    else:
        builder.add_node("execute", lambda state: execution_handler(conn, run, gate, state))
        builder.add_edge("approval", "execute")
        builder.add_edge("execute", END)
    return builder.compile(checkpointer=saver)


def projection(conn, run, graph, saver, *, graph_version=GRAPH_VERSION, approval_resume=None):
    saved = saver.get_tuple(config(run))
    if saved is None:
        return None
    values = saved.checkpoint["channel_values"]
    if (values.get("graph_version") or values.get("__start__", {}).get("graph_version")) != graph_version:
        return FragmentOutcome("handoff", "CONFIG_VERSION_UNSUPPORTED")
    if "outcome" in values:
        fields = _confirmed(run)
        row = values["outcome"]
        policy_reply = isinstance(row.get('result'), dict) and row['result'].get('schema_version') == 7
        if values.get("customer_id") != fields.customer_id or fields.requested_action is not None and not policy_reply:
            return FragmentOutcome("handoff", "CUSTOMER_BINDING_CHANGED")
        if row["result"] is not None:
            if policy_reply:
                from apps.supportflow.domain.grant_policy_reply import GrantPolicyReply, enabled as policy_reply_enabled
                if (not policy_reply_enabled(run['config_snapshot']) or fields.requested_action != 'grant_temporary_seats'
                        or (fields.seats, fields.days) != (row['result'].get('requested_seats'), row['result'].get('requested_days'))):
                    return FragmentOutcome('handoff','CHECKPOINT_INCOMPATIBLE')
                GrantPolicyReply.model_validate_json(json.dumps(row['result']))
            else:
                rag_graph._outcome(row["result"], run["config_snapshot"])
            customer = row["result"].get("customer")
            if (customer is not None and customer["customer_id"] != fields.customer_id
                    or any(ref["run_id"] != run["run_id"] or ref["execution_generation"] > run["execution_generation"]
                           for ref in row["result"]["observation_refs"])):
                return FragmentOutcome("handoff", "CHECKPOINT_INCOMPATIBLE")
        return FragmentOutcome(row["status"], row["reason_code"], row["result"])
    snapshot = graph.get_state(config(run))
    pending = [item.value for task in snapshot.tasks for item in task.interrupts]
    if not pending:
        return None
    if len(pending) != 1 or not isinstance(pending[0], dict):
        return FragmentOutcome("handoff", "CHECKPOINT_INCOMPATIBLE")
    value = pending[0]
    metadata = {"wait_checkpoint_id": saved.checkpoint["id"], "wait_checkpoint_ns": "",
                "wait_checkpoint_at": datetime.fromisoformat(saved.checkpoint["ts"].replace("Z", "+00:00"))}
    if value.get("kind") == "clarification" and set(value) == {"kind", "input_id", "version"}:
        if not isinstance(value["input_id"], str) or type(value["version"]) is not int or value["version"] < 1:
            return FragmentOutcome("handoff", "CHECKPOINT_INCOMPATIBLE")
        if waiting.read_resume(conn, run, input_id=value["input_id"]) is not None:
            return None
        return FragmentOutcome("awaiting_input", "INPUT_REQUIRED", wait_input_id=value["input_id"], **metadata)
    if value.get("kind") == "approval" and set(value) == {"kind", "proposal_id", "approval_id", "action_id"}:
        proposal = proposal_waiting.prepared(conn, run, proposal_id=value["proposal_id"])
        if proposal is None or any(proposal[key] != value[key] for key in ("proposal_id", "approval_id", "action_id")):
            return FragmentOutcome("handoff", "CHECKPOINT_INCOMPATIBLE")
        if approval_resume is not None and approval_resume(conn, run, approval_id=proposal["approval_id"]) is not None:
            return None
        try:
            with conn.transaction():
                proposal_waiting.validate_prepared(conn, run, proposal)
        except StopFragment as error:
            return FragmentOutcome(error.status, error.reason_code)
        return FragmentOutcome("awaiting_approval", "APPROVAL_REQUIRED", wait_proposal_id=value["proposal_id"], **metadata)
    return FragmentOutcome("handoff", "CHECKPOINT_INCOMPATIBLE")


def checkpoint_probe(conn, run):
    if not waiting.compatible_config(run["config_snapshot"]) or run["config_snapshot"]["graph_version"] != GRAPH_VERSION:
        return FragmentOutcome("handoff", "CONFIG_VERSION_UNSUPPORTED")
    saver = PostgresSaver(conn)
    return projection(conn, run, build_graph(conn, run, None, None, saver), saver)


def execute_fragment(conn, run, gate, *, model):
    gate.check("grant.start")
    if not waiting.compatible_config(run["config_snapshot"]) or run["config_snapshot"]["graph_version"] != GRAPH_VERSION:
        return FragmentOutcome("handoff", "CONFIG_VERSION_UNSUPPORTED")
    saver = PostgresSaver(conn)
    graph = build_graph(conn, run, gate, model, saver)
    saved = saver.get_tuple(config(run))
    initial = None
    if saved is None:
        fields = _confirmed(run)
        names = ("customer_id", "invitation_email", "seats", "days") if fields.requested_action else ("customer_id",)
        initial = {"graph_version": GRAPH_VERSION, "grant_requested": fields.requested_action is not None,
                   "required_fields": [name for name in names if getattr(fields, name) is None]}
    else:
        outcome = projection(conn, run, graph, saver)
        if outcome is not None:
            return outcome
        snapshot = graph.get_state(config(run))
        pending = [item.value for task in snapshot.tasks for item in task.interrupts]
        if pending:
            if pending[0].get("kind") != "clarification":
                raise StopFragment("APPROVAL_RESUME_UNSUPPORTED")
            answer = waiting.read_resume(conn, run, input_id=pending[0]["input_id"])
            if answer is None:
                raise StopFragment("CLARIFICATION_RESUME_INVALID")
            initial = Command(resume=answer)
    graph.invoke(initial, config(run), durability="sync")
    gate.check("grant.after")
    result = projection(conn, run, graph, saver)
    if result is None:
        raise StopFragment("CHECKPOINT_INCOMPATIBLE")
    return result
