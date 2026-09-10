"""
AI Automation Master Agent — top-level coordinator (ARCHITECTURE.md /
INSTRUCTIONS.md). For the current MCP scope (AR only), it fans out to
the AR Agent for every installation still entitled to run. Once Finance
Reconciliation or other business-module agents exist, they'd be
dispatched here too — the same way SUB_AGENT_SYNCERS in
connector_daemon.py adds ERP adapters without touching the daemon's main
loop.

Not an HTTP endpoint — meant to run on a schedule (cron), same pattern as
run_license_watcher() in console_registration_api.py. Invoke standalone
via `python master_agent.py`. See deploy/DEPLOY.md.
"""

import asyncpg

from ar_agent import run_ar_agent
from db import create_pool


async def run_master_agent_cycle(pool: asyncpg.Pool) -> None:
    async with pool.acquire() as conn:
        installations = await conn.fetch(
            "select * from installations where license_status in ('trial', 'active')"
        )
        for installation in installations:
            processed = await run_ar_agent(conn, installation)
            if processed:
                await conn.execute(
                    """
                    insert into audit_log (installation_id, agent, action, detail)
                    values ($1, 'master_agent', 'dispatched_ar_agent', $2)
                    """,
                    installation["installation_id"],
                    {"events_processed": processed},
                )


if __name__ == "__main__":
    import asyncio

    async def _main() -> None:
        pool = await create_pool()
        try:
            await run_master_agent_cycle(pool)
        finally:
            await pool.close()

    asyncio.run(_main())
