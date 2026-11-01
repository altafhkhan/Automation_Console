# AR Automation Console — notes for Claude

Multi-tenant B2B SaaS that automates accounts receivable: a Master Agent
coordinating seven AR sub-agents on top of a deterministic policy/approval
engine, with human sign-off on high-risk actions. Live at
https://console.qumaxa.com (admin UI at `/admin`). One of Qumaxa's two
products; the other is OCRDocFlow.

## Read these before changing anything
- `INSTRUCTIONS.md` — the brief: role, goal, audience, tone.
- `ARCHITECTURE.md` — the non-negotiable design principles (deterministic
  business rules, human approval for high-risk actions, tenant isolation,
  version everything, self-hosted where practical).
- `CONTEXT.md` — deployment model: thin installed connector at the customer
  site, all AI reasoning in the central Console.
- `skills.md` — running log of conventions, decisions and learnings. Append
  to it when you learn something that isn't obvious from the code.
- `installation-registration-design.md` — registration/licensing flow.
- `deploy/DEPLOY.md` — how the droplet is set up and how schema changes are
  applied (one-off `deploy/migration_<date>_<name>.sql` files; `schema.sql`
  is the final-state record).

## Stack and layout
- Python. `console_registration_api.py` is the Console API (FastAPI);
  `master_agent.py` → `ar_agent.py` → `sub_agents/` is the agent chain;
  `connector_daemon.py` / `installer_skeleton.py` are the customer-side
  pieces; `gp_client.py` (Dynamics GP via SQL Server) and `qbo_client.py`
  (QuickBooks Online) are the ERP connectors; `stripe_config.py` holds
  billing config. Static admin/portal UI lives in `static/`.
- Postgres, Docker Compose, nginx + Let's Encrypt — all in `deploy/`.
- The Console's nginx also fronts `ocrdocflow.qumaxa.com` (see
  `deploy/DEPLOY.md`, "Shared with OCRDocFlow").

## Working conventions
- Git remote: `altafhkhan/Automation_Console`, default branch `master`.
  Work on feature branches; never commit `deploy/.env` or any secret.
- Financial calculations, thresholds and approvals stay deterministic and
  policy-driven — never let an LLM decide them.
- Keep CURRENT/MVP clearly separated from PLANNED/ROADMAP in any
  user-facing text (reconciliation, finance reporting agents etc. are
  roadmap).
- Local GP test database: SQL Server Express `localhost\SQLEXPRESS`,
  database `GPTestCo`. Scratch test scripts live in the untracked
  `SQLEXPRESS — queries.txt`.
- Infra status and cross-project facts (droplet, DNS via cPanel, hosting)
  are tracked in Claude's memory and in `D:\Projects\qumaxa\CONTEXT.md`.

## Related projects
- `D:\Projects\ocrdocflow` — OCRDocFlow (sibling product, shares the droplet).
- `D:\Projects\qumaxa` — the qumaxa.com marketing site, which describes
  this product on `/products/`.
