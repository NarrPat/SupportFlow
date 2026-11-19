"""Explicit boundary between durable execution control and the application graph."""
from dataclasses import dataclass
from datetime import datetime
from typing import Callable, Literal, Protocol

from psycopg import Connection


@dataclass(frozen=True)
class FragmentOutcome:
    status: Literal["succeeded", "handoff", "failed", "cancelled", "expired",
                    "awaiting_input", "awaiting_approval", "needs_reconciliation"]
    reason_code: str
    result: dict | None = None
    wait_expires_at: datetime | None = None
    wait_input_id: str | None = None
    wait_checkpoint_id: str | None = None
    wait_checkpoint_ns: str = ""
    wait_checkpoint_at: datetime | None = None
    wait_proposal_id: str | None = None


class FragmentGate(Protocol):
    def check(self, stage: str) -> float:
        """Revalidate execution ownership and return remaining processing seconds."""

    def reserve_model(self) -> int:
        """Persist one model attempt before issuing it and return its stable number."""

    def complete_model(self, attempt: int, usage: dict | None,
                       provider_response_id: str | None = None, error_code: str | None = None) -> None:
        """Record known response metadata; disconnected attempts remain unknown."""

    def reserve_tool(self) -> int:
        """Persist one tool decision round before dispatching its calls."""


Runner = Callable[[Connection, dict, FragmentGate], FragmentOutcome]
CheckpointProbe = Callable[[Connection, dict], FragmentOutcome | None]


class StopFragment(Exception):
    """A controlled execution gate result; it contains no user or provider text."""

    def __init__(self, reason_code: str, status: str = "handoff"):
        super().__init__(reason_code)
        self.reason_code = reason_code
        self.status = status


class StaleFragment(Exception):
    """The fixed connection no longer owns the current execution generation."""
