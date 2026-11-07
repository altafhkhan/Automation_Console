# AR Automation Console — website demo

Two stages. Stage 1 works today with no server; stage 2 needs the droplet
(due back around 2026-11-22).

## Stage 1 — interactive demo page (ready now)

`demo/index.html` is a single self-contained HTML file: no build step, no
backend, no external requests. It reproduces the tenant portal
(`static/portal/index.html`) — same tabs, same look — on seeded sample
data for a fictional company ("Harbor & Pine Supplies Ltd.", QuickBooks
Online). An 8-step guided tour starts automatically (`#notour` in the URL
suppresses it). Visitors can approve/reject items, reply to inbox
messages and request a credit-limit change; every action lands in the
demo's audit log, and "Reset demo" restores the seed.

Honesty rules baked in (match `CLAUDE.md` "current vs roadmap"):
- Inbox messages show "pending human review (draft-only mode)" — the
  real product has no AI classifier or reply drafting yet.
- DSO and CEI show "Not available yet" with the reason, as the real
  reporting sub-agent does.
- Credit holds and limit changes are recommendations that go to the
  approval queue; nothing "applies" them.
- The demo bar says plainly that nothing is connected and nothing is sent.

### Putting it on qumaxa.com (website session)

1. Copy `demo/index.html` to `site/public/demo/ar-automation-console/index.html`
   in `D:\Projects\qumaxa` (Astro serves `public/` as-is, so the page is
   reachable at `https://qumaxa.com/demo/ar-automation-console/`).
2. On `site/src/pages/products/ar-automation-console.astro` add a
   secondary button next to "Request a Demo":
   `Try the interactive demo` → `/demo/ar-automation-console/`
   (open in the same tab; the page has its own "Request a real demo" link
   back to `/contact?topic=ar-automation-console`).
3. Optionally add the same link to the products index card
   (`site/src/data/site.ts`, the `ar-automation-console` product entry).
4. Redeploy per `phase11/namecheap-deploy.md`.

The page carries `<meta name="robots" content="noindex">` so it does not
compete with the product page in search.

### Updating the demo

Edit the `seed()` function in `index.html` for data, `TOUR` for the tour
text. Dates are relative to "today", so the aging buckets never go stale.
Open the file in a browser to check — nothing to build.

## Stage 2 — hosted sandbox on console.qumaxa.com (after droplet restore)

Goal: the same tour, but on the real portal with a real (sandbox) tenant,
so a prospect can click through live software.

1. **Seed a demo tenant** with the real registration flow: `POST /v1/leads`
   for "Harbor & Pine Supplies (demo)", register an installation with
   `erp_type = "QuickBooks Online"`, invite one portal user
   (`demo@qumaxa.com`, a known password published on the website page).
2. **Load sample data** by POSTing to the sync endpoints the connector
   uses (`/v1/sync/open-invoices`, `/v1/sync/customers`,
   `/v1/sync/cash-application`, `/v1/sync/customer-inbox`) with the same
   figures as `index.html`'s `seed()` — a small `demo/seed_demo_tenant.py`
   script, run from the droplet with the demo installation's token. Then
   run `master_agent.py` once so the agents create the real approval items.
3. **Nightly reset**: a cron entry on the droplet that deletes the demo
   tenant (`POST /v1/admin/tenants/{id}/delete`, which already exists) and
   re-runs the seed script, so every visitor starts from the same state and
   nothing a visitor types survives.
4. **Guard rails**: the demo tenant must not have email integration or a
   QBO connection configured (nothing can leave the box); Stripe stays in
   test mode for it; rate-limit the demo login.
5. **Website**: the "Try the interactive demo" button can then point at
   `https://console.qumaxa.com/portal` with the demo credentials shown
   beside it, or keep stage 1 as the first click and link the live
   sandbox from inside it.

Stage 2 is not built; it depends on the droplet, the Console database and
an admin key, none of which exist until the restore.
