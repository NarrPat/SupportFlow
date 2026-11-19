"""Bounded DeepSeek Chat/SSE adapter. Credentials never enter messages or snapshots."""
import json
import asyncio
from contextlib import AsyncExitStack, suppress
from functools import wraps
import math
import os
from pathlib import Path
from time import monotonic

import httpx
from langchain_core.messages import convert_to_openai_messages
from apps.supportflow.jobs.contracts import StaleFragment, StopFragment


BASE_URL = "https://api.deepseek.com"
GRAPH_VERSION = "customer-handoff-v1"
MAX_WIRE_BYTES = 131072
PUBLIC_REPLY_TOKENS = 64
PUBLIC_REPLY_TIMEOUT_SECONDS = 12
PUBLIC_REPLY_TICK_SECONDS = .025


class ModelError(Exception):
    def __init__(self, code, *, status=None, usage=None, provider_response_id=None):
        super().__init__(code)
        self.code, self.status = code, status
        self.usage, self.provider_response_id = usage, provider_response_id


def snapshot_settings():
    mode = os.environ.get("SF_MODEL_MODE", "live")
    if mode not in ("live", "scripted"):
        raise ValueError("Invalid SupportFlow model mode")
    if mode == "scripted" and not os.environ.get("PGDATABASE", "").startswith("sf_eval_"):
        raise ValueError("Scripted configuration requires an evaluation database")
    graph_version = os.environ.get("SF_GRAPH_VERSION", GRAPH_VERSION)
    if graph_version not in {GRAPH_VERSION, "readonly-diagnosis-v1", "readonly-diagnosis-rag-v1", "clarification-diagnosis-v1", "grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"}:
        raise ValueError("Invalid SupportFlow graph version")
    snapshot = {"schema_version": 1, "contract_version": "dispatch-v1", "graph_version": graph_version,
            "provider": "deepseek", "base_url": BASE_URL, "model_id": os.environ.get("SF_MODEL_ID", "deepseek-flash"),
            "model_mode": mode, "thinking": "disabled", "max_output_tokens": 512}
    if graph_version in {"readonly-diagnosis-rag-v1", "clarification-diagnosis-v1", "grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"}:
        from apps.supportflow.retrieval.contracts import CHUNKER_VERSION, DIMENSION, MODEL_ID, MODEL_REVISION
        snapshot["retrieval"] = {"model_id": MODEL_ID, "model_revision": MODEL_REVISION, "dimension": DIMENSION,
            "chunker_version": CHUNKER_VERSION, "strategy": "hybrid_rrf", "vector_k": 20, "lexical_k": 20,
            "final_k": 5, "rrf_constant": 60}
    knowledge_mode = os.environ.get("SF_KNOWLEDGE_QUESTION_MODE", "disabled")
    if knowledge_mode not in {"enabled", "disabled"} or (knowledge_mode == "enabled" and "retrieval" not in snapshot):
        raise ValueError("Invalid knowledge question mode")
    if knowledge_mode == "enabled":
        snapshot["knowledge_question_contract_version"] = 1
        snapshot["knowledge_search_contract_version"] = 1
    roles_mode = os.environ.get("SF_KNOWLEDGE_ROLES_MODE", "disabled")
    if roles_mode not in {"enabled", "disabled"} or roles_mode == "enabled" and knowledge_mode != "enabled":
        raise ValueError("Knowledge roles require the knowledge contract")
    if roles_mode == "enabled":
        snapshot["knowledge_roles_contract_version"] = 1
    capacity_mode = os.environ.get("SF_CAPACITY_DIAGNOSIS_MODE", "disabled")
    if capacity_mode not in {"enabled", "disabled"} or capacity_mode == "enabled" and knowledge_mode != "enabled":
        raise ValueError("Capacity diagnosis requires the knowledge contract")
    if capacity_mode == "enabled":
        snapshot["capacity_diagnosis_contract_version"] = 1
    diagnosis_mode = os.environ.get("SF_DIAGNOSIS_ROUTING_MODE", "disabled")
    if diagnosis_mode not in {"enabled", "disabled"} or diagnosis_mode == "enabled" and capacity_mode != "enabled":
        raise ValueError("Diagnosis routing requires capacity and knowledge contracts")
    if diagnosis_mode == "enabled":
        snapshot["diagnosis_routing_contract_version"] = 1
    if graph_version in {"grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"}:
        snapshot["grant_contract_version"] = 1
    if graph_version == "grant-approval-v1":
        snapshot.update(approval_contract_version=1, tool_round_limit=7)
    if graph_version == "grant-execution-v1":
        snapshot.update(approval_contract_version=1, execution_contract_version=2, tool_round_limit=10)
        reply_mode = os.environ.get("SF_GRANT_REPLY_MODE", "deterministic")
        if reply_mode not in {"deterministic", "assisted", "streamed"}:
            raise ValueError("Invalid grant reply mode")
        if reply_mode != "deterministic":
            snapshot["grant_reply_mode"] = reply_mode
        if reply_mode == "streamed":
            snapshot["public_reply_contract_version"] = 1
    policy_reply_mode = os.environ.get("SF_GRANT_POLICY_REPLY_MODE", "disabled")
    if policy_reply_mode not in {"enabled", "disabled"} or policy_reply_mode == "enabled" and graph_version != "grant-execution-v1":
        raise ValueError("Policy replies require the execution graph")
    if policy_reply_mode == "enabled":
        snapshot["grant_policy_reply_contract_version"] = 1
    if graph_version in {"grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"}:
        snapshot["input_refusal_policy_version"] = 1
    return snapshot


