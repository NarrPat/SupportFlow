"""PostgreSQL is the authority for claims, execution generations and permits."""
from datetime import timedelta
import hashlib
import os
import re

import psycopg
from psycopg.types.json import Jsonb

from . import event_hints
from .contracts import CheckpointProbe, FragmentOutcome, Runner, StaleFragment, StopFragment
from .runtime import trace


QUEUED = {"queued", "resume_queued"}
QUEUED_REDELIVERY_AGE = timedelta(seconds=15)
WAITING = {"awaiting_input", "awaiting_approval"}
CLOSED = {"succeeded", "handoff", "failed", "cancelled", "expired", "rejected"}
OUTCOMES = CLOSED | WAITING | {"needs_reconciliation"}
USAGE_KEYS = {"input_tokens", "output_tokens", "total_tokens",
              "cache_hit_input_tokens", "cache_miss_input_tokens"}


def lock_key(run_id: str) -> int:
    return int.from_bytes(hashlib.sha256(("supportflow\0" + run_id).encode()).digest()[:8],
                          "big", signed=True)


def try_lock(conn, run_id: str) -> bool:
    return conn.execute("SELECT pg_try_advisory_lock(%s) AS locked", (lock_key(run_id),)).fetchone()["locked"]


def owns_lock(conn, run_id: str) -> bool:
    key = lock_key(run_id) & ((1 << 64) - 1)
    return conn.execute("""
        SELECT EXISTS (SELECT 1 FROM pg_locks WHERE locktype='advisory'
            AND pid=pg_backend_pid() AND granted AND objsubid=1
            AND classid=%s::oid AND objid=%s::oid) AS held
    """, (key >> 32, key & 0xffffffff)).fetchone()["held"]


def unlock(conn, run_id: str):
    if not conn.closed:
        try:
            conn.execute("SELECT pg_advisory_unlock(%s)", (lock_key(run_id),))
        except psycopg.Error:
            # Closing this same connection releases the lock. Never reconnect.
            pass


def session_identity(conn, run_id: str, generation: int):
    conn.execute("SELECT set_config('sf.run_id', %s, false), set_config('sf.generation', %s, false)",
                 (run_id, str(generation)))


def locked_run(conn, run_id: str):
    limits = conn.execute("SELECT * FROM runtime_limits WHERE app='supportflow' FOR UPDATE").fetchone()
    if limits is None:
        raise RuntimeError("Admission configuration missing")
    run = conn.execute("""
        SELECT r.*, COALESCE(r.confirmed_fields->>'customer_id',t.customer_id) AS customer_id FROM runs r
        JOIN threads h USING (tenant_id, thread_id, actor_id)
        JOIN tickets t USING (tenant_id, ticket_id)
        WHERE r.run_id=%s FOR UPDATE OF r
    """, (run_id,)).fetchone()
    return limits, run


def authorized(conn, run) -> bool:
    return conn.execute("""
        SELECT EXISTS (
            SELECT 1 FROM memberships m JOIN threads h
              ON h.tenant_id=m.tenant_id AND h.actor_id=m.user_id
            JOIN tickets t ON t.tenant_id=h.tenant_id AND t.ticket_id=h.ticket_id
            WHERE m.tenant_id=%s AND m.user_id=%s AND h.thread_id=%s
              AND m.role IN ('support','supervisor','admin')
              AND ((t.customer_id IS NULL AND t.assignee_id=m.user_id)
                   OR EXISTS (SELECT 1 FROM customer_scopes s
                       WHERE s.tenant_id=m.tenant_id AND s.user_id=m.user_id
                         AND s.customer_id=t.customer_id))
              AND (%s::text IS NULL OR EXISTS (SELECT 1 FROM customer_scopes cs
                  WHERE cs.tenant_id=m.tenant_id AND cs.user_id=m.user_id AND cs.customer_id=%s))
        ) AS allowed
    """, (run["tenant_id"], run["actor_id"], run["thread_id"],
          run.get("confirmed_fields", {}).get("customer_id"), run.get("confirmed_fields", {}).get("customer_id"))).fetchone()["allowed"]


