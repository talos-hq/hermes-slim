# Talos Messaging deployment preparation

No deployment is performed by this change. Recommendation: use the existing Docker
host at **159.223.153.134**, with **talos.talosbot.ai** and a TLS reverse proxy.
This reuses existing capacity and keeps the profile database on persistent storage.
The host's containers, available ports, resources and DNS ownership have not been
inspected; check them before launch. Do not replace existing containers or listeners.

| Target | Concrete approach | Tradeoff |
| --- | --- | --- |
| Existing DigitalOcean droplet | Build the repo image, persist `/opt/data`, run gateway + dashboard under existing s6 supervision, proxy HTTPS to the web listener | Recommended: existing Docker capacity; operator owns upgrades, backups, TLS and firewall |
| Render | Paid Docker web service, persistent disk at `/opt/data`, dashboard listening on `0.0.0.0:$PORT`, gateway under s6 | Managed TLS and builds; paid persistent storage required, more image/startup integration work |
| `talos.talosbot.ai` | A record to the droplet, or CNAME to Render's supplied hostname | A stable origin in front of either host, not a compute target; changing origin requires new browser subscriptions |

Render exposes one HTTP port per service and supports Docker builds. Its persistent
disks require a paid service. See [web services](https://render.com/docs/web-services),
[Docker](https://render.com/docs/docker), [disks](https://render.com/docs/disks), and
[custom domains](https://render.com/docs/custom-domains). Use one instance with this
SQLite profile; do not scale replicas against separate disks.

## Existing runtime and important limits

`docker-compose.yml` runs `gateway run` with host networking and `/opt/data` as the
persistent profile. Its separate dashboard command binds to `127.0.0.1:9119`.
Alternatively the image can supervise its dashboard alongside the gateway using
`HERMES_DASHBOARD=true` (see `docker/s6-rc.d/dashboard/run`). Do not start both dashboard
variants on the same port. The OpenAI-compatible API server is a different listener:
`API_SERVER_KEY` does not authorize these push routes.

The FastAPI web/dashboard listener mounts `apps/talos-messaging/dist` at `/talos/`
(`hermes_cli/web_server_talos_messaging.py`). The stock Dockerfile builds other frontends
but does **not** build this PWA: explicitly include its build output in a derived image.
The authoritative REST paths are **`/api/push/v1/mailbox`**, not `/api/mailbox`.
`Idempotency-Key` enforces owner-scoped request deduplication; without it,
`message_id` in the JSON body provides the existing mailbox deduplication.

**Browser auth compatibility remains a release gate:** the current PWA sends
`X-Talos-Messaging-Key`; that credential only authorizes `/api/webapp/socket-ticket`,
not the push/mailbox routes. Its mailbox UI falls back from `/api/mailbox` to
`/api/push/mailbox` (the deprecated alias), but that does not fix authentication. A passing bearer smoke test
proves the backend path, not phone enrollment. Before advertising phone push, wire
browser enrollment/mailbox access to the existing authenticated session flow and
verify on a real device. This preparation does not change the auth boundary.

## Server configuration and secret provisioning

Keep one stable, backed-up profile and signing key. Secrets belong in the deployment
secret manager or the profile `.env`, never committed images or browser bundles.
Do not enable shell tracing while loading secrets.

| Setting | Required value / purpose |
| --- | --- |
| `HERMES_HOME` | `/opt/data` inside the image; persist the entire directory, including `state.db`, config and credentials |
| `TALOS_VAPID_PRIVATE_KEY` | Contents of the generated base64url raw P-256 private key file; server only, **not** a filename |
| `push.subject` in `config.yaml` | Operator contact URI, e.g. `mailto:ops@talosbot.ai`; required alongside signing key |
| `HERMES_DASHBOARD_SESSION_TOKEN` | Stable random deployment credential, at least 32 characters; literal secret or `file:/opt/data/secrets/dashboard-token` |
| `TALOS_API_TOKEN` | Connector credential: same literal deployment token, not the `file:` reference; smoke CLI reads this by default |
| `HERMES_DASHBOARD` | `true` for the single-container supervised-dashboard layout |
| `HERMES_DASHBOARD_HOST` | `127.0.0.1` with droplet host networking; `0.0.0.0` on Render |
| `HERMES_DASHBOARD_PORT` | `9119` on droplet, Render's assigned `$PORT` on Render |
| `HERMES_UID`, `HERMES_GID` | UID/GID owning persistent storage; ensure the runtime user can read secret files |
| Model/provider credentials | Existing gateway setup credentials if running agents, provisioned separately; smoke does not call a model |

In the existing profile `config.yaml`, merge:

```yaml
push:
  subject: mailto:ops@talosbot.ai
dashboard:
  public_url: https://talos.talosbot.ai
  session_token_auth:
    enabled: true
```

The bundled `plugins/dashboard_auth/session_token` provider registers when the deployment
token is provisioned and accepts bearer REST authentication on gated listeners. Verify
it appears in startup logs; do not disable the auth gate. A non-loopback public URL
engages authentication even when the backend binds loopback. The principal is
`deployment-token` with provider `session-token`; a connector `user_id` must match that
principal. A loopback-only token owner is different, so keep the public configuration
stable. OAuth/password browser sessions are separate identities with separate mailboxes.
Configure the Talos connector's existing `platforms.talos.extra.endpoint` as
`https://talos.talosbot.ai/api/push/v1/mailbox` and `dry_run: false`, and enable the plugin
through the existing plugin setup UX. VAPID is signing authority, not API authentication.

Generate a signing key in the managed Python environment (with `cryptography` installed):

```bash
python scripts/talos_vapid_keygen.py --private-key-out /secure/talos-vapid.key
# Alternative: set TALOS_VAPID_KEY_OUT to the destination path.
chmod 600 /secure/talos-vapid.key
```

The script refuses existing files, creates mode 0600, and prints only the public key
and loading instructions. Transfer the private file through your secret manager;
load its contents into `TALOS_VAPID_PRIVATE_KEY`. Do not paste secret literals into
shell commands. The output file is raw encoded material, not a dotenv file. Back it up
encrypted; rotating it requires browsers to resubscribe. Generate the dashboard token
separately via a secure secret manager and restrict its file to mode 0600 too.

## Droplet launch recipe (for later execution)

1. Audit existing Docker services and listeners, choose an unused loopback backend port,
   back up the intended profile, and arrange DNS and SSH access. Do not run these steps
   as part of preparing this PR.
2. Build a derived image from the repository Dockerfile, preserving its entrypoint and
   supervision. In a Node build stage, use the repository lockfile/workspace installation
   tooling, install the `apps/talos-messaging` workspace and run
   `node scripts/build/talos-messaging.mjs`. Copy the resulting `apps/talos-messaging/dist`
   to `/opt/hermes/apps/talos-messaging/dist` in the runtime image. Confirm `sw.js`,
   manifest and icons are present. Do not rely on ignored host `dist` being copied.
3. Prepare a dedicated Compose file using the derived image, `network_mode: host`,
   `restart: unless-stopped`, the profile bind mount to `/opt/data`, the settings above,
   and `command: ["gateway", "run"]`. Enable the image's supervised dashboard; omit the
   original separate dashboard service. Read secrets from a protected `env_file` or secret
   manager; never put them inline in Compose. Keep `/init` / the image entrypoint intact.
4. Configure a host Caddy/nginx listener with a valid publicly trusted certificate.
   Point DNS A `talos.talosbot.ai` to `159.223.153.134`; publish AAAA only if IPv6 works.
   Allow TCP 80/443 for certificate issuance and HTTPS, restrict SSH, and firewall backend
   ports. Resolve existing port conflicts before installing a second proxy.
5. Once the derived image, config, secrets and proxy are prepared, the application start
   command is `docker compose -f compose.talos.yml up -d`. Start/reload the reviewed proxy
   configuration through the host's existing service manager. Run smoke before phone rollout.

Copy these concrete templates to an operator-owned deployment directory (outside the
checkout). First prepare the base image from this checkout:

```bash
docker build -t hermes-agent:talos-base .
```

Save the following as `Dockerfile.talos` in that deployment directory:

```dockerfile
FROM hermes-agent:talos-base AS pwa
USER root
WORKDIR /opt/hermes
RUN node scripts/build/node-deps.mjs --source /opt/hermes --workspace apps/talos-messaging \
    && node scripts/build/talos-messaging.mjs
FROM hermes-agent:talos-base
COPY --from=pwa --chmod=a+rX /opt/hermes/apps/talos-messaging/dist /opt/hermes/apps/talos-messaging/dist
```

Save this as `compose.talos.yml` alongside it. Set `TALOS_PROFILE_DIR` to the absolute
profile directory and `TALOS_ENV_FILE` to an absolute mode-0600 dotenv file containing
the required secrets. Neither variable contains secret material itself. The profile
must already contain the public URL/subject configuration above and the dashboard
token file if using `file:`. These names are deployment-template inputs, not Hermes
runtime configuration. The template binds the dashboard to host loopback only.

```yaml
services:
  talos:
    build:
      context: .
      dockerfile: Dockerfile.talos
    image: hermes-agent:talos-messaging
    restart: unless-stopped
    network_mode: host
    volumes:
      - ${TALOS_PROFILE_DIR:?set absolute profile directory}:/opt/data
    env_file:
      - ${TALOS_ENV_FILE:?set absolute protected env file}
    environment:
      HERMES_HOME: /opt/data
      HERMES_DASHBOARD: "true"
      HERMES_DASHBOARD_HOST: 127.0.0.1
      HERMES_DASHBOARD_PORT: "9119"
      HERMES_UID: ${HERMES_UID:-10000}
      HERMES_GID: ${HERMES_GID:-10000}
    command: ["gateway", "run"]
```

After the base build, DNS/proxy and secret provisioning, the single application launch
command in that directory is `docker compose -f compose.talos.yml up -d --build`.
The derived build uses the repository's pinned workspace installer. These templates
have been reviewed but no container has been built or launched for this preparation.
Check that the runtime includes the `pywebpush` dependency group before launch
(`python -c 'import pywebpush, py_vapid'` inside the prepared image); provision it through
the repository PM workflow if absent, never through ad-hoc pip installation.

For a host Caddy installation, the essential site configuration is:

```caddyfile
talos.talosbot.ai {
    reverse_proxy 127.0.0.1:9119
}
```

Caddy passes request paths/headers and handles WebSocket upgrades; the backend must trust
the public hostname via `dashboard.public_url`. Use the existing auth gate for all API
requests. Keep dashboard HTML and administrative routes authenticated; never make a
loopback SPA (which can inject its session token) anonymously reachable. See Caddy's
[automatic HTTPS](https://caddyserver.com/docs/automatic-https) and
[reverse proxy](https://caddyserver.com/docs/caddyfile/directives/reverse_proxy) docs.
Do not log Authorization headers, cookies or install-link fragments. Do not cache mailbox,
public-key responses or authenticated API data; preserve `Cache-Control` response headers.

## Render alternative

Use the same derived Docker image with the PWA baked in. Create a paid Docker web service,
attach persistent disk `/opt/data`, provision the profile and secrets, and retain the
image entrypoint with command `gateway run`. Set `HERMES_DASHBOARD=true` and
`HERMES_DASHBOARD_HOST=0.0.0.0`. Map Render's `$PORT` to `HERMES_DASHBOARD_PORT` in a
startup wrapper that then execs the original entrypoint; dashboard defaults to 9119,
so setting `PORT` alone is insufficient. Do not bypass s6. Ensure the disk is writable
by the configured UID/GID. Set `dashboard.public_url` to the actual HTTPS hostname and
keep the deployment-token provider enabled. Use `/talos/` as a static health check;
it must return the built page, not a missing-build response. Confirm unauthenticated
push requests return 401 and authenticated smoke passes.

Render supplies an HTTPS `onrender.com` origin. For `talos.talosbot.ai`, add the custom
domain in Render, create the indicated CNAME, wait for certificate readiness, then set
the public URL consistently. Do not point DNS at a transient service IP. Keep the service
awake and preserve the disk across deploys; export backups before deleting the service.

## Validation and operational limits

After launch, with the connector token already supplied securely in `TALOS_API_TOKEN`:

```bash
python scripts/talos_push_smoke.py https://talos.talosbot.ai
# --token-env OTHER_SECRET_VARIABLE selects a different environment variable.
# --timeout 45 controls each HTTP request timeout in seconds.
```

No token command-line argument is accepted. HTTPS certificate verification remains on;
HTTP is allowed only for loopback stub tests. Redirects fail rather than forwarding the
credential. Exit 0 means all checks passed; exit 1 means a check failed; argument errors
exit 2. Each attempted HTTP check prints its exact status code, or `NO RESPONSE` on a
transport error. The test validates the VAPID public point, enrolls a fresh fake device,
writes/replays one `message_id`, scans cursor pages and verifies exactly one matching
row, then revokes the fake device even after mailbox failures. It leaves a uniquely
named smoke message in durable history; use a dedicated test principal/profile.

The reserved `example.invalid` fake endpoint cannot receive a push; it can cause bounded
push retries and make the first write slower. Existing subscriptions for the same
principal can receive a smoke wake-up. **A 2xx on a push endpoint is not delivery to a
device.** The CLI does not test the PWA build, service worker, connector plugin execution,
provider acceptance, notification permission or phone delivery. Check `/talos/sw.js`
serves JavaScript and the manifest/icons load over HTTPS; then complete real-device testing
including foreground, background and revoked-permission cases. Browser localhost may
permit service workers during development, but it is not a publicly reachable production
origin. Keep TLS, application auth, browser permission and VAPID configuration as separate
requirements. Push failure does not roll back a mailbox commit.


The canonical messaging API is `/api/push/v1`. The deprecated `/api/push`
alias remains available for this release; removal is deferred to a later release
with advance notice. Every alias response includes `Deprecation: true` and a
`Link` with `rel="successor-version"` pointing to its v1 endpoint.

| Endpoint | Contract / auth |
| --- | --- |
| `GET /api/push/v1/vapid-public-key` | Public VAPID key |
| `POST /api/push/v1/subscribe` | Register or replace a device subscription; browser credential/session or connector `subscribe:write` |
| `DELETE /api/push/v1/subscribe?device_id=…` | Revoke a device subscription |
| `POST /api/push/v1/mailbox` | Admit a message and attempt push delivery once; browser credential/session or connector `mailbox:write` |
| `GET /api/push/v1/mailbox?after=0&limit=50` | Cursor page with `messages`, `next_cursor`, `has_more` |
| `GET /api/push/v1/mailbox/{message_id}` | Owner's message record; otherwise 404 with `message_not_found` |
| `PATCH /api/push/v1/mailbox/{message_id}/read` | Mark the owner's message read |

`Idempotency-Key` is durable and scoped to the authenticated owner. Repeating
an identical JSON body under the same key returns 200 with `created: false`,
without another mailbox row or push. Object key ordering does not matter.
Changing any body field, including `message_id`, under that key returns 409
with `detail.code: idempotency_conflict`. Without the header, `message_id`
retains its existing deduplication behavior. The connector reuses its key and
body across retries.

Mailbox `limit` defaults to 50, accepts 1–200, and returns 422 outside that
range. `has_more` is determined by fetching one extra row. Pass `next_cursor`
as `after` to continue; an empty page preserves the input cursor. Single-message
lookup returns the same 404 for unknown IDs and IDs belonging to another owner.


### Connector credentials and request budgets

Create a connector token per agent on the installation that serves the API:

```bash
hermes talos-messaging connector-token create --name report-agent --scope mailbox:write
hermes talos-messaging connector-token create --name enrollment-agent --scope subscribe:write
hermes talos-messaging connector-token list
hermes talos-messaging connector-token revoke --token-id <id-from-create-or-list>
```

Creation prints JSON with the token **once**. Pass it as `Authorization: Bearer <token>`;
keep it in the connector's secret configuration and never log it. Repeat `--scope` to grant
both write operations. The default scope is `mailbox:write`. All other endpoints in the
table require the existing browser credential/session; connector grants cannot read mail,
delete subscriptions, or authorize other APIs. Missing scope returns 403 with
`detail.code: insufficient_scope`; revoked or unknown tokens return 401.

Tokens are installation/profile scoped. Metadata (id, label, scopes, creation and revocation
timestamps) and a SHA-256 digest live in the existing profile `state.db`, in
`push_connector_tokens`. Random 256-bit secrets make a digest appropriate; plaintext tokens
are never persisted and cannot be recovered by listing. Independent revocation retains audit
metadata. `browser_credential.on_rotate` revokes all active connectors in the rotating profile.
Tokens survive process restarts; changing the browser credential through its rotation hook
invalidates them. Connector writes target the installation's loopback browser mailbox
(`legacy-token-owner`, provider `session-token`); cookie/OAuth mailboxes remain separate.

Both POST operations and their legacy aliases use process-local sliding-window budgets:

```yaml
push:
  rate_limit:
    mailbox_owner: 60
    mailbox_ip: 300
    subscribe_owner: 10
    subscribe_ip: 60
    window_seconds: 60
    max_buckets: 4096
```

Set these under `push.rate_limit` in the profile's `config.yaml`. Each route has independent
buckets keyed by profile plus authenticated principal (connector id or browser owner), and
by profile plus ASGI peer IP. IP attempts count before authentication, including rejected
credentials; owner attempts count after authentication. Aliases share the v1 budgets.
Only trust forwarded IP normalization configured upstream; arbitrary forwarded headers are
ignored. All accesses prune expired entries; least-recently-used buckets are evicted at the
configured process-wide capacity. Each bucket retains at most its configured request budget.
429 responses include integer seconds in `Retry-After` and
`detail: {code: rate_limited, message: ...}`. Windows recover without a restart; process
restarts clear budgets. Multiple worker processes have independent limits, so public
multi-worker deployments also need a shared proxy limit. Capacity eviction makes this
best-effort protection, as with the existing password-login buckets.
