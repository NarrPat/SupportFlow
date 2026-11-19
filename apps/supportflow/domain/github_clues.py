"""Freeze the operator-selected external repository and project unverified clues."""
import json
from typing import Annotated, Literal

from pydantic import AwareDatetime, Field, ValidationError

from .contracts import ErrorCode, StrictModel, ToolEnvelope
from .github_access import allowed_repositories_from_environment


TOOL_ID = "list_external_issues"
SNAPSHOT_KEY = "external_issues"
GRAPH_VERSIONS = frozenset({
    "readonly-diagnosis-rag-v1", "clarification-diagnosis-v1", "grant-proposal-v1",
    "grant-approval-v1", "grant-execution-v1",
})


class ExternalIssueClue(StrictModel):
    number: Annotated[int, Field(ge=1)]
    state: Literal["open", "closed"]
    title: Annotated[str, Field(min_length=1, max_length=256)]
    url: str
    created_at: AwareDatetime
    updated_at: AwareDatetime
    closed_at: AwareDatetime | None


class ExternalIssuesView(StrictModel):
    source: Literal["github"]
    trust: Literal["external_unverified"]
    trusted_for_policy: Literal[False]
    repository: str
    observed_at: AwareDatetime
    ok: bool
    error_code: ErrorCode | None
    retryable: bool
    has_more: bool
    issues: Annotated[list[ExternalIssueClue], Field(max_length=50)]


def freeze_repository(snapshot: dict, tenant_id: str) -> None:
    if snapshot.get("graph_version") not in GRAPH_VERSIONS:
        return
    try:
        repositories = allowed_repositories_from_environment().get(tenant_id, ())
    except ValueError:
        return
    if repositories:
        snapshot[SNAPSHOT_KEY] = {"schema_version": 1, "repository": repositories[0]}


def snapshot_repository(snapshot: dict) -> str | None:
    config = snapshot.get(SNAPSHOT_KEY)
    if not isinstance(config, dict) or set(config) != {"schema_version", "repository"}:
        return None
    version, repository = config["schema_version"], config["repository"]
    return repository if type(version) is int and version == 1 and isinstance(repository, str) else None


def currently_allowed(tenant_id: str, repository: str) -> bool:
    try:
        return repository in allowed_repositories_from_environment().get(tenant_id, ())
    except ValueError:
        return False


def project_issue_observation(raw: dict, repository: str) -> ExternalIssuesView | None:
    try:
        envelope = ToolEnvelope.model_validate_json(json.dumps(raw, ensure_ascii=False))
        if envelope.source != "github":
            return None
        issues, has_more = [], False
        if envelope.ok:
            data = envelope.data
            if (data is None or f"{data.get('owner')}/{data.get('repo')}" != repository
                    or data.get("trust") != "external_unverified" or data.get("trusted_for_policy") is not False
                    or not isinstance(data.get("issues"), list) or type(data.get("has_more")) is not bool):
                return None
            has_more = data["has_more"]
            for item in data["issues"]:
                if not isinstance(item, dict):
                    return None
                # Build the public link from the currently allowed repository and
                # validated issue number. Raw HTML, links and body never reach the browser.
                fields = {key: item[key] for key in ("number", "state", "title", "created_at", "updated_at", "closed_at")}
                fields["url"] = f"https://github.com/{repository}/issues/{item['number']}"
                issues.append(ExternalIssueClue.model_validate_json(json.dumps(fields, ensure_ascii=False)))
        return ExternalIssuesView(source="github", trust="external_unverified", trusted_for_policy=False,
            repository=repository, observed_at=envelope.observed_at, ok=envelope.ok, error_code=envelope.error_code,
            retryable=envelope.retryable, has_more=has_more, issues=issues)
    except (KeyError, TypeError, ValidationError, ValueError):
        return None
