"""Optional wording selection after an actual verified grant."""
from dataclasses import replace
from time import monotonic
from typing import Literal

from langchain_core.messages import HumanMessage, SystemMessage
from pydantic import ValidationError

from apps.supportflow.domain.contracts import StrictModel
from apps.supportflow.domain.grant_effects import (
    GRANT_REPLY_ACKNOWLEDGEMENT, GRANT_REPLY_PUBLIC_OPENING, streamed_grant_reply_enabled,
)
from apps.supportflow.jobs.contracts import StopFragment
from apps.supportflow.jobs.public_text import PublicTextBatcher
from .models import ModelError
from .run_graph import MODEL_ERROR_CODES, ResponseMetadata, _metadata


MAX_REPLY_SECONDS = 2.0
MAX_PUBLIC_REPLY_SECONDS = 12.0
PUBLIC_GATE_INTERVAL_SECONDS = 0.2


class ReplyChoice(StrictModel):
    opening: Literal["direct", "acknowledged"]


class ReplyResponse(ResponseMetadata):
    content: ReplyChoice


class StreamedReplyResponse(ResponseMetadata):
    content: str


def _complete(gate, attempt, metadata, *, error_code=None):
    usage = metadata.usage.model_dump() if metadata.usage is not None else None
    gate.complete_model(attempt, usage, metadata.provider_response_id, error_code=error_code)


def _present_streamed(outcome, gate, stream, publish_delta, attempt, timeout):
    opening = GRANT_REPLY_PUBLIC_OPENING
    accepted = ""
    part = 0
    next_gate_check = monotonic() + PUBLIC_GATE_INTERVAL_SECONDS

    def emit(flush):
        nonlocal part
        part += 1
        publish_delta(flush.delta, part)

    batcher = PublicTextBatcher(emit)

    def feed(text):
        nonlocal accepted
        if not isinstance(text, str) or not opening.startswith(accepted + text):
            raise ModelError("MODEL_PROTOCOL_ERROR")
        if not text:
            return
        accepted += text
        batcher.feed(text)
        if accepted == opening:
            # An entirely approved opening can be delivered before provider
            # completion metadata arrives. The atomic final can replace it.
            batcher.complete()

    def tick():
        nonlocal next_gate_check
        now = monotonic()
        if now >= next_gate_check:
            gate.check("grant.reply.stream")
            next_gate_check = now + PUBLIC_GATE_INTERVAL_SECONDS
        batcher.flush_due()

    messages = [
        SystemMessage(content="Output exactly this Chinese acknowledgement, with no extra characters or formatting: " + opening),
        HumanMessage(content="请输出这句致意语。"),
    ]
    try:
        raw = stream(messages, timeout, allowed_texts=(opening,), on_text=feed, tick=tick)
    except (ModelError, StopFragment) as error:
        code = error.reason_code if isinstance(error, StopFragment) else error.code
        if not isinstance(error, StopFragment) and code not in MODEL_ERROR_CODES:
            code = "MODEL_REPLY_UNAVAILABLE"
        metadata = _metadata(getattr(error, "usage", None), getattr(error, "provider_response_id", None))
        _complete(gate, attempt, metadata, error_code=code)
        return outcome
    try:
        response = StreamedReplyResponse.model_validate(raw)
        if accepted != opening or response.content != opening:
            raise ValueError("Incomplete approved opening")
    except (ValidationError, ValueError):
        metadata = _metadata(raw.get("usage"), raw.get("provider_response_id")) if isinstance(raw, dict) else ResponseMetadata()
        _complete(gate, attempt, metadata, error_code="MODEL_PROTOCOL_ERROR")
        return outcome
    _complete(gate, attempt, response)
    return replace(outcome, result={**outcome.result, "reply_text": opening + outcome.result["reply_text"]})


def present_verified_grant(outcome, run, gate, model, *, publish_delta=None):
    """Use one original-budget attempt and keep the verified receipt text on failure."""
    result = outcome.result or {}
    verification = result.get("verification") or {}
    config = run["config_snapshot"]
    streamed = streamed_grant_reply_enabled(config)
    if (not (streamed or config.get("grant_reply_mode") == "assisted")
            or run.get("recovery_count", 0) != 0
            or outcome.status != "succeeded" or result.get("schema_version") != 4
            or result.get("effect_status") != "committed"
            or verification.get("status") not in {"verified_current", "verified_historical"}):
        return outcome
    stream = getattr(model, "stream_public_text", None) if streamed else None
    if streamed and (not callable(stream) or publish_delta is None):
        return outcome

    try:
        gate.check("grant.reply.before")
        attempt = gate.reserve_model()
    except StopFragment:
        return outcome
    try:
        limit = MAX_PUBLIC_REPLY_SECONDS if streamed else MAX_REPLY_SECONDS
        timeout = min(limit, gate.check("grant.reply.dispatch"))
    except StopFragment as error:
        _complete(gate, attempt, ResponseMetadata(), error_code=error.reason_code)
        return outcome
    if streamed:
        return _present_streamed(outcome, gate, stream, publish_delta, attempt, timeout)

    # The adapter chooses an opening only. It receives no business or actor data
    # and cannot add facts, issue a tool call, or change the stored receipt.
    messages = [SystemMessage(content="Choose one opening for a short support confirmation: "
                                      "direct or acknowledged. Return only the requested JSON.")]
    try:
        raw = model.structured(messages, ReplyChoice.model_json_schema(), timeout)
    except Exception as error:
        code = getattr(error, "code", None)
        if not isinstance(code, str) or code not in MODEL_ERROR_CODES:
            code = "MODEL_REPLY_UNAVAILABLE"
        metadata = _metadata(getattr(error, "usage", None), getattr(error, "provider_response_id", None))
        _complete(gate, attempt, metadata, error_code=code)
        return outcome

    try:
        response = ReplyResponse.model_validate(raw)
    except ValidationError:
        metadata = _metadata(raw.get("usage"), raw.get("provider_response_id")) if isinstance(raw, dict) else ResponseMetadata()
        _complete(gate, attempt, metadata, error_code="MODEL_PROTOCOL_ERROR")
        return outcome
    _complete(gate, attempt, response)
    if response.content.opening == "acknowledged":
        return replace(outcome, result={**result, "reply_text": GRANT_REPLY_ACKNOWLEDGEMENT + result["reply_text"]})
    return outcome
