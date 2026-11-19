"""Synchronous, read-only compatibility Spike. The model bytes are scripted fixture inputs."""

import json
from collections.abc import Callable
from typing import Annotated, Literal, TypedDict

from langchain_core.messages import AIMessage, AIMessageChunk, AnyMessage, ToolCall, ToolMessage
from langchain_core.output_parsers import PydanticOutputParser
from langchain_core.tools import StructuredTool
from langgraph.graph import END, START, StateGraph, add_messages
from langgraph.types import interrupt
from pydantic import BaseModel, ConfigDict, Field


Identifier = Annotated[str, Field(min_length=1, max_length=128)]
SeatCount = Annotated[int, Field(ge=0)]


class Boundary(BaseModel):
    model_config = ConfigDict(extra="forbid", strict=True, hide_input_in_errors=True)


class SubscriptionArgs(Boundary):
    customer_id: Identifier


class Answer(SubscriptionArgs):
    input_id: Identifier


class SubscriptionFacts(SubscriptionArgs):
    used_seats: SeatCount
    base_seats: SeatCount


class Resolution(SubscriptionFacts):
    resolution_code: Literal["read_only_diagnosis"]


class Chunk(Boundary):
    id: Identifier | None = None
    name: Identifier | None = None
    args: str


class State(TypedDict, total=False):
    messages: Annotated[list[AnyMessage], add_messages]
    input_id: str
    customer_id: str
    tool_call_id: str
    observation: dict
    resolution: dict
    usage: dict | None


def unique_fields(pairs):
    fields = {}
    for key, value in pairs:
        if key in fields:
            raise ValueError("Duplicate tool argument")
        fields[key] = value
    return fields


def decode_tool_call(chunks, *, confirmed_customer_id: str) -> ToolCall:
    """One permitted call in this Spike. Dispatch only after the complete stream validates."""
    if not isinstance(chunks, list) or not 1 <= len(chunks) <= 32:
        raise ValueError("Expected one bounded tool call stream")
    parts = [Chunk.model_validate(raw) for raw in chunks]
    call_id, name = parts[0].id, parts[0].name
    if not call_id or name != "get_subscription":
        raise ValueError("Missing call ID or unregistered tool")
    if sum(len(part.args.encode("utf-8")) for part in parts) > 8192:
        raise ValueError("Tool arguments exceed the Spike byte limit")
    message = AIMessageChunk(content="")
    for index, part in enumerate(parts):
        if part.id not in (None, call_id) or part.name not in (None, name):
            raise ValueError("Unexpected second tool call or conflicting identity")
        # Providers can repeat the common ID/name. The chunk reducer concatenates
        # argument strings; provide stable metadata just once for its single slot.
        delta = {"index": 0, "id": call_id if index == 0 else None,
                 "name": name if index == 0 else None, "args": part.args}
        message += AIMessageChunk(content="", tool_call_chunks=[delta])
    # LangChain exposes partial parsed args while streaming. Require complete JSON
    # from the merged raw buffer, including the closing delimiter.
    raw_args = message.tool_call_chunks[0]["args"]
    args = SubscriptionArgs.model_validate(json.loads(raw_args, object_pairs_hook=unique_fields))
    if args.customer_id != confirmed_customer_id:
        raise ValueError("Tool customer differs from the confirmed customer")
    return ToolCall(name=name, id=call_id, args=args.model_dump(), type="tool_call")


def parse_resolution(raw: dict, observation: dict) -> dict:
    """Validate the scripted model's structured reply against independent tool facts."""
    facts = SubscriptionFacts.model_validate(observation)
    if not isinstance(raw, dict):
        raise ValueError("Expected a structured reply")
    message = AIMessage(content=json.dumps(raw, ensure_ascii=False))
    result = PydanticOutputParser(pydantic_object=Resolution).invoke(message)
    if result.model_dump(exclude={"resolution_code"}) != facts.model_dump():
        raise ValueError("Structured reply differs from observed subscription")
    return result.model_dump()