def lock_final_scope(conn, run) -> bool:
    """Current wrapper authorization held through its short publication commit."""
    row = conn.execute("""SELECT m.role,t.customer_id,t.assignee_id FROM memberships m
        JOIN threads h ON h.tenant_id=m.tenant_id AND h.actor_id=m.user_id
        JOIN tickets t ON t.tenant_id=h.tenant_id AND t.ticket_id=h.ticket_id
        WHERE m.tenant_id=%s AND m.user_id=%s AND h.thread_id=%s
        FOR SHARE OF m,h,t""", (run["tenant_id"], run["actor_id"], run["thread_id"])).fetchone()
    if row is None or row["role"] not in {"support", "supervisor", "admin"}:
        return False
    confirmed = run.get("confirmed_fields", {}).get("customer_id")
    if row["customer_id"] is None and row["assignee_id"] != run["actor_id"]:
        return False
    if row["customer_id"] is not None and confirmed is not None and row["customer_id"] != confirmed:
        return False
    for customer in sorted({c for c in (row["customer_id"], confirmed) if c is not None}):
        grant = conn.execute("""SELECT 1 FROM customer_scopes
            WHERE tenant_id=%s AND user_id=%s AND customer_id=%s FOR SHARE""",
            (run["tenant_id"], run["actor_id"], customer)).fetchone()
        if grant is None:
            return False
    return True


def remaining_seconds(conn, run) -> float:
    now = conn.execute("SELECT clock_timestamp() AS now").fetchone()["now"]
    elapsed = (now - run["budget_origin_at"]).total_seconds() - run["accumulated_wait_ms"] / 1000
    return run["budget_ms"] / 1000 - max(0, elapsed)


def stop_reason(conn, run):
    if not authorized(conn, run):
        return FragmentOutcome("handoff", "AUTHORIZATION_REVOKED")
    if run["cancel_requested"]:
        return FragmentOutcome("cancelled", "CANCEL_REQUESTED")
    if remaining_seconds(conn, run) <= 0:
        return FragmentOutcome("handoff", "BUDGET_EXHAUSTED")
    return None


def append_event(conn, run, event_type: str, payload: dict):
    conn.execute("""
        INSERT INTO run_events (run_id,tenant_id,seq,status_version,execution_generation,type,payload)
        VALUES (%s,%s,%s,%s,%s,%s,%s)
    """, (run["run_id"], run["tenant_id"], run["last_event_seq"], run["version"],
          run["execution_generation"], event_type, Jsonb(payload)))
    event_hints.mark_sync(conn, run)


def append_progress(conn, run_id: str, generation: int, event_type: str, payload: dict):
    """Persist a public progress projection under the fragment's original lock.

    Sequence advances independently of the visible status version. No tool
    response, prompt or framework state is admitted to this event boundary.
    """
    fields = {"stage.changed": {"stage"}, "tool.started": {"tool_id"},
              "tool.finished": {"tool_id", "ok"}}
    if event_type not in fields or set(payload) != fields[event_type]:
        raise ValueError("Unexpected public progress event")
    label = payload.get("stage", payload.get("tool_id"))
    if not isinstance(label, str) or not re.fullmatch(r"[a-z][a-z0-9_.]{0,63}", label):
        raise ValueError("Unexpected public progress label")
    if "ok" in payload and type(payload["ok"]) is not bool:
        raise ValueError("Expected a boolean tool outcome")
    with event_hints.transaction(conn):
        run = Gate(conn, run_id, generation)._current()
        updated = conn.execute("UPDATE runs SET last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *",
                               (run_id,)).fetchone()
        append_event(conn, updated, event_type, payload)


