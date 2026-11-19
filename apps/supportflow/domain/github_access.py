"""Shared GitHub repository contract and backend allowlist."""
import json
import os
import re
from collections.abc import Mapping
from typing import Annotated, Literal

from pydantic import Field, ValidationError, field_validator

from .contracts import StrictModel


_IDENTITY = re.compile(r"[A-Za-z0-9_-]{1,128}")
_OWNER_PATTERN = r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,37}[A-Za-z0-9])?$"
_REPO_PATTERN = r"^[A-Za-z0-9_.-]{1,100}$"


def valid_identity(value: object) -> bool:
    return isinstance(value, str) and _IDENTITY.fullmatch(value) is not None


class ExternalIssuesInput(StrictModel):
    owner: Annotated[str, Field(pattern=_OWNER_PATTERN)]
    repo: Annotated[str, Field(pattern=_REPO_PATTERN)]
    state: Literal["open", "closed", "all"] = "open"

    @field_validator("owner", "repo")
    @classmethod
    def normalize_path_segment(cls, value: str) -> str:
        normalized = value.lower()
        if normalized in {".", ".."} or normalized.endswith(".git"):
            raise ValueError("Invalid GitHub repository segment")
        return normalized

    @property
    def repository(self) -> str:
        return f"{self.owner}/{self.repo}"


def normalize_allowlist(value: Mapping) -> dict[str, tuple[str, ...]]:
    if not isinstance(value, Mapping):
        raise ValueError("Invalid GitHub repository allowlist")
    result = {}
    for tenant_id, entries in value.items():
        if not valid_identity(tenant_id) or not isinstance(entries, (list, tuple, set, frozenset)):
            raise ValueError("Invalid GitHub repository allowlist")
        canonical = set()
        for entry in entries:
            if not isinstance(entry, str) or entry.count("/") != 1:
                raise ValueError("Invalid GitHub repository allowlist")
            owner, repo = entry.split("/")
            try:
                canonical.add(ExternalIssuesInput(owner=owner, repo=repo).repository)
            except ValidationError:
                raise ValueError("Invalid GitHub repository allowlist") from None
        result[tenant_id] = tuple(sorted(canonical))
    return result


def allowed_repositories_from_environment() -> dict[str, tuple[str, ...]]:
    raw = os.environ.get("SF_GITHUB_ALLOWED_REPOSITORIES", "")
    if not raw:
        return {}
    try:
        value = json.loads(raw)
    except (ValueError, RecursionError):
        raise ValueError("Invalid GitHub repository allowlist") from None
    return normalize_allowlist(value)