def subscription_tool(read_subscription: Callable[[str], dict]) -> StructuredTool:
    def get_subscription(customer_id: str) -> dict:
        """Read the subscription for the confirmed customer."""
        raw = read_subscription(customer_id)
        # The callback owns authorization. Only these fields are checkpointed.
        facts = SubscriptionFacts.model_validate({key: raw[key] for key in SubscriptionFacts.model_fields})
        if facts.customer_id != customer_id:
            raise ValueError("Tool returned facts for a different customer")
        return facts.model_dump()

    return StructuredTool.from_function(get_subscription, args_schema=SubscriptionArgs)


def build_graph(checkpointer, *, run_id: str, ensure_pending: Callable[[str], str],
                authorize_customer: Callable[[str], bool], check_fragment: Callable[[str], None],
                read_subscription: Callable[[str], dict], model_script: dict):
    tool = subscription_tool(read_subscription)

    def clarify(state: State):
        check_fragment("clarify")
        # Resume restarts this node from its beginning. The callback must read or
        # insert one durable input for this run and return its original ID.
        input_id = ensure_pending(run_id)
        value = interrupt({"input_id": input_id, "required_fields": ["customer_id"]})
        answer = Answer.model_validate(value)
        if answer.input_id != input_id:
            raise ValueError("Resume input does not match the pending clarification")
        if not authorize_customer(answer.customer_id):
            raise PermissionError("Customer outside the current actor's scope")
        return {"input_id": input_id, "customer_id": answer.customer_id}

    def diagnose(state: State):
        check_fragment("diagnose")
        call = decode_tool_call(model_script["tool_chunks"], confirmed_customer_id=state["customer_id"])
        check_fragment("tool_dispatch")
        facts = tool.invoke(call["args"])
        return {
            "tool_call_id": call["id"], "observation": facts, "usage": None,
            "messages": [
                AIMessage(id=f"{run_id}:tool", content="", tool_calls=[call]),
                ToolMessage(id=f"{run_id}:observation", content=json.dumps(facts), tool_call_id=call["id"]),
                AIMessage(id=f"{run_id}:diagnosis", content="正在核对订阅数据。"),
            ],
        }

    def finish(state: State):
        check_fragment("finish")
        resolution = parse_resolution(model_script["structured_output"], state["observation"])
        # add_messages replaces the draft with the same ID rather than appending a second answer.
        text = f"基础席位 {resolution['base_seats']}，已用 {resolution['used_seats']}。"
        return {"resolution": resolution, "messages": [AIMessage(id=f"{run_id}:diagnosis", content=text)]}

    graph = StateGraph(State)
    graph.add_node("clarify", clarify)
    graph.add_node("diagnose", diagnose)
    graph.add_node("finish", finish)
    graph.add_edge(START, "clarify")
    graph.add_edge("clarify", "diagnose")
    graph.add_edge("diagnose", "finish")
    graph.add_edge("finish", END)
    return graph.compile(checkpointer=checkpointer)


def project_events(update: dict) -> list[dict]:
    """Map updates to typed public data; the caller adds seq/generation after persistence."""
    events = []
    for item in update.get("__interrupt__", ()):
        value = item.value
        events.append({"type": "clarification.requested", "payload": {
            "input_id": value["input_id"], "required_fields": ["customer_id"],
        }})
    if "clarify" in update:
        result = update["clarify"]
        events.append({"type": "clarification.answered", "payload": {
            "input_id": result["input_id"], "customer_id": result["customer_id"],
        }})
    if "diagnose" in update:
        events.append({"type": "tool.completed", "payload": {
            "tool_call_id": update["diagnose"]["tool_call_id"], "tool_name": "get_subscription",
        }})
    if "finish" in update:
        result = update["finish"]
        safe = Resolution.model_validate(result["resolution"])
        events.append({"type": "message.final", "payload": {
            "message_id": result["messages"][0].id, **safe.model_dump(),
        }})
    return events