def finish_locked(conn, run, outcome: FragmentOutcome):
    if outcome.status not in OUTCOMES or not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", outcome.reason_code):
        raise ValueError("Invalid graph outcome")
    if outcome.result is not None and not isinstance(outcome.result, dict):
        raise ValueError("Invalid graph result")
    if run['config_snapshot'].get('graph_version') == 'grant-execution-v1':
        from .grant_effects import protect_outcome, read_dispatch
        outcome = protect_outcome(conn,run,outcome)
        if read_dispatch(conn,run) is None and not lock_final_scope(conn,run):
            outcome = FragmentOutcome('handoff','AUTHORIZATION_REVOKED')
    if run["config_snapshot"].get("graph_version") in {"clarification-diagnosis-v1", "grant-proposal-v1", "grant-approval-v1"} and not lock_final_scope(conn, run):
        outcome = FragmentOutcome("handoff", "AUTHORIZATION_REVOKED")
    if outcome.status == "awaiting_input" and outcome.wait_input_id is not None:
        from .waiting import project_wait
        if not outcome.wait_checkpoint_id or outcome.wait_checkpoint_at is None:
            raise ValueError("Expected an actual durable waiting checkpoint")
        return project_wait(conn, run, input_id=outcome.wait_input_id, checkpoint_id=outcome.wait_checkpoint_id,
                            checkpoint_ns=outcome.wait_checkpoint_ns, checkpoint_created_at=outcome.wait_checkpoint_at)
    if outcome.status == "awaiting_approval" and outcome.wait_proposal_id is not None:
        from .proposal_waiting import project_wait
        if not outcome.wait_checkpoint_id or outcome.wait_checkpoint_at is None:
            raise ValueError("Expected an actual durable proposal checkpoint")
        try:
            return project_wait(conn, run, proposal_id=outcome.wait_proposal_id, checkpoint_id=outcome.wait_checkpoint_id,
                                checkpoint_ns=outcome.wait_checkpoint_ns, checkpoint_created_at=outcome.wait_checkpoint_at)
        except StopFragment as error:
            outcome = FragmentOutcome(error.status, error.reason_code)
    if outcome.result is not None and outcome.result.get("schema_version") == 7:
        from .grant_policy_reply import validate_publication
        outcome = validate_publication(conn, run, outcome)
    elif outcome.result is not None and outcome.result.get("schema_version") == 6:
        from .capacity_diagnosis import validate_publication
        outcome = validate_publication(conn, run, outcome)
    elif outcome.result is not None and outcome.result.get("schema_version") == 5:
        from .knowledge_answers import validate_publication
        outcome = validate_publication(conn, run, outcome)
    elif ((run["config_snapshot"].get("graph_version") in {"readonly-diagnosis-rag-v1", "clarification-diagnosis-v1", "grant-proposal-v1", "grant-approval-v1"}
            or outcome.result is not None and outcome.result.get('schema_version')==8)
            and outcome.result is not None and (outcome.result.get("evidence_refs")
                or outcome.result.get("resolution_code") == "DIAGNOSIS_COMPLETE")):
        from apps.supportflow.retrieval.service import KnowledgeError, reviewed_support, validate_refs
        from apps.supportflow.domain.rules import invalidate_rag_evidence
        try:
            # Final publication shares this transaction with the held run lock.
            # No embedding, requery, model call or fresh connection is permitted here.
            rows = validate_refs(conn, run, outcome.result["evidence_refs"], current_generation=True, lock=True)
            if outcome.result.get("resolution_code") == "DIAGNOSIS_COMPLETE":
                # A completed checkpoint proves a past decision, not that the
                # current reviewed manifest still supports every published claim.
                topics, codes = reviewed_support(rows)
                if ("invitation_diagnosis" not in topics
                        or not set(outcome.result["blocking_codes"]).issubset(codes)):
                    raise KnowledgeError("EVIDENCE_REQUIRED")
        except KnowledgeError:
            outcome = FragmentOutcome("handoff", "EVIDENCE_REQUIRED", invalidate_rag_evidence(outcome.result))
    waiting = outcome.status in WAITING
    if waiting and outcome.wait_expires_at is None:
        raise ValueError("A durable wait must have an expiry")
    if run["config_snapshot"].get("graph_version") in {"clarification-diagnosis-v1", "grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"} and not waiting:
        from .waiting import close_pending
        close_pending(conn, run)
        if run["config_snapshot"].get("graph_version") in {"grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"}:
            from .proposal_waiting import close_pending as close_proposal
            close_proposal(conn, run, status="expired" if outcome.status == "expired" else "cancelled")
    if run['config_snapshot'].get('graph_version') == 'grant-execution-v1':
        from .grant_effects import finalize_domain
        finalize_domain(conn,run,outcome)
    conn.execute("""
        UPDATE runtime_limits SET logical_queued=logical_queued-%s,
            active_reserved=active_reserved-%s WHERE app='supportflow'
    """, (int(run["admission_state"] == "queued"), int(run["admission_state"] == "active")))
    if (run["config_snapshot"].get("graph_version") in {"readonly-diagnosis-v1", "readonly-diagnosis-rag-v1", "clarification-diagnosis-v1", "grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"}
            and outcome.result is not None):
        reply = outcome.result.get("reply_text")
        if not isinstance(reply, str) or len(reply) > 12000:
            raise ValueError("Expected a bounded final reply")
        message_run = conn.execute(
            "UPDATE runs SET last_event_seq=last_event_seq+1 WHERE run_id=%s RETURNING *",
            (run["run_id"],)).fetchone()
        append_event(conn, message_run, "message.final", {"message_id": run["run_id"] + ":reply", "text": reply})
    updated = conn.execute("""
        UPDATE runs SET status=%s, reason_code=%s, result=%s, admission_state='none',
            version=version+1,last_event_seq=last_event_seq+1,
            waiting_since=CASE WHEN %s THEN clock_timestamp() ELSE NULL END,
            wait_expires_at=%s WHERE run_id=%s AND execution_generation=%s RETURNING *
    """, (outcome.status, outcome.reason_code, Jsonb(outcome.result) if outcome.result is not None else None,
          waiting, outcome.wait_expires_at, run["run_id"], run["execution_generation"])).fetchone()
    append_event(conn, updated, "run.status_changed", {"status": outcome.status, "reason_code": outcome.reason_code})
    return updated


