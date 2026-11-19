"""Bounded worker ping without importing the graph, model or task module."""
import os
import re
import socket
from celery import Celery
from .runtime import broker_url


def worker_ready(client, hostname):
    node='sf-worker@'+hostname
    replies=client.control.inspect(destination=[node],timeout=2).ping()
    return replies=={node:{'ok':'pong'}}


def broker_options(queue):
    if not re.fullmatch(r'sf[a-z0-9_.-]{0,62}|supportflow',queue):
        raise ValueError('Expected a SupportFlow queue namespace')
    return {'global_keyprefix':queue+':','socket_connect_timeout':1,'socket_timeout':2}


def main():
    try:
        with Celery('supportflow-health',broker=broker_url()) as client:
            client.conf.update(broker_connection_timeout=1,broker_connection_retry_on_startup=False,
                broker_transport_options=broker_options(os.environ.get('SF_CELERY_QUEUE','supportflow')))
            return 0 if worker_ready(client,socket.gethostname()) else 1
    except Exception:
        # Health failure must not print a broker URL or credentials.
        return 1


if __name__=='__main__':raise SystemExit(main())
