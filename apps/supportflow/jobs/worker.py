"""Celery's task body carries only the run identifier and execution generation."""
from contextlib import closing
import os
import re

from celery import Celery

from .core import run_fragment
from .runtime import broker_url, connect, trace


QUEUE = os.environ.get("SF_CELERY_QUEUE", "supportflow")
if not re.fullmatch(r"sf[a-z0-9_.-]{0,62}|supportflow", QUEUE):
    raise ValueError("Expected a SupportFlow queue namespace")
app = Celery("supportflow", broker=broker_url())
app.conf.update(
    task_serializer="json", accept_content=["json"], result_serializer="json",
    task_ignore_result=True, task_acks_late=True, worker_prefetch_multiplier=1,
    task_default_queue=QUEUE, broker_connection_timeout=2,
    broker_transport_options={"queue_order_strategy": "priority", "priority_steps": [0, 9],
                              "visibility_timeout": 120, "socket_connect_timeout": 2,
                              "socket_timeout": 2, "global_keyprefix": QUEUE + ":"},
    task_publish_retry=False,
)


def runner(conn, run, gate):
    # Test bootstrap may replace this function in its own worker process. Product
    # code never imports fixtures and exposes no HTTP switch for that replacement.
    from apps.supportflow.agent.models import from_snapshot
    from apps.supportflow.agent.run_graph import execute_fragment
    return execute_fragment(conn, run, gate, model=from_snapshot(run["config_snapshot"]))


@app.task(name="supportflow.run_fragment")
def execute(run_id: str, generation: int):
    if (not isinstance(run_id, str) or not 1 <= len(run_id) <= 128
            or type(generation) is not int or generation < 1):
        return {"outcome": "invalid_message"}
    try:
        with closing(connect("worker")) as conn:
            return run_fragment(conn, run_id, generation, runner)
    except Exception as exc:
        trace("worker_unavailable", error_type=type(exc).__name__)
        return {"run_id": run_id, "generation": generation, "outcome": "dependency_unavailable"}
