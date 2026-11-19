"""Durable human-input boundaries, separate from graph execution and HTTP identity.

Projection/expiry helpers require the caller's limits -> run transaction and the
same fixed connection as its advisory lock. They never connect or commit.
"""
from datetime import datetime, timedelta
import hashlib
import json
from typing import Annotated
from uuid import NAMESPACE_URL, uuid5

from pydantic import BaseModel, ConfigDict, Field, field_validator

from .contracts import StaleFragment, StopFragment

WAIT_TTL_SECONDS = 900
GRAPH_VERSION = "clarification-diagnosis-v1"
ANSWER_FIELDS = frozenset({"customer_id", "invitation_email", "seats", "days"})


def compatible_config(config):
    if isinstance(config, dict) and config.get("graph_version") in {"grant-approval-v1", "grant-execution-v1"}:
        from apps.supportflow.domain.approvals import approval_capable_config
        return approval_capable_config(config)
    return (isinstance(config, dict) and type(config.get("schema_version")) is int
            and config["schema_version"] == 1 and config.get("contract_version") == "dispatch-v1"
            and (config.get("graph_version") == GRAPH_VERSION or
                 config.get("graph_version") == "grant-proposal-v1" and
                 type(config.get("grant_contract_version")) is int and config["grant_contract_version"] == 1))


def valid_text(value, *, maximum):
    if not isinstance(value, str) or not 1 <= len(value) <= maximum or not value.strip() or "\0" in value:
        raise ValueError("Expected bounded nonempty text")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        raise ValueError("Expected valid UTF-8 text") from None
    return value


class AnswerFields(BaseModel):
    model_config = ConfigDict(strict=True, extra="forbid", hide_input_in_errors=True)
    customer_id: Annotated[str, Field(min_length=1, max_length=128)] | None = None
    invitation_email: Annotated[str, Field(min_length=3, max_length=254)] | None = None
    seats: Annotated[int, Field(ge=1, le=20)] | None = None
    days: Annotated[int, Field(ge=1, le=7)] | None = None

    @field_validator("customer_id", "invitation_email", "seats", "days", mode="before")
    @classmethod
    def reject_explicit_null(cls, value):
        if value is None:
            raise ValueError("A submitted answer cannot be null")
        return value

    @field_validator("customer_id")
    @classmethod
    def valid_customer(cls, value):
        return valid_text(value, maximum=128)

    @field_validator("invitation_email")
    @classmethod
    def valid_email(cls, value):
        from apps.supportflow.domain.rules import normalize_email
        return normalize_email(valid_text(value, maximum=254))


def validate_required_fields(fields):
    if (not isinstance(fields, list) or not 1 <= len(fields) <= 4
            or any(not isinstance(x, str) or x not in ANSWER_FIELDS for x in fields)
            or len(set(fields)) != len(fields)):
        raise ValueError("Expected distinct necessary answer fields")
    return list(fields)


def normalize_answers(answers, required_fields=None):
    if not isinstance(answers, dict) or not answers:
        raise ValueError("Expected nonempty answers")
    if required_fields is not None and set(answers) != set(validate_required_fields(required_fields)):
        raise ValueError("Answers must fill exactly the necessary fields")
    return AnswerFields.model_validate(answers).model_dump(exclude_unset=True)


def canonical_answer(run_id, input_id, expected_version, answers, *, refusal_reason=None):
    valid_text(run_id, maximum=128)
    valid_text(input_id, maximum=128)
    if type(expected_version) is not int or not 1 <= expected_version <= 2**31-1:
        raise ValueError("Expected a positive input version")
    if refusal_reason is not None:
        valid_text(refusal_reason, maximum=500)
        if answers != {}:raise ValueError("Refusal cannot confirm fields")
    value = {"schema_version": 1, "operation": "submit_clarification", "run_id": run_id,
             "input_id": input_id, "expected_version": expected_version, "answers": {} if refusal_reason is not None else normalize_answers(answers)}
    if refusal_reason is not None:value["refusal_reason"] = refusal_reason
    raw = json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"), allow_nan=False)
    return raw, hashlib.sha256(raw.encode("utf-8")).hexdigest()


