"""Read approved GitHub Issues as external clues under one deadline."""
import asyncio
from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import logging
import math
import os
from pathlib import Path
import re
from time import monotonic
from typing import Annotated, Literal

import httpx
from pydantic import AwareDatetime, Field, ValidationError

from apps.supportflow.domain.contracts import ErrorCode, StrictModel, ToolEnvelope
from apps.supportflow.domain.github_access import (
    ExternalIssuesInput,
    allowed_repositories_from_environment,
    normalize_allowlist,
    valid_identity,
)
from .readonly import MAX_BYTES, TIMEOUT_SECONDS, compact, failure, unique_object


SOURCE = "github"
PAGE_SIZE = 10
MAX_PAGES = 5
MAX_ISSUES = PAGE_SIZE * MAX_PAGES
API_VERSION = "2022-11-28"
API_ORIGIN = "https://api.github.com"
WEB_ORIGIN = "https://github.com"
_IDENTITY_FIELDS = ("tenant_id", "actor_id", "customer_id", "run_id")
_TRANSIENT_STATUSES = frozenset({500, 502, 503, 504})
_LOGGER = logging.getLogger(__name__)


class ExternalIssue(StrictModel):
    number: Annotated[int, Field(gt=0, le=2**31 - 1)]
    state: Literal["open", "closed"]
    title: Annotated[str, Field(min_length=1, max_length=256)]
    body_excerpt: Annotated[str, Field(max_length=600)] | None
    url: Annotated[str, Field(max_length=192)]
    created_at: AwareDatetime
    updated_at: AwareDatetime
    closed_at: AwareDatetime | None


class ExternalIssues(StrictModel):
    owner: str
    repo: str
    issues: Annotated[list[ExternalIssue], Field(max_length=MAX_ISSUES)]
    has_more: bool
    trust: Literal["external_unverified"] = "external_unverified"
    trusted_for_policy: Literal[False] = False


@dataclass(frozen=True)
class PageRead:
    status: int | None
    response_bytes: int = 0
    issues: tuple[ExternalIssue, ...] = ()
    has_more: bool = False
    error: ErrorCode | None = None
    retryable: bool = False
    retry_after: float = 0.0


def _retry_delay(headers: httpx.Headers, *, limited: bool) -> float:
    delays = []
    retry_after = headers.get("Retry-After")
    if retry_after is not None:
        if re.fullmatch(r"[0-9]{1,10}", retry_after) is None:
            return math.inf
        delays.append(float(retry_after))
    if limited and headers.get("X-RateLimit-Remaining") == "0":
        reset = headers.get("X-RateLimit-Reset", "")
        if re.fullmatch(r"[0-9]{1,10}", reset) is None:
            return math.inf
        delays.append(max(0.0, float(reset) - datetime.now(timezone.utc).timestamp()))
    # GitHub asks clients to wait at least a minute for an unspecified limit.
    return max(delays) if delays else 60.0 if limited else 0.05


def _response_failure(status: int, headers: httpx.Headers) -> PageRead:
    limited = status == 429 or status == 403 and (
        headers.get("X-RateLimit-Remaining") == "0" or "Retry-After" in headers
    )
    if limited:
        return PageRead(status, error="RATE_LIMIT", retryable=True, retry_after=_retry_delay(headers, limited=True))
    if status in {401, 403}:
        return PageRead(status, error="FORBIDDEN")
    if status == 404:
        return PageRead(status, error="NOT_FOUND")
    if status == 422:
        return PageRead(status, error="VALIDATION")
    if status in _TRANSIENT_STATUSES:
        return PageRead(status, error="UPSTREAM_UNAVAILABLE", retryable=True, retry_after=_retry_delay(headers, limited=False))
    return PageRead(status, error="UPSTREAM_UNAVAILABLE")


def _reject_constant(_value):
    raise ValueError("Invalid upstream JSON constant")


def _text(value: str, token: str, limit: int) -> str:
    if not isinstance(value, str):
        raise ValueError("Invalid upstream issue text")
    return value.replace(token, "[redacted]").replace("\x00", "").strip()[:limit]


def _issue(row: dict, repository: str, token: str) -> ExternalIssue:
    number = row["number"]
    if type(number) is not int:
        raise ValueError("Invalid upstream issue number")
    excerpt = row.get("body")
    projected = {
        "number": number,
        "state": row["state"],
        "title": _text(row["title"], token, 256),
        "body_excerpt": _text(excerpt, token, 600) if excerpt is not None else None,
        "url": f"{WEB_ORIGIN}/{repository}/issues/{number}",
        "created_at": row["created_at"],
        "updated_at": row["updated_at"],
        "closed_at": row["closed_at"],
    }
    return ExternalIssue.model_validate_json(compact(projected))