def rearm(conn, run, *, delay_seconds: float = 1):
    # Increment the revision even when a publisher is still outside its transaction.
    # Its later mark-sent cannot overwrite this new scheduling decision.
    kind = "recover_fragment" if run["status"] == "running" else (
        "resume" if run["status"] == "resume_queued" else "create")
    return conn.execute("""
        INSERT INTO outbox (tenant_id,run_id,execution_generation,kind,available_at)
        VALUES (%s,%s,%s,%s,clock_timestamp()+(%s * interval '1 second'))
        ON CONFLICT (app,run_id,execution_generation,kind) DO UPDATE
        SET status='pending', available_at=clock_timestamp()+(%s * interval '1 second'),
            dispatch_version=outbox.dispatch_version+1
        RETURNING outbox_id
    """, (run["tenant_id"], run["run_id"], run["execution_generation"], kind,
          delay_seconds, delay_seconds)).fetchone()["outbox_id"]


class Gate:
    """Every call uses the original lock/Saver connection; no fallback connector."""
    def __init__(self, conn, run_id: str, generation: int):
        self.conn, self.run_id, self.generation = conn, run_id, generation

    def _current(self, *, check_execution: bool = True):
        if not owns_lock(self.conn, self.run_id):
            raise StaleFragment("Run lock missing")
        _, run = locked_run(self.conn, self.run_id)
        if (run is None or run["execution_generation"] != self.generation
                or run["claimed_generation"] != self.generation
                or run["status"] != "running" or run["admission_state"] != "active"):
            raise StaleFragment("Execution ownership changed")
        if check_execution:
            stopped = stop_reason(self.conn, run)
            if stopped is not None:
                raise StopFragment(stopped.reason_code, stopped.status)
        return run

    def check(self, stage: str) -> float:
        with self.conn.transaction():
            run = self._current()
            self.conn.execute("UPDATE runs SET heartbeat_at=clock_timestamp() WHERE run_id=%s", (self.run_id,))
            return remaining_seconds(self.conn, run)

    __call__ = check

    def _reserve(self, kind: str) -> int:
        column, maximum = ("model_attempts", 8) if kind == "model" else ("tool_rounds", 6)
        with self.conn.transaction():
            run = self._current()
            if kind == "tool":
                from apps.supportflow.domain.approvals import approval_compatible_config, execution_compatible_config
                if execution_compatible_config(run['config_snapshot']):
                    maximum = run['config_snapshot']['tool_round_limit']
                elif approval_compatible_config(run["config_snapshot"]):
                    maximum = 7
            if run[column] >= maximum:
                raise StopFragment("MODEL_ATTEMPTS_EXHAUSTED" if kind == "model" else "TOOL_ROUNDS_EXHAUSTED")
            number = run[column] + 1
            # The identifier is selected from two internal constants, never an input.
            self.conn.execute(f"UPDATE runs SET {column}=%s,heartbeat_at=clock_timestamp() WHERE run_id=%s",
                              (number, self.run_id))
            if kind == "model":
                self.conn.execute("""
                    INSERT INTO run_model_attempts(run_id,attempt,execution_generation) VALUES (%s,%s,%s)
                """, (self.run_id, number, self.generation))
            return number

    def reserve_model(self) -> int:
        return self._reserve("model")

    def reserve_tool(self) -> int:
        return self._reserve("tool")

    def complete_model(self, attempt: int, usage: dict | None, provider_response_id: str | None = None,
                       error_code: str | None = None) -> None:
        if usage is not None and (not isinstance(usage, dict) or set(usage) - USAGE_KEYS
                or any(value is not None and (type(value) is not int or value < 0) for value in usage.values())):
            raise ValueError("Invalid usage metadata")
        if provider_response_id is not None and (not isinstance(provider_response_id, str)
                or not 1 <= len(provider_response_id) <= 256):
            raise ValueError("Invalid provider response identifier")
        if error_code is not None and not re.fullmatch(r"[A-Z][A-Z0-9_]{0,63}", error_code):
            raise ValueError("Invalid provider error category")
        with self.conn.transaction():
            self._current(check_execution=False)
            self.conn.execute("""
                UPDATE run_model_attempts SET ended_at=clock_timestamp(),status=%s,
                    usage=%s,provider_response_id=%s,error_code=%s
                WHERE run_id=%s AND attempt=%s AND execution_generation=%s AND status='started'
            """, ("error" if error_code else "completed", Jsonb(usage) if usage is not None else None,
                  provider_response_id, error_code, self.run_id, attempt, self.generation))


