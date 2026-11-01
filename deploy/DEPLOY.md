# Deploying console.qumaxa.com

Scaffold status: gets the API + Postgres live behind HTTPS on a single
small VPS. No CI/CD, no zero-downtime deploy, no real migration tool
(schema.sql only applies on the database's first creation — see the note
at its top). Good enough to test the registration/heartbeat flow
end-to-end from a real installer, not yet a production posture.

## 1. Get a VPS

Any provider works since this is just Docker; pick one you're comfortable
with. Cheap, simple, Docker-friendly options: DigitalOcean, Hetzner,
Linode. Smallest tier is enough for this scaffold (1 vCPU / 1-2GB RAM).

Requirements on the box:
- A public IPv4 address
- Docker + Docker Compose installed
- Ports 80 and 443 open (most providers open these by default; check any
  firewall/security-group rule if the console is unreachable later)

## 2. Point console.qumaxa.com at the VPS

At your registrar's DNS management page (GoDaddy/Namecheap/whichever
holds qumaxa.com), add:

| Type | Host      | Value                  | TTL     |
|------|-----------|------------------------|---------|
| A    | `console` | `<your VPS public IP>` | default |

DNS propagation is usually minutes, occasionally up to ~1 hour. Verify
with:

```bash
nslookup console.qumaxa.com
```

Don't proceed to step 4 until this resolves to your VPS IP — certbot's
domain validation will fail otherwise.

## 3. Copy the project to the VPS and bring up HTTP only

```bash
# from your machine
scp -r D:\Projects\console user@<vps-ip>:/opt/console

# on the VPS — set a real Postgres password before first run
cd /opt/console/deploy
cp .env.example .env
nano .env   # set POSTGRES_PASSWORD to something real, not the placeholder

docker compose up -d --build
```

At this point `nginx/console.conf` only serves the ACME challenge path and
redirects everything else to HTTPS (which doesn't work yet — that's
expected until step 4 issues a real certificate).

`schema.sql` applies automatically the first time the `db` container
starts against an empty data volume. If you ever need to reset it during
testing: `docker compose down -v` drops the Postgres volume along with
everything else (nginx certs volumes too) — don't run that against a
real deployment with real tenant data.

## 4. Issue the TLS certificate

Run certbot once, using the shared webroot volume nginx is already
serving `/.well-known/acme-challenge/` from:

```bash
docker run --rm \
  -v deploy_certbot-webroot:/var/www/certbot \
  -v deploy_certbot-certs:/etc/letsencrypt \
  certbot/certbot certonly --webroot -w /var/www/certbot \
  -d console.qumaxa.com \
  --email <your-real-email> --agree-tos --no-eff-email
```

(Volume names get a prefix from the compose project name, which defaults
to the directory you run `docker compose` from — `deploy_` if you ran it
from inside `deploy/` as step 3 does. Run `docker volume ls` if they
don't match.)

## 5. Activate HTTPS

```bash
mv nginx/console-ssl.conf.example nginx/console-ssl.conf
docker compose restart nginx
```

Confirm:

```bash
curl https://console.qumaxa.com/v1/leads -X POST \
  -H "Content-Type: application/json" \
  -d '{"email":"test@example.com","company_name":"Test Co"}'
```

A `{"tenant_id": "..."}` response means the Console is live at
console.qumaxa.com.

## 6. Point the installer/daemon at it

Set the environment variable on any machine running
`installer_skeleton.py` or `connector_daemon.py` (they read this via
`console_config.py`):

```bash
export CONSOLE_BASE_URL=https://console.qumaxa.com
```

## Renewing the certificate

Let's Encrypt certs last 90 days. Add a cron job on the VPS to renew and
reload nginx, e.g. monthly:

```cron
0 3 1 * * docker run --rm -v deploy_certbot-webroot:/var/www/certbot -v deploy_certbot-certs:/etc/letsencrypt certbot/certbot renew --webroot -w /var/www/certbot >> /var/log/certbot-renew.log 2>&1 && docker compose -f /opt/console/deploy/docker-compose.yml restart nginx
```

## License watcher

`run_license_watcher()` (design doc Section 6) needs to run hourly against
the same database as the API — it's not an HTTP endpoint. Run it inside
the already-running `api` container so it shares the same `DATABASE_URL`:

```cron
0 * * * * cd /opt/console/deploy && docker compose exec -T api python console_registration_api.py >> /var/log/license-watcher.log 2>&1
```

## Master Agent

`master_agent.py` fans out to the AR Agent (`ar_agent.py`) for every
installation still entitled to run — same "not an HTTP endpoint, run on a
schedule" pattern as the license watcher, sharing the same container and
`DATABASE_URL`. Runs every 10 minutes:

```cron
*/10 * * * * cd /opt/console/deploy && docker compose exec -T api python master_agent.py >> /var/log/master-agent.log 2>&1
```

Install all three cron entries with `crontab -e` (or `(crontab -l; echo '...') | crontab -`
as used during initial setup).

## Applying a schema change to an already-running database

`schema.sql` only applies automatically on the `db` container's *first*
creation — there's no migration tool yet (see the note at the top of that
file). To change a live database's schema: write the `alter`/`create`
statements to a one-off file (this project's convention so far:
`deploy/migration_<date>_<name>.sql`, applied once and then removed —
`schema.sql` itself is the record of the final state, not the migration
file), then:

```bash
scp deploy/migration_20260908_orchestration.sql root@<vps-ip>:/opt/console/deploy/
ssh root@<vps-ip> "docker cp /opt/console/deploy/migration_20260908_orchestration.sql deploy-db-1:/tmp/migration.sql && docker exec deploy-db-1 psql -U console -d console -f /tmp/migration.sql"
```

Passing SQL to `psql` through an inline SSH heredoc (`ssh host "psql <<'EOF' ..."`)
is fragile — quoting across the SSH boundary breaks silently with no
error and no tables created. Always write the SQL to a file and copy it
over instead.

Also: recreating the `api` container after a code change leaves nginx
holding a stale internal IP for it (502 Bad Gateway) — always follow with
`docker compose restart nginx`.

## Shared with OCRDocFlow

This nginx also fronts `ocrdocflow.qumaxa.com` on the same droplet rather
than a second nginx running for it — see `../../ocrdocflow/deploy/DEPLOY.md`.
That's why `docker-compose.yml`'s nginx service mounts
`/opt/ocrdocflow/frontend/dist`, and why `nginx/` on the droplet holds
`ocrdocflow.conf` / `ocrdocflow-ssl.conf` (copied in from the OCRDocFlow
repo, not tracked here). If OCRDocFlow isn't deployed on the box, Docker
just creates that mount path empty and nginx starts normally.
