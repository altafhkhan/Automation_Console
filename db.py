"""
Shared Postgres bootstrap — one place for the connection string and the
jsonb codec, so console_registration_api.py, master_agent.py, and
ar_agent.py all talk to the same database the same way.
"""

import json
import os

import asyncpg

DATABASE_URL = os.environ.get(
    "DATABASE_URL", "postgresql://console:console@localhost:5432/console"
)


async def _init_connection(conn: asyncpg.Connection) -> None:
    # asyncpg treats jsonb as opaque text by default — this codec makes
    # jsonb columns round-trip as plain Python dicts/lists.
    await conn.set_type_codec(
        "jsonb", encoder=json.dumps, decoder=json.loads, schema="pg_catalog"
    )


async def create_pool() -> asyncpg.Pool:
    return await asyncpg.create_pool(DATABASE_URL, init=_init_connection)