def claim(conn, run_id: str, generation: int) -> tuple[dict | None, dict | None]:
    """Return only after the claim transaction commits, including early exits."""
    with event_hints.transaction(conn):
        limits, run = locked_run(conn, run_id)
        if run is None or run["execution_generation"] != generation:
            return None, {"outcome": "stale"}
        if run["status"] not in QUEUED | {"running"}:
            return None, {"outcome": "inactive"}
        if run["status"] == "running" and run["claimed_generation"] == generation:
            return None, {"outcome": "already_claimed"}
        effect = None
        if run['config_snapshot'].get('graph_version') == 'grant-execution-v1':
            from .grant_effects import effect_outcome, read_dispatch
            dispatch = read_dispatch(conn,run)
            if dispatch is not None and dispatch['status'] != 'rejected':
                effect = effect_outcome(conn, run, row=dispatch)
        stopped = stop_reason(conn, run)
        first_claim = run["status"] in QUEUED
        if effect is not None and (stopped or first_claim):
            finalized = finish_locked(conn, run, effect)
            return None, {"outcome": "effect_projected", "status": finalized["status"],
                          "reason_code": finalized["reason_code"]}
        if stopped:
            finish_locked(conn, run, stopped)
            return None, {"outcome": "controlled_stop", "status": stopped.status, "reason_code": stopped.reason_code}
        if first_claim and limits["active_reserved"] >= 2:
            rearm(conn, run)
            return None, {"outcome": "no_capacity"}
        if first_claim:
            conn.execute("""UPDATE runtime_limits SET logical_queued=logical_queued-1,
                active_reserved=active_reserved+1 WHERE app='supportflow'""")
        run = conn.execute("""
            UPDATE runs SET status='running',admission_state='active',reason_code='RUNNING',
                claimed_generation=execution_generation,claimed_at=clock_timestamp(),
                heartbeat_at=clock_timestamp(),version=version+1,last_event_seq=last_event_seq+1
            WHERE run_id=%s AND execution_generation=%s RETURNING *
        """, (run_id, generation)).fetchone()
        append_event(conn, run, "run.started" if first_claim else "run.resumed", {"status": "running"})
        if effect is not None:
            # Persist the recovery claim under the original permit. Its ledger
            # determines the outcome before any checkpoint or graph is loaded.
            finalized = finish_locked(conn, run, effect)
            return None, {"outcome": "effect_projected", "status": finalized["status"],
                          "reason_code": finalized["reason_code"]}
        return run, None


