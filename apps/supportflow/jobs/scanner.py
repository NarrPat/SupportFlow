"""Recover released execution locks; stable waits and terminal runs are excluded."""
import argparse
from contextlib import closing
import logging
import time

from .event_retention import INTERVAL_SECONDS, configured_days, prune_once
from .core import scan_once
from .runtime import connect, trace


def checkpoint_probe(conn, run):
    from apps.supportflow.agent.run_graph import checkpoint_probe as probe
    return probe(conn, run)


def main():
    parser = argparse.ArgumentParser(description="Scan SupportFlow run recovery candidates")
    parser.add_argument("--once", action="store_true")
    args = parser.parse_args()
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    next_retention = 0.0
    while True:
        try:
            with closing(connect("scanner")) as conn:
                scan_once(conn, checkpoint_probe)
                from .grant_reconciliation import reconcile_once
                reconcile_once(conn,limit=1)
                if time.monotonic() >= next_retention:
                    report = prune_once(conn, days=configured_days())
                    trace('event_retention', cutoff=report['cutoff'],
                          deleted=sum(len(row['deleted_seqs']) for row in report['runs']))
                    next_retention = time.monotonic() + INTERVAL_SECONDS
        except Exception as exc:
            trace("scanner_unavailable", error_type=type(exc).__name__)
            if args.once:
                return 1
        if args.once:
            return 0
        time.sleep(2)


if __name__ == "__main__":
    raise SystemExit(main())
