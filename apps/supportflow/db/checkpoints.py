"""Provision Saver tables with the migrator, outside Alembic's migration transaction."""
import os
import psycopg
from psycopg.rows import dict_row
from langgraph.checkpoint.postgres import PostgresSaver


def main():
    database = os.environ['PGDATABASE']
    if database != 'sf_app' and not (database.startswith('sf_eval_') and database.endswith('_app')):
        raise ValueError('Expected a SupportFlow app database')
    with psycopg.connect(user='sf_app_migrator', autocommit=True, row_factory=dict_row) as conn:
        PostgresSaver(conn).setup()
    print('Synchronous PostgresSaver schema ready (migrator).')


if __name__ == '__main__': main()
