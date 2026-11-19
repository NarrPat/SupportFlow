"""Durable missing-customer interrupt around the unchanged read-only diagnosis.

Only server-stored answers are resumed. The two graph checkpoints use distinct
server-derived thread IDs on the same locked connection. No proposal or grant
node is enabled before the full current grant gate exists.
"""
from datetime import datetime
from typing import TypedDict

from langgraph.checkpoint.postgres import PostgresSaver
from langgraph.graph import END, START, StateGraph
from langgraph.types import Command, interrupt

from apps.supportflow.jobs import waiting
from apps.supportflow.jobs.contracts import FragmentOutcome, StopFragment
from . import rag_graph


GRAPH_VERSION = "clarification-diagnosis-v1"


class State(TypedDict, total=False):
    graph_version: str
    customer_id: str
    resolution: dict


def config(run):
    return {"configurable": {"thread_id": run["run_id"]}, "recursion_limit": 8}


def inner_run(run, customer_id):
    return {**run, "customer_id": customer_id, "_diagnosis_checkpoint_id": run["run_id"] + ":diagnosis",
            "config_snapshot": {**run["config_snapshot"], "graph_version": rag_graph.GRAPH_VERSION}}


def build_graph(conn, run, gate, model, saver):
    def confirm(state):
        gate.check("clarification.before")
        # Use the checkpoint's original branch on re-entry. A newly persisted
        # answer must not make the restarted node skip its matching interrupt.
        customer = state.get("customer_id")
        if customer is None:
            pending = waiting.prepare_input(conn, run, ["customer_id"], "请选择本次诊断对应的客户。")
            answer = interrupt({"kind": "clarification", "input_id": pending["input_id"], "version": pending["version"]})
            stored = waiting.read_resume(conn, run, input_id=pending["input_id"])
            if stored is None or answer != stored:
                raise StopFragment("CLARIFICATION_RESUME_INVALID")
            customer = stored["answers"]["customer_id"]
        gate.check("clarification.after")
        return {"customer_id": customer}

    def diagnose(state):
        gate.check("diagnosis.before")
        current = run.get("confirmed_fields", {}).get("customer_id") or run.get("customer_id")
        if current != state["customer_id"]:
            raise StopFragment("CUSTOMER_BINDING_CHANGED")
        outcome = rag_graph.execute_fragment(conn, inner_run(run, current), gate, model=model, checkpointer=saver)
        if outcome.result is None:
            raise StopFragment(outcome.reason_code, outcome.status)
        return {"resolution": outcome.result}

    builder = StateGraph(State)
    builder.add_node("confirm", confirm)
    builder.add_node("diagnose", diagnose)
    builder.add_edge(START, "confirm")
    builder.add_edge("confirm", "diagnose")
    builder.add_edge("diagnose", END)
    return builder.compile(checkpointer=saver)


def pending_outcome(conn, run, graph, saver):
    snapshot = graph.get_state(config(run))
    saved = saver.get_tuple(config(run))
    if saved is None:
        return None
    state = saved.checkpoint["channel_values"]
    version = state.get("graph_version") or state.get("__start__", {}).get("graph_version")
    if version != GRAPH_VERSION:
        return FragmentOutcome("handoff", "GRAPH_VERSION_MISMATCH")
    if "resolution" in state:
        outcome = rag_graph._outcome(state["resolution"], run["config_snapshot"])
        customer = run.get("confirmed_fields", {}).get("customer_id") or run.get("customer_id")
        if state.get("customer_id") != customer:
            return FragmentOutcome("handoff", "CUSTOMER_BINDING_CHANGED")
        result_customer = outcome.result.get("customer")
        if (result_customer is not None and result_customer["customer_id"] != customer
                or any(ref["run_id"] != run["run_id"] or ref["execution_generation"] > run["execution_generation"]
                       for ref in outcome.result["observation_refs"])):
            return FragmentOutcome("handoff", "CHECKPOINT_INCOMPATIBLE")
        return outcome
    interruptions = [item for task in snapshot.tasks for item in task.interrupts]
    if not interruptions:
        return None
    if len(interruptions) != 1:
        return FragmentOutcome("handoff", "CHECKPOINT_INCOMPATIBLE")
    value = interruptions[0].value
    if (not isinstance(value, dict) or set(value) != {"kind", "input_id", "version"}
            or value["kind"] != "clarification" or not isinstance(value["input_id"], str)
            or type(value["version"]) is not int or value["version"] < 1):
        return FragmentOutcome("handoff", "CHECKPOINT_INCOMPATIBLE")
    if waiting.read_resume(conn, run, input_id=value["input_id"]) is not None:
        return None
    return FragmentOutcome("awaiting_input", "CLARIFICATION_REQUIRED", wait_input_id=value["input_id"],
        wait_checkpoint_id=saved.checkpoint["id"], wait_checkpoint_ns="",
        wait_checkpoint_at=datetime.fromisoformat(saved.checkpoint["ts"].replace("Z", "+00:00")))


def checkpoint_probe(conn, run):
    if not waiting.compatible_config(run["config_snapshot"]):
        return FragmentOutcome("handoff", "CONFIG_VERSION_UNSUPPORTED")
    saver = PostgresSaver(conn)
    return pending_outcome(conn, run, build_graph(conn, run, None, None, saver), saver)


def execute_fragment(conn, run, gate, *, model):
    gate.check("graph.start")
    if not waiting.compatible_config(run["config_snapshot"]):
        return FragmentOutcome("handoff", "CONFIG_VERSION_UNSUPPORTED")
    saver = PostgresSaver(conn)
    graph = build_graph(conn, run, gate, model, saver)
    saved = saver.get_tuple(config(run))
    initial = None
    if saved is None:
        initial = {"graph_version": GRAPH_VERSION}
        customer = run.get("confirmed_fields", {}).get("customer_id") or run.get("customer_id")
        if customer is not None:
            initial["customer_id"] = customer
    if saved is not None:
        outcome = pending_outcome(conn, run, graph, saver)
        if outcome is not None:
            return outcome
        snapshot = graph.get_state(config(run))
        pending = [i.value for task in snapshot.tasks for i in task.interrupts]
        if pending:
            resume = waiting.read_resume(conn, run, input_id=pending[0]["input_id"])
            if resume is None:
                raise StopFragment("CLARIFICATION_RESUME_INVALID")
            initial = Command(resume=resume)
    graph.invoke(initial, config(run), durability="sync")
    gate.check("graph.after")
    outcome = pending_outcome(conn, run, graph, saver)
    if outcome is None:
        raise StopFragment("CHECKPOINT_INCOMPATIBLE")
    return outcome