class GitHubIssuesClient:
    def __init__(self, allowed_repositories: Mapping, token: str | None, *, transport=None):
        self._allowlist = normalize_allowlist(allowed_repositories)
        if token is not None and (not isinstance(token, str) or re.fullmatch(r"[!-~]{1,512}", token) is None):
            raise ValueError("Invalid GitHub credential")
        self._token = token
        self._transport = transport

    @classmethod
    def from_environment(cls):
        allowed = allowed_repositories_from_environment()
        token = None
        path = os.environ.get("SF_GITHUB_TOKEN_FILE", "/run/secrets/github_token")
        if allowed and path:
            try:
                with Path(path).open("rb") as source:
                    raw = source.read(1025)
                if len(raw) <= 1024:
                    candidate = raw.decode("ascii").strip()
                    if re.fullmatch(r"[!-~]{1,512}", candidate):
                        token = candidate
            except (OSError, UnicodeError, ValueError):
                # Credential storage errors surface as dependency unavailability.
                pass
        return cls(allowed, token)

    def repositories_for(self, tenant_id: str) -> tuple[str, ...]:
        return self._allowlist.get(tenant_id, ())

    def list_issues(
        self, query: ExternalIssuesInput | dict, context: Mapping, *,
        timeout: float = TIMEOUT_SECONDS, observe: Callable[[dict], None] | None = None,
        before_request: Callable[[], object] | None = None,
    ) -> ToolEnvelope:
        try:
            query = ExternalIssuesInput.model_validate(query)
        except ValidationError:
            return failure("VALIDATION", SOURCE)
        if not isinstance(context, Mapping) or not all(valid_identity(context.get(key)) for key in _IDENTITY_FIELDS):
            return failure("FORBIDDEN", SOURCE)
        if query.repository not in self.repositories_for(context["tenant_id"]):
            return failure("FORBIDDEN", SOURCE)
        if self._token is None:
            return failure("UPSTREAM_UNAVAILABLE", SOURCE)
        if isinstance(timeout, bool) or not isinstance(timeout, (float, int)) or not math.isfinite(timeout):
            return failure("VALIDATION", SOURCE)
        if timeout <= 0:
            return failure("TIMEOUT", SOURCE, True)
        deadline = monotonic() + min(timeout, TIMEOUT_SECONDS)
        return asyncio.run(self._read(query, deadline, observe, before_request))

    async def _read(self, query, deadline, observe, before_request):
        issues = {}
        remaining_bytes = MAX_BYTES
        retry_available = True
        headers = {
            "Accept": "application/vnd.github+json",
            "Accept-Encoding": "identity",
            "Authorization": f"Bearer {self._token}",
            "User-Agent": "SupportFlow/0.0.1",
            "X-GitHub-Api-Version": API_VERSION,
        }
        async with httpx.AsyncClient(
            headers=headers, follow_redirects=False, trust_env=False, transport=self._transport,
        ) as client:
            for page in range(1, MAX_PAGES + 1):
                url = httpx.URL(
                    f"{API_ORIGIN}/repos/{query.repository}/issues",
                    params={"state": query.state, "per_page": PAGE_SIZE, "page": page},
                )
                while True:
                    if before_request is not None:
                        before_request()
                    started = monotonic()
                    remaining = deadline - started
                    if remaining <= 0:
                        return failure("TIMEOUT", SOURCE, True)
                    response = await self._page(client, url, query.repository, remaining, remaining_bytes)
                    observed = {
                        "method": "GET", "url": str(url), "page": page, "status": response.status,
                        "response_bytes": response.response_bytes, "observed_at": datetime.now(timezone.utc).isoformat(),
                        "duration_ms": (monotonic() - started) * 1000, "error_code": response.error,
                    }
                    _LOGGER.info("github_issue_read %s", json.dumps(observed, separators=(",", ":")))
                    if observe is not None:
                        observe(observed)
                    remaining_bytes -= response.response_bytes
                    if response.error is None:
                        break
                    if not (retry_available and response.retryable and response.retry_after < deadline - monotonic()):
                        return failure(response.error, SOURCE, response.retryable)
                    retry_available = False
                    await asyncio.sleep(response.retry_after)
                for issue in response.issues:
                    issues.setdefault(issue.number, issue)
                if not response.has_more:
                    break
        result = ExternalIssues(owner=query.owner, repo=query.repo, issues=list(issues.values()), has_more=response.has_more)
        envelope = ToolEnvelope(ok=True, data=result.model_dump(mode="json"), error_code=None, retryable=False,
                                source=SOURCE, observed_at=datetime.now(timezone.utc))
        if len(compact(envelope.model_dump(mode="json"))) > MAX_BYTES:
            return failure("RESULT_TOO_LARGE", SOURCE)
        return envelope

    async def _page(self, client, url, repository, timeout, max_bytes):
        status = None
        seen = 0
        try:
            limits = httpx.Timeout(timeout, connect=min(timeout, 3))
            async with asyncio.timeout(timeout), client.stream("GET", url, timeout=limits) as response:
                status = response.status_code
                if status != 200:
                    return _response_failure(status, response.headers)
                if response.headers.get("Content-Encoding", "identity").lower() != "identity":
                    return PageRead(status, error="UPSTREAM_UNAVAILABLE")
                body = bytearray()
                async for chunk in response.aiter_bytes(chunk_size=8192):
                    seen += len(chunk)
                    if seen > max_bytes:
                        return PageRead(status, seen, error="RESULT_TOO_LARGE")
                    body.extend(chunk)
                payload = json.loads(body, object_pairs_hook=unique_object, parse_constant=_reject_constant)
                if not isinstance(payload, list) or not all(isinstance(row, dict) for row in payload):
                    return PageRead(status, seen, error="UPSTREAM_UNAVAILABLE")
                if len(payload) > PAGE_SIZE:
                    return PageRead(status, seen, error="RESULT_TOO_LARGE")
                issues = tuple(_issue(row, repository, self._token) for row in payload if "pull_request" not in row)
                # Only rel=next is used as a continuation hint. The URL itself
                # is never followed or retained.
                link = response.headers.get("Link", "")
                if len(link) > 8192:
                    return PageRead(status, seen, error="RESULT_TOO_LARGE")
                return PageRead(status, seen, issues, "next" in response.links)
        except (httpx.TimeoutException, TimeoutError):
            return PageRead(status, seen, error="TIMEOUT", retryable=True, retry_after=0.05)
        except (httpx.NetworkError, httpx.RemoteProtocolError):
            return PageRead(status, seen, error="UPSTREAM_UNAVAILABLE", retryable=True, retry_after=0.05)
        except (httpx.HTTPError, KeyError, ValueError, TypeError, RecursionError):
            return PageRead(status, seen, error="UPSTREAM_UNAVAILABLE")