def stable_input_id(run_id, input_seq):
    valid_text(run_id, maximum=128)
    if type(input_seq) is not int or input_seq < 1:
        raise ValueError("Expected a positive input sequence")
    return str(uuid5(NAMESPACE_URL, f"supportflow:input:{run_id}:{input_seq}"))


def waiting_milliseconds(start, end):
    if start is None:
        return 0
    if not isinstance(start, datetime) or not isinstance(end, datetime) or start.tzinfo is None or end.tzinfo is None:
        raise ValueError("Expected aware waiting timestamps")
    delta = max(end-start, timedelta(0))
    return delta.days*86400000 + delta.seconds*1000 + delta.microseconds//1000


def wait_has_expired(expires_at, now):
    if expires_at.tzinfo is None or now.tzinfo is None:
        raise ValueError("Expected aware expiry timestamps")
    return now >= expires_at


def _now(conn):
    return conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]


def _input(conn, run, input_id=None):
    condition = "input_id=%s" if input_id is not None else "status='pending'"
    args = (run["tenant_id"], run["run_id"], input_id) if input_id is not None else (run["tenant_id"], run["run_id"])
    return conn.execute("SELECT * FROM pending_inputs WHERE tenant_id=%s AND run_id=%s AND "
                        + condition + " FOR UPDATE", args).fetchone()


def prepare_input(conn, run, required_fields, prompt, *, input_seq=1):
    from .core import Gate
    fields = validate_required_fields(required_fields)
    valid_text(prompt, maximum=1000)
    input_id = stable_input_id(run["run_id"], input_seq)
    with conn.transaction():
        current = Gate(conn, run["run_id"], run["execution_generation"])._current()
        existing = _input(conn, current, input_id)
        if existing is not None:
            if existing["required_fields"] != fields or existing["prompt"] != prompt:
                raise StopFragment("INPUT_CONTRACT_CHANGED")
            # interrupt() restarts the same node even after this row was answered.
            return existing
        if not compatible_config(current["config_snapshot"]):
            raise StopFragment("CONFIG_VERSION_UNSUPPORTED")
        if _input(conn, current) is not None:
            raise StopFragment("INPUT_ALREADY_PENDING")
        now = _now(conn)
        return conn.execute("""INSERT INTO pending_inputs
            (input_id,tenant_id,run_id,input_seq,required_fields,prompt,created_generation,created_at,expires_at)
            VALUES(%s,%s,%s,%s,%s,%s,%s,%s,%s) RETURNING *""",
            (input_id, current["tenant_id"], current["run_id"], input_seq, fields, prompt,
             current["execution_generation"], now, now+timedelta(seconds=WAIT_TTL_SECONDS))).fetchone()


def project_wait(conn, run, *, input_id, checkpoint_id, checkpoint_ns="", checkpoint_created_at):
    from .core import append_event, owns_lock
    if not owns_lock(conn, run["run_id"]):
        raise StaleFragment("Run lock missing")
    row = _input(conn, run, input_id)
    if row is None or row["status"] != "pending":
        raise StaleFragment("Pending input changed")
    if run["status"] == "awaiting_input" and row["checkpoint_id"] is not None:
        return run
    if run["status"] != "running" or run["admission_state"] != "active":
        raise StaleFragment("Run is no longer active")
    if not isinstance(checkpoint_created_at, datetime) or checkpoint_created_at.tzinfo is None:
        raise ValueError("Expected actual checkpoint timestamp")
    valid_text(checkpoint_id, maximum=128)
    if checkpoint_ns != "":
        raise ValueError("Only the server-owned outer checkpoint namespace may wait")
    exists = conn.execute("""SELECT 1 FROM checkpoints
        WHERE thread_id=%s AND checkpoint_ns=%s AND checkpoint_id=%s""",
        (run["run_id"], checkpoint_ns, checkpoint_id)).fetchone()
    if exists is None:
        raise ValueError("A wait requires a persisted checkpoint")
    now = _now(conn)
    conn.execute("""UPDATE pending_inputs SET checkpoint_id=%s,checkpoint_ns=%s,checkpoint_at=%s,
        checkpoint_generation=%s,wait_started_at=%s WHERE input_id=%s""",
        (checkpoint_id, checkpoint_ns, checkpoint_created_at, run["execution_generation"], now, input_id))
    conn.execute("UPDATE runtime_limits SET active_reserved=active_reserved-1 WHERE app='supportflow'")
    updated = conn.execute("""UPDATE runs SET status='awaiting_input',reason_code='INPUT_REQUIRED',result=NULL,
        admission_state='none',version=version+1,last_event_seq=last_event_seq+1,waiting_since=%s,
        wait_expires_at=%s WHERE run_id=%s AND execution_generation=%s RETURNING *""",
        (now, row["expires_at"], run["run_id"], run["execution_generation"])).fetchone()
    append_event(conn, updated, "clarification.required", {
        "input_id": input_id, "version": row["version"], "prompt": row["prompt"],
        "required_fields": row["required_fields"], "expires_at": row["expires_at"].isoformat()})
    updated = conn.execute("UPDATE runs SET last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *",
                           (run["run_id"],)).fetchone()
    append_event(conn, updated, "run.status_changed", {"status": "awaiting_input", "reason_code": "INPUT_REQUIRED"})
    return updated


