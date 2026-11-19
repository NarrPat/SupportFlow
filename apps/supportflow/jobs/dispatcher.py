"""Bounded outbox polling; broker publication and its sent marker are separate."""
import argparse
from contextlib import closing
import logging
import os
import time

from .runtime import connect, trace


def dispatch_once(conn, publish, *, limit: int = 16) -> list[dict]:
    if not 1 <= limit <= 128:
        raise ValueError("Invalid dispatcher batch size")
    results = []
    for _ in range(limit):
        with conn.transaction():
            row = conn.execute("""
                SELECT o.* FROM outbox o JOIN runs r USING (run_id)
                WHERE o.status='pending' AND o.available_at<=clock_timestamp()
                  AND o.execution_generation=r.execution_generation
                  AND ((o.kind='recover_fragment' AND r.status='running'
                        AND r.claimed_generation IS DISTINCT FROM r.execution_generation)
                       OR (o.kind IN ('create','resume') AND r.status IN ('queued','resume_queued')))
                ORDER BY (o.kind='recover_fragment') DESC,o.available_at,o.created_at,o.outbox_id
                LIMIT 1 FOR UPDATE OF o SKIP LOCKED
            """).fetchone()
            if row is None:
                break
            # A crash while publishing leaves a bounded retry deadline. The
            # broker never participates in a transaction that owns run/slot rows.
            row = conn.execute("""
                UPDATE outbox SET attempts=attempts+1,last_attempt_at=clock_timestamp(),
                    dispatch_version=dispatch_version+1,
                    available_at=clock_timestamp()+interval '5 seconds'
                WHERE outbox_id=%s RETURNING *
            """, (row["outbox_id"],)).fetchone()
        identity = {"outbox_id": row["outbox_id"], "run_id": row["run_id"],
                    "generation": row["execution_generation"], "publisher_pid": os.getpid()}
        try:
            # Only IDs go into the task body; kind selects transport priority.
            publish(row["run_id"], row["execution_generation"], row["kind"])
        except Exception as exc:
            delay = min(30, 2 ** min(row["attempts"], 5))
            with conn.transaction():
                conn.execute("""
                    UPDATE outbox SET available_at=clock_timestamp()+%s*interval '1 second'
                    WHERE outbox_id=%s AND dispatch_version=%s
                """, (delay, row["outbox_id"], row["dispatch_version"]))
            result = {**identity, "outcome": "publish_error", "error_type": type(exc).__name__}
        else:
            trace("outbox_published", **identity)
            with conn.transaction():
                marked = conn.execute("""
                    UPDATE outbox SET status='sent' WHERE outbox_id=%s AND dispatch_version=%s
                """, (row["outbox_id"], row["dispatch_version"])).rowcount
            result = {**identity, "outcome": "sent" if marked else "rescheduled"}
        trace("dispatch_attempt", **result)
        results.append(result)
    return results


def publish(run_id: str, generation: int, kind: str):
    from .worker import app, QUEUE
    app.send_task("supportflow.run_fragment", args=[run_id, generation], queue=QUEUE,
                  priority=0 if kind == "recover_fragment" else 9, retry=False)


def main():
    parser = argparse.ArgumentParser(description="Dispatch committed SupportFlow outbox rows")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    while True:
        try:
            with closing(connect("dispatcher")) as conn:
                results = dispatch_once(conn, publish)
        except Exception as exc:
            trace("dispatcher_unavailable", error_type=type(exc).__name__)
            if args.once:
                return 1
        if args.once:
            return int(any(item["outcome"] == "publish_error" for item in results))
        time.sleep(1)


if __name__ == "__main__":
    raise SystemExit(main())