def run_fragment(conn, run_id: str, generation: int, runner: Runner) -> dict:
    identity = {"run_id": run_id, "generation": generation,
                "worker_pid": os.getpid(), "backend_pid": conn.info.backend_pid}

    def done(outcome: str, **fields):
        result = {**identity, "outcome": outcome, **fields}
        trace("worker_attempt", **result)
        return result

    acquired = False
    try:
        session_identity(conn, run_id, generation)
        preliminary = conn.execute("SELECT execution_generation,status FROM runs WHERE run_id=%s", (run_id,)).fetchone()
        if preliminary is None or preliminary["execution_generation"] != generation:
            return done("stale")
        if preliminary["status"] not in QUEUED | {"running"}:
            return done("inactive")
        acquired = try_lock(conn, run_id)
        if not acquired:
            return done("lock_busy")
        run, early = claim(conn, run_id, generation)
        if early:
            return done(**early)
        # Current customer binding is server-owned; no model/client value selects it.
        bound_customer = conn.execute("""
            SELECT t.customer_id FROM threads h JOIN tickets t USING (tenant_id,ticket_id)
            WHERE h.tenant_id=%s AND h.thread_id=%s AND h.actor_id=%s
        """, (run["tenant_id"], run["thread_id"], run["actor_id"])).fetchone()["customer_id"]
        run["customer_id"] = run.get("confirmed_fields", {}).get("customer_id") or bound_customer
        gate = Gate(conn, run_id, generation)
        try:
            gate.check("before_graph")
            outcome = runner(conn, run, gate)
            gate.check("after_graph")
        except StopFragment as exc:
            outcome = FragmentOutcome(exc.status, exc.reason_code)
        try:
            with event_hints.transaction(conn):
                current = gate._current(check_execution=False)
                finalized = finish_locked(conn, current, outcome)
        except Exception as error:
            # The remote receipt was committed separately. A rollback of local
            # ticket/result/events must expose pending finalization, not failure.
            if run['config_snapshot'].get('graph_version') != 'grant-execution-v1' or conn.closed or conn.broken:
                raise
            from .grant_effects import mark_finalization_pending
            trace('finalization_pending',run_id=run_id,error_type=type(error).__name__)
            with event_hints.transaction(conn):
                current = gate._current(check_execution=False)
                pending = mark_finalization_pending(conn,current)
                if pending is None:
                    raise
                finalized = finish_locked(conn,current,pending)
        if run['config_snapshot'].get('graph_version') == 'grant-execution-v1':
            from .grant_effects import after_finalize_commit
            after_finalize_commit(conn,finalized,outcome)
        return done("executed", status=finalized["status"], reason_code=finalized["reason_code"])
    except StaleFragment:
        return done("stale")
    except (psycopg.OperationalError, psycopg.InterfaceError) as exc:
        return done("connection_lost" if conn.closed or conn.broken else "fragment_error",
                    error_type=type(exc).__name__)
    except Exception as exc:
        # The accepted active reservation remains durable for the scanner. Error
        # strings can contain model text or bound SQL values and are never logged.
        return done("fragment_error", error_type=type(exc).__name__)
    finally:
        if acquired:
            unlock(conn, run_id)