def from_snapshot(config):
    if (config.get("provider") != "deepseek" or config.get("base_url") != BASE_URL
            or config.get("model_mode") != "live" or config.get("thinking") != "disabled"):
        raise ModelError("MODEL_PROTOCOL_ERROR")
    path = Path(os.environ.get("SF_MODEL_KEY_FILE", "/run/secrets/deepseek_api_key"))
    return DeepSeekModel(config["model_id"], path.read_text(encoding="ascii").strip(),
                         max_tokens=config.get("max_output_tokens", 512))


def unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ModelError("MODEL_PROTOCOL_ERROR")
        result[key] = value
    return result


def read_usage(raw):
    if raw is None:
        return None
    if not isinstance(raw, dict):
        raise ModelError("MODEL_PROTOCOL_ERROR")
    fields = {"input_tokens": "prompt_tokens", "output_tokens": "completion_tokens", "total_tokens": "total_tokens",
              "cache_hit_input_tokens": "prompt_cache_hit_tokens", "cache_miss_input_tokens": "prompt_cache_miss_tokens"}
    result = {target: raw.get(source) for target, source in fields.items()}
    if any(value is not None and (type(value) is not int or value < 0) for value in result.values()):
        raise ModelError("MODEL_PROTOCOL_ERROR")
    return result


def protocol_boundary(method):
    """Keep already observed accounting when parsing or transport fails."""
    @wraps(method)
    def wrapped(self, *args, **kwargs):
        previous = len(self.audit)
        try:
            return method(self, *args, **kwargs)
        except (ModelError, ValueError, KeyError, TypeError, AttributeError) as error:
            code = error.code if isinstance(error, ModelError) else 'MODEL_PROTOCOL_ERROR'
            audit = self.audit[-1] if len(self.audit) > previous else {}
            if audit:
                audit['error_code'] = code
            raise ModelError(code, status=getattr(error, 'status', None),
                             usage=audit.get('usage'),
                             provider_response_id=audit.get('provider_response_id')) from None
    return wrapped


async def _await_with_ticks(awaitable, tick, *, close_response_on_abort=False):
    if tick is None:
        return await awaitable
    task = asyncio.ensure_future(awaitable)
    try:
        while True:
            ready, _ = await asyncio.wait((task,), timeout=PUBLIC_REPLY_TICK_SECONDS)
            tick()
            if ready:
                return task.result()
    except BaseException:
        task.cancel()
        # Collect a raced transport result without replacing the original
        # cancellation or application gate error.
        result, = await asyncio.gather(task, return_exceptions=True)
        if close_response_on_abort and isinstance(result, httpx.Response):
            with suppress(httpx.HTTPError):
                await result.aclose()
        raise


