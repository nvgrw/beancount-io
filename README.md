# Self-hosted branch guide

This branch is a focused, single-host Beancount.io deployment. It intentionally
differs from `main`; it is not a general replacement for the hosted product.
The deployment target is Linux AMD64 with Docker Compose, a host-managed
Cloudflare Tunnel, Cloudflare Access, and trusted private ledger repositories.

## Differences from `main`

### Product surface

- `mobile/` is removed. This branch ships the web dashboard, backend, Git
  service, and ledger runtime only.
- Self-hosted mode is unlimited. Backend ledger, collaborator, directive, AI,
  and API-key quota checks use unlimited sentinels; the dashboard hides billing
  and quota indicators.
- Stripe is not configured by the self-hosted Compose target. The dashboard
  skips subscription-quota GraphQL requests, Stripe environment variables are
  absent, and the backend omits Stripe webhooks and subscription resolvers when
  `SELF_HOSTED_UNLIMITED=true`.

### Authentication and ingress

- Cloudflare Access is the identity boundary for browser, API, mobile-OAuth
  compatibility, and smart Git HTTPS traffic.
- Backend-v2 validates the Access assertion issuer, audience, and signature,
  then provisions the corresponding local and Gitea user.
- Password, signup, reset, magic-link, and refresh-token ceremonies are disabled
  while Access mode is configured.
- The dashboard forwards Access identity during SSR and uses Access logout.
- Caddy serves plain HTTP only on `127.0.0.1:8004`; host-managed cloudflared is
  the public ingress and TLS endpoint. No public origin listener is configured.
- Gitea's web UI is bound only to `127.0.0.1:9091` for SSH-tunneled maintenance.

### Git

- Smart Git HTTPS is routed through backend-v2 on the application hostname.
- Backend-v2 maps a verified Access identity to internal Gitea credentials and
  enforces the main-branch policy.
- `scripts/git-credential-cloudflare-access.py` provides Managed OAuth, dynamic
  client registration, PKCE, loopback callback, refresh, revocation, and macOS
  Keychain storage for command-line Git.

### State and deployment

- Persistent state uses bind mounts below `deploy/docker/data/` rather than
  anonymous or named Compose volumes. This includes both PostgreSQL databases,
  Gitea, Redis, Caddy, and the Python ledger cache.
- The production Compose project has one public host binding: Caddy on loopback.
  Backend-v2, dashboard, ledger, Redis, and PostgreSQL remain private.
- `SELF_HOSTED_UNLIMITED=true` is the default deployment policy.
- The deployment supports a flattened server layout with `compose.yaml`,
  `Caddyfile`, `.env`, `data/`, and `source/` below one directory.

### Ledger runtime

- `backend-cluster/ledger-python` replaces the TypeScript/Rust-WASM ledger
  service in production while preserving the `ledger:8000` Compose service
  contract used by backend-v2.
- The runtime uses Python 3.12, Beancount, Beanquery, and the repository's
  compatible Fava modules. Native Python plugins execute during ledger load.
- Repositories may declare pinned plugin dependencies in
  `.beancountio-requirements.txt`. Repositories are trusted code in this private
  deployment.
- A root `.beancountio.json` may select a repository-relative `.bean` or
  `.beancount` entrypoint; otherwise `main.bean` is used.
- The Python service implements all canonical ledger API path/method pairs,
  including reports, journals, BQL, source edits, file and repository adapters,
  administration, webhooks, and legacy compatibility.
- Entry hashes are engine-specific and must not be persisted across a runtime
  switch.

### Ledger performance and resource policy

- Each worker has an eight-entry parsed-ledger LRU keyed by commit, entrypoint,
  effective date, and materialized root. Concurrent misses for one key are
  coalesced.
- Unique parse misses queue to a bounded depth of 32.
- Two Uvicorn workers provide process isolation and parallelism for trusted
  Python plugins. Each worker owns its imports, HTTP pool, queue, and LRU.
- Gitea requests share one lifespan-scoped `httpx.AsyncClient` per worker.
- BQL shell state is request-local.
- Projected checks and source edits use hard-linked copy-on-write workspaces on
  the ledger-cache filesystem.
- Ledger source limits match the prior Rust loader: 4,096 source files, 8 MiB
  per source file, 32 MiB aggregate, and 16 KiB for `.beancountio.json`.
- Materialized snapshots, dependency environments, temporary workspaces, and
  lock bookkeeping are bounded and evicted.

## Build a transfer bundle

### Prerequisites