def scan_one(conn, run_id: str, checkpoint_probe: CheckpointProbe) -> dict:
    identity = {"run_id": run_id, "backend_pid": conn.info.backend_pid}
    if not try_lock(conn, run_id):
        return {**identity, "outcome": "RECOVERY_BLOCKED"}
    try:
        # Reading a checkpoint happens under the session lock, outside a business
        # transaction. The graph adapter must not execute nodes in this probe.
        existing = conn.execute("""
            SELECT r.*,t.customer_id FROM runs r
            JOIN threads h USING (tenant_id,thread_id,actor_id)
            JOIN tickets t USING (tenant_id,ticket_id) WHERE r.run_id=%s
        """, (run_id,)).fetchone()
        if existing is None or existing["status"] not in QUEUED | {"running"} | WAITING:
            return {**identity, "outcome": "inactive"}
        session_identity(conn, run_id, existing["execution_generation"])
        dispatch = None
        if existing['config_snapshot'].get('graph_version') == 'grant-execution-v1':
            from .grant_effects import effect_outcome, read_dispatch
            dispatch = read_dispatch(conn, existing)
        has_effect = dispatch is not None and dispatch['status'] != 'rejected'
        projection = None
        if existing["status"] == "running" and not has_effect:
            projection = checkpoint_probe(conn, existing)
        with event_hints.transaction(conn):
            _, run = locked_run(conn, run_id)
            if (run is None or run["execution_generation"] != existing["execution_generation"]
                    or run["status"] not in QUEUED | {"running"} | WAITING):
                return {**identity, "outcome": "stale"}
            if run["status"] in WAITING:
                if run["status"] == "awaiting_approval" and run["config_snapshot"].get("graph_version") in {"grant-proposal-v1", "grant-approval-v1", "grant-execution-v1"}:
                    from .proposal_waiting import expire_wait
                else:
                    from .waiting import expire_wait
                expired = expire_wait(conn, run)
                return {**identity, "outcome": "wait_expired" if expired is not None else "inactive"}
            stopped = stop_reason(conn, run)
            if has_effect and (stopped or run["status"] != "running"):
                finalized = finish_locked(conn, run, effect_outcome(conn, run, row=dispatch))
                return {**identity, "outcome": "effect_projected", "status": finalized["status"],
                        "reason_code": finalized["reason_code"]}
            if stopped is not None or projection is not None:
                outcome = stopped or projection
                finalized = finish_locked(conn, run, outcome)
                return {**identity, "outcome": "projected", "status": finalized["status"],
                        "reason_code": finalized["reason_code"]}
            if run["status"] in QUEUED:
                if run["claimed_generation"] != run["execution_generation"]:
                    kind = "resume" if run["status"] == "resume_queued" else "create"
                    sent = conn.execute("""
                        SELECT outbox_id FROM outbox
                        WHERE app='supportflow' AND tenant_id=%s AND run_id=%s
                          AND execution_generation=%s AND kind=%s AND status='sent'
                          AND last_attempt_at<=clock_timestamp()-%s
                        FOR UPDATE
                    """, (run["tenant_id"], run_id, run["execution_generation"],
                          kind, QUEUED_REDELIVERY_AGE)).fetchone()
                    if sent is not None:
                        # A worker can ACK after losing its connection before the
                        # claim. Reuse the accepted queued permit and generation.
                        # Pending rows retain the dispatcher's existing backoff.
                        rearm(conn, run, delay_seconds=1)
                        return {**identity, "outcome": "queued_redelivery",
                                "generation": run["execution_generation"]}
                return {**identity, "outcome": "queued"}
            if run["claimed_generation"] != run["execution_generation"]:
                # A sent recovery message is still unclaimed. Reuse its generation.
                rearm(conn, run)
                return {**identity, "outcome": "recovery_pending", "generation": run["execution_generation"]}
            if run["recovery_count"] >= 2:
                finish_locked(conn, run, FragmentOutcome("handoff", "RECOVERY_EXHAUSTED"))
                return {**identity, "outcome": "recovery_exhausted"}
            run = conn.execute("""
                UPDATE runs SET execution_generation=execution_generation+1,recovery_count=recovery_count+1,
                    reason_code='RECOVERING',heartbeat_at=clock_timestamp(),
                    version=version+1,last_event_seq=last_event_seq+1
                WHERE run_id=%s AND execution_generation=%s RETURNING *
            """, (run_id, run["execution_generation"])).fetchone()
            rearm(conn, run, delay_seconds=0)
            append_event(conn, run, "run.recovering", {"status": "running", "reason_code": "RECOVERING"})
            return {**identity, "outcome": "recovering", "generation": run["execution_generation"]}
    finally:
        unlock(conn, run_id)


def scan_once(conn, checkpoint_probe: CheckpointProbe, *, limit: int = 32, stale_after_seconds: float = 15) -> list[dict]:
    if not 1 <= limit <= 128 or not 0 <= stale_after_seconds <= 300:
        raise ValueError("Invalid scanner bounds")
    candidates = conn.execute("""
        SELECT r.run_id FROM runs r WHERE
          (r.status='running' AND (r.heartbeat_at IS NULL OR r.heartbeat_at < clock_timestamp()-%s))
          OR (r.status IN ('awaiting_input','awaiting_approval') AND r.wait_expires_at<=clock_timestamp())
          OR (r.status IN ('queued','resume_queued') AND (
              r.budget_origin_at + (r.budget_ms+r.accumulated_wait_ms)*interval '1 millisecond' <= clock_timestamp()
              OR (r.claimed_generation IS DISTINCT FROM r.execution_generation AND EXISTS (
                  SELECT 1 FROM outbox o WHERE o.app=r.app AND o.tenant_id=r.tenant_id AND o.run_id=r.run_id
                    AND o.execution_generation=r.execution_generation
                    AND o.kind=CASE WHEN r.status='resume_queued' THEN 'resume' ELSE 'create' END
                    AND o.status='sent' AND o.last_attempt_at<=clock_timestamp()-%s
              ))))
        ORDER BY r.created_at,r.run_id LIMIT %s
    """, (timedelta(seconds=stale_after_seconds), QUEUED_REDELIVERY_AGE, limit)).fetchall()
    results = []
    for candidate in candidates:
        result = scan_one(conn, candidate["run_id"], checkpoint_probe)
        trace("scanner", **result)
        results.append(result)
    return results