async def _public_lines(response, tick):
    if response.headers.get("Content-Encoding", "identity").lower() != "identity":
        raise ModelError("MODEL_PROTOCOL_ERROR")
    iterator = response.aiter_bytes()
    pending = b""
    consumed = 0
    try:
        while True:
            try:
                chunk = await _await_with_ticks(anext(iterator), tick)
            except StopAsyncIteration:
                break
            consumed += len(chunk)
            if consumed > MAX_WIRE_BYTES:
                raise ModelError("MODEL_PROTOCOL_ERROR")
            lines = (pending + chunk).split(b"\n")
            pending = lines.pop()
            for line in lines:
                yield line.rstrip(b"\r").decode("utf-8")
        if pending:
            yield pending.rstrip(b"\r").decode("utf-8")
    finally:
        await iterator.aclose()


class _PublicText:
    def __init__(self, allowed_texts, on_text, tick):
        self.allowed_texts, self.on_text, self.tick = allowed_texts, on_text, tick
        self.text = ""

    def consume(self, event, audit, *, elapsed_ms):
        choices = event.get("choices")
        if ("error" in event or not isinstance(choices, list) or len(choices) > 1
                or not choices and event.get("usage") is None):
            raise ModelError("MODEL_PROTOCOL_ERROR")
        if not choices:
            return
        choice = choices[0]
        index, finish = choice.get("index"), choice.get("finish_reason")
        delta = choice.get("delta")
        if (type(index) is not int or index != 0 or not isinstance(delta, dict)
                or audit["finish_reason"] is not None):
            raise ModelError("MODEL_PROTOCOL_ERROR")
        if finish not in (None, "stop"):
            if finish in ("length", "content_filter", "tool_calls"):
                audit["finish_reason"] = finish
            raise ModelError("MODEL_PROTOCOL_ERROR")
        if (delta.get("tool_calls") not in (None, []) or delta.get("function_call") is not None
                or delta.get("reasoning_content") not in (None, "")):
            raise ModelError("MODEL_PROTOCOL_ERROR")
        if delta.get("role") not in (None, "assistant"):
            raise ModelError("MODEL_PROTOCOL_ERROR")
        content = delta.get("content")
        content = "" if content is None else content
        if not isinstance(content, str):
            raise ModelError("MODEL_PROTOCOL_ERROR")
        candidate = self.text + content
        if not any(allowed.startswith(candidate) for allowed in self.allowed_texts):
            raise ModelError("MODEL_PROTOCOL_ERROR")
        if finish == "stop" and candidate not in self.allowed_texts:
            raise ModelError("MODEL_PROTOCOL_ERROR")
        audit["finish_reason"] = finish
        if content:
            self.text = candidate
            audit["accepted_chunks"] += 1
            audit["accepted_characters"] = len(candidate)
            if audit["first_content_ms"] is None:
                audit["first_content_ms"] = elapsed_ms
            audit["last_content_ms"] = elapsed_ms
            self.on_text(content)


