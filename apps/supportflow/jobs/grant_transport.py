"""Private, bounded grant HTTP. No write tool and no automatic POST retry."""
import asyncio
from dataclasses import dataclass
from datetime import datetime
import json
from time import monotonic_ns
from typing import Annotated, Literal
from urllib.parse import quote

import httpx
from pydantic import AwareDatetime, Field, field_validator, model_validator

from apps.supportflow.domain.contracts import StrictModel, Subscription
from apps.supportflow.domain.grant_commands import GrantCommand, InternalGrantReceipt
from apps.supportflow.tools.readonly import SandboxClient, compact, unique_object, MAX_BYTES


class ActionEnvelope(StrictModel):
    ok: bool
    data: dict | None
    error_code: Annotated[str, Field(min_length=1, max_length=64, pattern=r'^[A-Z][A-Z0-9_]*$')] | None
    retryable: bool
    source: str
    observed_at: AwareDatetime
    schema_version: Literal[1]

    @field_validator('schema_version', mode='before')
    @classmethod
    def integer_schema(cls, value):
        if type(value) is not int:
            raise ValueError('Expected integer schema')
        return value

    @model_validator(mode='after')
    def consistent(self):
        if self.ok and (self.data is None or self.error_code is not None or self.retryable):
            raise ValueError('Invalid success envelope')
        if not self.ok and (self.data is not None or self.error_code is None):
            raise ValueError('Invalid failure envelope')
        return self


@dataclass(frozen=True)
class TransportResult:
    status: int | None
    envelope: ActionEnvelope | None
    error_code: str | None


def observe_http(record):
    """Production noop; isolated collectors may record this secret-free boundary."""


def bound_receipt(command, value):
    receipt = InternalGrantReceipt.model_validate_json(compact(value))
    receipt.validate_binding(command)
    if any(getattr(receipt, key) != getattr(command, key) for key in
           ('proposal_id', 'authorization_id', 'authorized_at')):
        raise ValueError('Receipt authorization differs from the unique app intent')
    return receipt


class GrantClient:
    def __init__(self, *, token=None, base_url=None, transport=None):
        # Reuse only credential/fixed-URL validation, never the read retry loop.
        validated = SandboxClient(token=token, base_url=base_url, transport=transport)
        self.base_url, self._token, self.transport = validated.base_url, validated._token, transport

    def _call(self, kind, method, path, command, context, *, timeout, body=None, query=None):
        maximum = 2 if kind == 'receipt' else 8
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not 0 < timeout <= maximum:
            raise ValueError('Invalid bounded transport timeout')
        return asyncio.run(self._request(kind, method, path, command, context, timeout, body, query))

    async def _request(self, kind, method, path, command, context, timeout, body, query):
        headers = {'Authorization': 'Bearer '+self._token, 'X-SupportFlow-Tenant': command.tenant_id,
                   'X-SupportFlow-Actor': context['actor_id'], 'X-SupportFlow-Run': context['run_id'],
                   'Accept': 'application/json', 'Accept-Encoding': 'identity'}
        source = {'grant':'sandbox.temporary_grants', 'receipt':'sandbox.action_receipts',
                  'subscription':'sandbox.subscriptions'}[kind]
        started = monotonic_ns()
        status, envelope, error = None, None, None
        try:
            async with asyncio.timeout(timeout), httpx.AsyncClient(trust_env=False, follow_redirects=False,
                    transport=self.transport, timeout=httpx.Timeout(timeout, connect=min(timeout, 2))) as client:
                async with client.stream(method, self.base_url+path, headers=headers, json=body, params=query) as response:
                    status = response.status_code
                    if (response.headers.get('content-encoding', 'identity').lower() != 'identity'
                            or response.headers.get('content-type','').split(';',1)[0].strip().lower() != 'application/json'):
                        raise ValueError('Invalid envelope encoding')
                    raw = bytearray()
                    async for chunk in response.aiter_raw(chunk_size=8192):
                        if len(raw)+len(chunk)>MAX_BYTES:
                            raise ValueError('Response exceeds boundary')
                        raw.extend(chunk)
                    decoded = json.loads(bytes(raw), object_pairs_hook=unique_object,
                        parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Invalid number')))
                    if self._token.encode() in compact(decoded):
                        raise ValueError('Secret echo')
                    envelope = ActionEnvelope.model_validate_json(compact(decoded))
                    if envelope.source != source:
                        raise ValueError('Unexpected source')
                    if status in (200, 201):
                        if not envelope.ok or status == 201 and kind != 'grant':
                            raise ValueError('Invalid success status')
                        if kind in ('grant','receipt'):
                            bound_receipt(command, envelope.data)
                        else:
                            sub = Subscription.model_validate_json(compact(envelope.data))
                            if sub.subscription_id != command.subscription_id:
                                raise ValueError('Wrong subscription')
                    elif envelope.ok or envelope.retryable and kind == 'grant':
                        raise ValueError('Invalid rejection')
        except (httpx.TimeoutException, TimeoutError):
            error = 'TIMEOUT'
        except httpx.HTTPError:
            error = 'UPSTREAM_UNAVAILABLE'
        except (ValueError, TypeError, RecursionError):
            error, envelope = 'PROTOCOL_ERROR', None
        finally:
            import os
            observe_http({'kind':kind, 'method':method, 'path':path,
                'run_id':context['run_id'],
                'action_id':command.action_id,
                'scope':{'tenant_id':command.tenant_id,'customer_id':command.customer_id,
                         'subscription_id':command.subscription_id,'authentication':'service'},
                'request_body':body, 'query_pairs':list((query or {}).items()),
                'started_monotonic_ns':started, 'ended_monotonic_ns':monotonic_ns(),
                'timeout_seconds':timeout, 'status':status,
                'response_json':envelope.model_dump(mode='json') if envelope is not None else None,
                'transport_error':error, 'execution_generation':context['execution_generation'], 'worker_pid':os.getpid()})
        return TransportResult(status, envelope, error)

    def grant(self, command: GrantCommand, context, *, timeout):
        return self._call('grant','POST','/internal/v1/subscriptions/'+quote(command.subscription_id,safe='')+
            '/temporary-seat-grants',command,context,timeout=timeout,body=command.model_dump(mode='json'))

    def receipt(self, command: GrantCommand, context, *, timeout=2):
        return self._call('receipt','GET','/internal/v1/actions/'+quote(command.action_id,safe=''),command,context,
            timeout=timeout,query={key:getattr(command,key) for key in
                                 ('customer_id','subscription_id','canonical_args_hash')})

    def subscription(self, command: GrantCommand, context, *, timeout):
        return self._call('subscription','GET','/api/v1/customers/'+quote(command.customer_id,safe='')+
                         '/subscription',command,context,timeout=timeout)