def close_pending(conn, run, *, status="cancelled", now=None):
    """Close only a pending object, accounting each persisted interval once."""
    if status not in {"cancelled", "expired"}:
        raise ValueError("Expected an input closure status")
    row = _input(conn, run)
    if row is None:
        return
    now = now or _now(conn)
    end = max(now, row["wait_started_at"]) if row["wait_started_at"] is not None else None
    conn.execute("""UPDATE pending_inputs SET status=%s,version=version+1,wait_ended_at=%s WHERE input_id=%s""",
                 (status, end, row["input_id"]))
    elapsed = waiting_milliseconds(row["wait_started_at"], end) if end is not None else 0
    conn.execute("""UPDATE runs SET accumulated_wait_ms=accumulated_wait_ms+%s,
        waiting_since=NULL,wait_expires_at=NULL WHERE run_id=%s""", (elapsed, run["run_id"]))


def expire_wait(conn, run, *, now=None):
    from .core import append_event
    if run["status"] != "awaiting_input":
        return None
    row = _input(conn, run)
    now = now or _now(conn)
    if row is None or not wait_has_expired(row["expires_at"], now):
        return None
    close_pending(conn, run, status="expired", now=now)
    updated = conn.execute("""UPDATE runs SET status='expired',reason_code='CLARIFICATION_EXPIRED',
        result=NULL,version=version+1,last_event_seq=last_event_seq+1
        WHERE run_id=%s AND status='awaiting_input' RETURNING *""", (run["run_id"],)).fetchone()
    append_event(conn, updated, "run.status_changed", {"status": "expired", "reason_code": "CLARIFICATION_EXPIRED"})
    return updated


def read_resume(conn, run, *, input_id):
    # A scanner may inspect an unclaimed recovery generation. Reading an answer
    # neither claims that generation nor grants permission to execute a node.
    from .core import locked_run, owns_lock
    if not owns_lock(conn, run["run_id"]):
        raise StaleFragment("Run lock missing")
    with conn.transaction():
        _, current = locked_run(conn, run["run_id"])
        if (current is None or current["execution_generation"] != run["execution_generation"]
                or current["tenant_id"] != run["tenant_id"] or current["actor_id"] != run["actor_id"]
                or current["status"] != "running"):
            raise StaleFragment("Execution generation changed")
        row = _input(conn, current, input_id)
        if row is None or row["status"] != "answered":
            return None
        if (row["resume_generation"] is None or row["resume_generation"] > current["execution_generation"]
                or row["answered_by"] != current["actor_id"]):
            raise StaleFragment("Answer belongs to another fragment")
        answers = normalize_answers(row["answers"], row["required_fields"])
        if any(current["confirmed_fields"].get(key) != value for key, value in answers.items()):
            raise StopFragment("INPUT_CONTRACT_CHANGED")
        return {"input_id": input_id, "answers": answers}