Install Docker Engine or Docker Desktop with Compose, Git, Bash, `tar`, and
`gzip`. The build machine must have enough free space for all application and
dependency images. Start from a clean committed revision: the source archive is
made from `HEAD`, not from uncommitted files.

Create the build environment:

```zsh
cd deploy/docker
cp .env.example .env.build
chmod 600 .env.build
```

Set at least:

```dotenv
APP_DOMAIN=books.example.com
DOCKER_PLATFORM=linux/amd64
SELF_HOSTED_UNLIMITED=true
```

Replace every required `change-me` value so Compose validation succeeds. These
values are used to render Compose; `.env.build` is not included in the bundle.

From the repository root, run:

```zsh
scripts/build-self-hosted-bundle.sh
```

Optional paths:

```zsh
scripts/build-self-hosted-bundle.sh \
  --env-file deploy/docker/.env.build \
  --output-dir dist/self-hosted
```

The script:

1. refuses a dirty worktree;
2. validates Compose;
3. builds backend-v2, dashboard, and the Python ledger for Linux AMD64;
4. pulls the exact Caddy, Gitea, PostgreSQL, and Redis images required by
   Compose;
5. verifies every image is `linux/amd64`;
6. writes a compressed Docker image archive;
7. writes a server-ready deployment archive containing `compose.yaml`,
   `Caddyfile`, `.env.example`, the exact committed source, and `REVISION`;
8. writes a SHA-256 manifest for both archives.

Artifacts are revision-stamped under `dist/self-hosted/`:

```text
beancount-io-images-linux-amd64-<revision>.tar.gz
beancount-io-deploy-<revision>.tar.gz
beancount-io-transfer-<revision>.sha256
```

## Install on the server

Copy the three artifacts to the Linux host:

```zsh
scp dist/self-hosted/beancount-io-* user@server:/tmp/
```

On the server, identify the revision suffix and verify both archives before
loading or extracting:

```zsh
cd /tmp
revision=<revision>
sha256sum -c "beancount-io-transfer-$revision.sha256"
docker load < "beancount-io-images-linux-amd64-$revision.tar.gz"

sudo install -d -o "$USER" -g docker /srv/docker
sudo tar -xzf "beancount-io-deploy-$revision.tar.gz" -C /srv/docker
cd /srv/docker/beancount-io
```

Create the private runtime environment:

```zsh
cp .env.example .env
chmod 600 .env
```

Replace every `change-me` value. Set the real application domain and, when using
Cloudflare Access, both `CLOUDFLARE_ACCESS_ISSUER` and
`CLOUDFLARE_ACCESS_AUDIENCE`. Keep `SOURCE_ROOT=./source`,
`DOCKER_PLATFORM=linux/amd64`, and `SELF_HOSTED_UNLIMITED=true`.

Validate and start strictly from the loaded images:

```zsh
docker compose config --quiet
docker compose up -d --no-build --wait
docker compose ps --all
```

Expected one-shot services:

- `gitea-init` exits successfully after ensuring the administrator exists.
- `backend-migrate` exits successfully after applying database migrations.

Expected long-lived services:

- `caddy`
- `dashboard`
- `backend-v2`
- `ledger`
- `gitea`
- `postgres-gitea`
- `postgres-backend`
- `redis`

Point the host-managed Cloudflare Tunnel at `http://127.0.0.1:8004`. Do not add
a public origin listener or API bypass.

## Verify

```zsh
docker compose ps --all
docker compose logs --since 10m ledger backend-v2 dashboard
curl -I http://127.0.0.1:8004/
```

The dashboard and API should remain protected by Access. The ledger health
payload reports `engine: python-beancount` from inside the private network.

## Upgrade and rollback

Before an upgrade, retain image and Compose rollback points:

```zsh
docker tag beancount-io/backend-v2:selfhosted beancount-io/backend-v2:rollback
docker tag beancount-io/dashboard:selfhosted beancount-io/dashboard:rollback
docker tag beancount-io/ledger:selfhosted beancount-io/ledger:rollback
cp compose.yaml compose.yaml.rollback
```

Load a new bundle, replace `source/`, review `.env.example` changes, and run:

```zsh
docker compose config --quiet
docker compose up -d --no-build --wait
```

To roll back, restore `compose.yaml.rollback`, retag the rollback images to
`selfhosted`, and recreate the affected services. Bind-mounted data is not
removed by image rollback or `docker compose down`.

## Detailed operations

See [`deploy/docker/README.md`](deploy/docker/README.md) for first boot,
Cloudflare configuration, Python plugin dependencies, backups, routine
operations, optional Git-over-SSH, and service-specific rollback commands.