class DeepSeekModel:
    def __init__(self, model_id, key, *, max_tokens=512, transport=None, input_preflight=None):
        if not isinstance(model_id, str) or not model_id or not key or not 1 <= max_tokens <= 512:
            raise ModelError("MODEL_PROTOCOL_ERROR")
        self.model_id, self._key, self.max_tokens = model_id, key, max_tokens
        self.transport = transport  # Explicit dependency injection for offline wire tests only.
        self.audit = []
        self.input_preflight = input_preflight

    def _reject_secret(self, value):
        if self._key in json.dumps(value, ensure_ascii=False):
            # Split deltas may otherwise reconstruct a credential even though no
            # individual chunk contains it. Discard that unsafe protocol trace.
            self.audit[-1]['wire_events'] = []
            self.audit[-1]['provider_response_id'] = None
            raise ModelError('MODEL_PROTOCOL_ERROR')

    def _stream(self, messages, timeout, **options):
        # The graph and its fixed PostgreSQL connection stay synchronous. Only this
        # HTTP operation enters an event loop, so cancellation can enforce a total
        # deadline even when a provider stalls between streamed chunks.
        return asyncio.run(self._stream_async(messages, timeout, **options))

    async def _stream_async(self, messages, timeout, *, public_reply=None, **options):
        payload = {"model": self.model_id, "messages": convert_to_openai_messages(messages),
                   "thinking": {"type": "disabled"}, "max_tokens": self.max_tokens,
                   "stream": True, "stream_options": {"include_usage": True}, **options}
        encoded = json.dumps(payload, ensure_ascii=False)
        # Conservative byte ceiling until a matched provider tokenizer is verified.
        # This does not claim exact pre-request model token accounting (SF-T023).
        precheck = None
        if self.input_preflight is not None:
            try:
                precheck = self.input_preflight(payload)
            except ValueError as error:
                raise ModelError('MODEL_INPUT_BUDGET') from error
            if (type(precheck.get('input_tokens')) is not int or not 0 <= precheck['input_tokens'] <= 8192
                    or precheck.get('limit') != 8192 or precheck.get('allowed') is not True):
                raise ModelError('MODEL_INPUT_BUDGET')
        if (precheck is None and len(encoded.encode('utf-8')) > 12000) or self._key in encoded:
            raise ModelError("MODEL_INPUT_BUDGET")
        if timeout <= 0:
            raise ModelError("MODEL_TIMEOUT")
        started = monotonic()
        audit = {"requested_model": self.model_id, "wire_events": [], "http_status": None,
                 "duration_ms": None, "error_code": None, "usage": None, "provider_response_id": None}
        if precheck is not None:
            audit['input_preflight'] = precheck
        if public_reply is not None:
            audit.update(kind="public_reply", max_output_tokens=payload["max_tokens"], timeout_seconds=timeout,
                         accepted_chunks=0, accepted_characters=0, sse_data_frames=0, finish_reason=None,
                         done_seen=False, first_content_ms=None, last_content_ms=None)
        self.audit.append(audit)
        events, bytes_read, complete, event_count = [], 0, False, 0
        try:
            async with asyncio.timeout(min(timeout, 20)), httpx.AsyncClient(trust_env=False, transport=self.transport,
                              timeout=httpx.Timeout(min(timeout, 20), connect=min(timeout, 5))) as client, AsyncExitStack() as stack:
                headers = {"Authorization": "Bearer " + self._key}
                url = BASE_URL + '/chat/completions'
                if public_reply is None:
                    response = await stack.enter_async_context(client.stream("POST", url, json=payload, headers=headers))
                    lines = response.aiter_lines()
                else:
                    headers.update({"Accept": "text/event-stream", "Accept-Encoding": "identity"})
                    request = client.build_request("POST", url, json=payload, headers=headers)
                    response = await _await_with_ticks(client.send(request, stream=True), public_reply.tick,
                                                       close_response_on_abort=True)
                    stack.push_async_callback(response.aclose)
                    lines = _public_lines(response, public_reply.tick)
                audit['http_status'] = response.status_code
                if response.status_code != 200:
                    code = {401: 'MODEL_AUTH_ERROR', 402: 'MODEL_QUOTA_ERROR', 429: 'MODEL_RATE_LIMIT'}.get(
                        response.status_code, 'MODEL_UPSTREAM_ERROR')
                    raise ModelError(code, status=response.status_code)
                stack.push_async_callback(lines.aclose)
                async for line in lines:
                    if monotonic() - started > timeout:
                        raise ModelError('MODEL_TIMEOUT')
                    bytes_read += len(line.encode('utf-8'))
                    self._reject_secret(line)
                    if bytes_read > MAX_WIRE_BYTES:
                        raise ModelError('MODEL_PROTOCOL_ERROR')
                    if not line.startswith('data:'):
                        continue
                    value = line[5:].strip()
                    if value == '[DONE]':
                        complete = True
                        break
                    if event_count >= 4096:
                        raise ModelError('MODEL_PROTOCOL_ERROR')
                    event = json.loads(value, object_pairs_hook=unique_object)
                    self._reject_secret(event)
                    event_count += 1
                    if public_reply is None:
                        events.append(event)
                        # Private callers keep the existing selective protocol audit.
                        safe = {k: event[k] for k in ('id', 'model', 'usage') if k in event}
                        safe['choices'] = [{"index": c.get('index'), "finish_reason": c.get('finish_reason'),
                            "delta": {k: v for k, v in c.get('delta', {}).items() if k in ('content', 'tool_calls', 'role')}}
                            for c in event.get('choices', [])]
                        audit['wire_events'].append(safe)
                    provider_id = event.get('id')
                    if public_reply is not None and provider_id is not None:
                        if (not isinstance(provider_id, str) or not 1 <= len(provider_id) <= 256
                                or audit['provider_response_id'] not in (None, provider_id)):
                            raise ModelError('MODEL_PROTOCOL_ERROR')
                    if provider_id and audit['provider_response_id'] is None:
                        audit['provider_response_id'] = provider_id
                    if event.get('usage') is not None:
                        audit['usage'] = read_usage(event['usage'])
                    if public_reply is not None:
                        audit["sse_data_frames"] = event_count
                        public_reply.consume(event, audit, elapsed_ms=(monotonic() - started) * 1000)
            if public_reply is not None:
                audit["done_seen"] = complete
            if not complete or not event_count:
                raise ModelError('MODEL_PROTOCOL_ERROR')
            if precheck is not None:
                actual = (audit.get('usage') or {}).get('input_tokens')
                audit['input_token_count_matches'] = None if actual is None else actual == precheck['input_tokens']
                if audit['input_token_count_matches'] is False:
                    raise ModelError('MODEL_PROTOCOL_ERROR')
            if public_reply is not None:
                if audit["finish_reason"] != "stop" or public_reply.text not in public_reply.allowed_texts:
                    raise ModelError("MODEL_PROTOCOL_ERROR")
                return events, audit
            audit['provider_response_id'] = next((e['id'] for e in events if e.get('id')), None)
            usage = next((e['usage'] for e in reversed(events) if e.get('usage') is not None), None)
            audit['usage'] = read_usage(usage)
            return events, audit
        except (httpx.TimeoutException, TimeoutError):
            audit['error_code'] = 'MODEL_TIMEOUT'
            raise ModelError('MODEL_TIMEOUT') from None
        except httpx.HTTPError:
            audit['error_code'] = 'MODEL_NETWORK_ERROR'
            raise ModelError('MODEL_NETWORK_ERROR') from None
        except (ValueError, KeyError, TypeError, AttributeError):
            audit['error_code'] = 'MODEL_PROTOCOL_ERROR'
            raise ModelError('MODEL_PROTOCOL_ERROR') from None
        except ModelError as error:
            audit['error_code'] = error.code
            raise
        except (StopFragment, StaleFragment) as error:
            if public_reply is not None:
                audit['error_code'] = getattr(error, 'reason_code', 'STALE_FRAGMENT')
                error.usage, error.provider_response_id = audit['usage'], audit['provider_response_id']
            raise
        finally:
            audit['duration_ms'] = (monotonic() - started) * 1000

    @protocol_boundary
    def stream_public_text(self, messages, timeout, *, allowed_texts, on_text, tick=None):
        """Deliver only allowlisted plain-text prefixes as the wire arrives."""
        if (not isinstance(allowed_texts, tuple) or not 1 <= len(allowed_texts) <= 4
                or any(not isinstance(text, str) or not 1 <= len(text) <= 12000
                       or "\0" in text or self._key in text for text in allowed_texts)
                or not callable(on_text) or tick is not None and not callable(tick)
                or type(timeout) not in (float, int) or not math.isfinite(timeout)):
            raise ModelError("MODEL_PROTOCOL_ERROR")
        for text in allowed_texts:
            text.encode("utf-8")
        reply = _PublicText(allowed_texts, on_text, tick)
        _, audit = self._stream(messages, min(timeout, PUBLIC_REPLY_TIMEOUT_SECONDS), public_reply=reply,
                                max_tokens=min(self.max_tokens, PUBLIC_REPLY_TOKENS))
        return {'content': reply.text, 'usage': audit['usage'], 'provider_response_id': audit['provider_response_id']}

    @protocol_boundary
    def tool_call(self, messages, tool_schema, timeout):
        tool = json.loads(json.dumps(tool_schema))
        tool['function'].pop('strict', None)  # Standard endpoint; strict args are enforced by our Pydantic boundary.
        events, audit = self._stream(messages, timeout, tools=[tool], tool_choice='auto')
        calls, finish = {}, None
        for event in events:
            for choice in event.get('choices', []):
                if choice.get('index') != 0:
                    raise ModelError('MODEL_PROTOCOL_ERROR')
                finish = choice.get('finish_reason') or finish
                for raw in choice.get('delta', {}).get('tool_calls') or []:
                    if raw.get('index') != 0:
                        raise ModelError('MODEL_PROTOCOL_ERROR')
                    call = calls.setdefault(0, {'id': '', 'name': '', 'args': ''})
                    fn = raw.get('function', {})
                    for field, value in (('id', raw.get('id')), ('name', fn.get('name'))):
                        if value:
                            if call[field] and call[field] != value:
                                raise ModelError('MODEL_PROTOCOL_ERROR')
                            call[field] = value
                    call['args'] += fn.get('arguments') or ''
                    if len(call['args'].encode('utf-8')) > 8192:
                        raise ModelError('MODEL_PROTOCOL_ERROR')
        if len(calls) != 1 or finish != 'tool_calls':
            raise ModelError('MODEL_PROTOCOL_ERROR', usage=audit['usage'], provider_response_id=audit['provider_response_id'])
        call = calls[0]
        self._reject_secret(call)
        if not call['id'] or call['name'] != tool['function']['name']:
            raise ModelError('MODEL_PROTOCOL_ERROR')
        try:
            call['args'] = json.loads(call['args'], object_pairs_hook=unique_object)
            self._reject_secret(call)
            if not isinstance(call['args'], dict): raise ValueError()
        except (ValueError, TypeError):
            raise ModelError('MODEL_PROTOCOL_ERROR') from None
        return {'call': call, 'usage': audit['usage'], 'provider_response_id': audit['provider_response_id']}

    @protocol_boundary
    def structured(self, messages, schema, timeout):
        # JSON Output is syntax-only; the caller validates the independently defined schema and facts.
        wire = convert_to_openai_messages(messages)
        wire.insert(0, {'role': 'system', 'content': 'Return only a JSON object matching this JSON Schema: '
                       + json.dumps(schema, ensure_ascii=False)})
        events, audit = self._stream(wire, timeout, response_format={'type': 'json_object'})
        text, finish = '', None
        for event in events:
            for choice in event.get('choices', []):
                if choice.get('index') != 0 or choice.get('delta', {}).get('tool_calls'):
                    raise ModelError('MODEL_PROTOCOL_ERROR')
                text += choice.get('delta', {}).get('content') or ''
                finish = choice.get('finish_reason') or finish
        try:
            self._reject_secret(text)
            result = json.loads(text, object_pairs_hook=unique_object)
            self._reject_secret(result)
            if finish != 'stop' or not isinstance(result, dict): raise ValueError()
        except (ValueError, TypeError):
            raise ModelError('MODEL_PROTOCOL_ERROR', usage=audit['usage'], provider_response_id=audit['provider_response_id']) from None
        return {'content': result, 'usage': audit['usage'], 'provider_response_id': audit['provider_response_id']}
