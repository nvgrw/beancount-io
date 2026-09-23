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

## From-scratch deployment

This workflow builds a complete Linux AMD64 transfer bundle on one machine and
installs it on a fresh Docker host. The examples use `books.example.com` and
`user@server`; replace them with the deployment's application hostname and SSH
destination. The target host needs Docker Engine with Compose, an SSH account
that can run Docker, and a host-managed Cloudflare Tunnel.

### 1. Prepare the build environment

Install Docker Engine or Docker Desktop with Compose, Git, Bash, `tar`, and
`gzip` on the build machine. Allow enough free space for all application and
dependency images. Start from a clean committed revision: the source archive is
made from `HEAD`, not from uncommitted files.

Create the build environment:

```zsh
cd /path/to/beancount-io
cp deploy/docker/.env.example deploy/docker/.env.build
chmod 600 deploy/docker/.env.build
```

Set at least:

```dotenv
APP_DOMAIN=books.example.com
DOCKER_PLATFORM=linux/amd64
SELF_HOSTED_UNLIMITED=true
```

Replace every required `change-me` value so Compose validation succeeds. These
values are used to render Compose; `.env.build` is not included in the bundle.
`APP_DOMAIN` and `SELF_HOSTED_UNLIMITED` are compiled into the dashboard image,
so their build values must match the target deployment. Runtime secrets may be
generated independently on the target host.

Confirm that the committed revision is clean, then build from the repository
root:

```zsh
git status --short
scripts/build-self-hosted-bundle.sh
```

`git status --short` must produce no output. The bundle script also enforces
this requirement.

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
4. pulls the Caddy, Gitea, PostgreSQL, and Redis images required by Compose;
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

Verify both archives before transferring them. The example uses macOS
`shasum`; use `sha256sum -c` on Linux:

```zsh
revision=$(git rev-parse --short=12 HEAD)
(cd dist/self-hosted && \
  shasum -a 256 -c "beancount-io-transfer-$revision.sha256")
```

#### Apple Silicon image pulls

On Apple Silicon, Docker Compose may leave a host-native or incomplete
multi-platform dependency tag locally even when `DOCKER_PLATFORM=linux/amd64`.
The bundle script deliberately stops instead of packaging it. Inspect the
dependency tags when it reports a platform or `docker save` error:

```zsh
for image in \
  caddy:2-alpine \
  docker.gitea.com/gitea:1.24 \
  postgres:16-alpine \
  redis:7-alpine
do
  docker image inspect "$image" --format '{{.RepoTags}} {{.Os}}/{{.Architecture}}'
  docker save "$image" >/dev/null
done
```

If `docker pull --platform linux/amd64 <image>` does not replace the local tag,
use Buildx and `jq` to select its AMD64 manifest explicitly, then rerun the
bundle script:

```zsh
pull_amd64() {
  local image=$1 repository=${image%:*} digest
  digest=$(docker buildx imagetools inspect --raw "$image" |
    jq -r '.manifests[] | select(.platform.os == "linux" and .platform.architecture == "amd64") | .digest')
  test -n "$digest" && test "$digest" != null
  docker pull "$repository@$digest"
  docker tag "$repository@$digest" "$image"
}

pull_amd64 caddy:2-alpine
pull_amd64 docker.gitea.com/gitea:1.24
pull_amd64 postgres:16-alpine
pull_amd64 redis:7-alpine
```

### 2. Transfer and verify the bundle

Copy the three artifacts to the target Linux host:

```zsh
revision=$(git rev-parse --short=12 HEAD)
scp \
  "dist/self-hosted/beancount-io-images-linux-amd64-$revision.tar.gz" \
  "dist/self-hosted/beancount-io-deploy-$revision.tar.gz" \
  "dist/self-hosted/beancount-io-transfer-$revision.sha256" \
  user@server:/tmp/
```

Check `df -h /tmp /srv/docker` first. Some hosts mount `/tmp` as a small tmpfs;
in that case, create a private revision-specific directory below
`/srv/docker/.incoming-beancount-io/` and transfer the artifacts there instead.

On the target host, use the revision printed in the artifact names and verify
both archives before loading or extracting:

```zsh
cd /tmp
revision=<revision>
sha256sum -c "beancount-io-transfer-$revision.sha256"
docker load < "beancount-io-images-linux-amd64-$revision.tar.gz"

sudo install -d -m 0750 -o "$USER" -g docker /srv/docker
tar -xzf "beancount-io-deploy-$revision.tar.gz" -C /srv/docker
cd /srv/docker/beancount-io
```

The extracted directory contains `compose.yaml`, `Caddyfile`, `.env.example`,
the exact committed source under `source/`, and a `REVISION` file. Application
images are already loaded, so the target does not need to rebuild them.

### 3. Configure the target host

Create the private runtime environment:

```zsh
cp .env.example .env
chmod 600 .env
```

Replace every `change-me` value. Set the real application domain and, when using
Cloudflare Access, both `CLOUDFLARE_ACCESS_ISSUER` and
`CLOUDFLARE_ACCESS_AUDIENCE`. Keep `SOURCE_ROOT=./source`,
`DOCKER_PLATFORM=linux/amd64`, and `SELF_HOSTED_UNLIMITED=true`.

Redis recommends memory overcommit for reliable background persistence. Apply
it once on a new host and persist it across reboots:

```zsh
printf 'vm.overcommit_memory = 1\n' |
  sudo tee /etc/sysctl.d/99-redis.conf >/dev/null
sudo sysctl --system
```

Persistent service state will be created below
`/srv/docker/beancount-io/data/`. Back up that directory together with `.env`;
neither is included in transfer bundles.

### 4. Start the stack

Validate Compose and start strictly from the loaded images:

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

### 5. Connect Cloudflare

Point the application hostname in the host-managed Cloudflare Tunnel at
`http://127.0.0.1:8004`. Protect the complete hostname with one Cloudflare
Access application, including `/api-gateway/*` and smart Git HTTPS paths. Do
not add a public origin listener, origin-IP DNS record, API bypass, or service
token exception.

When native OAuth or the Git credential helper is required, enable Managed
OAuth and dynamic client registration for the Access application. The detailed
Access policy and callback requirements are in
[`deploy/docker/README.md`](deploy/docker/README.md#cloudflare-tunnel-and-access).

### 6. Verify the complete request path

```zsh
docker compose ps --all
docker compose logs --since 10m ledger backend-v2 dashboard

app_domain=$(sed -n 's/^APP_DOMAIN=//p' .env)
curl --fail --show-error --silent \
  -H "Host: $app_domain" \
  http://127.0.0.1:8004/api-gateway/v1/health

docker compose exec -T backend-v2 \
  wget -qO- http://localhost:4104/healthz
```

The first request exercises Caddy and backend-v2 and returns `"OK"`. The second
is backend-v2's private deep-readiness check; it must report `healthy` for
PostgreSQL, Redis, Gitea, and the ledger service. A direct ledger health request
is useful only when this aggregate check reports the ledger as unhealthy.

Before authenticating, requesting `https://<APP_DOMAIN>` from outside the host
must produce a Cloudflare Access login redirect rather than the dashboard. Sign
in through Access, open the dashboard, create or open a ledger, and perform one
ledger read. That final action verifies the complete Cloudflare → Caddy →
backend-v2 → Gitea/Python-ledger path and provisions the corresponding local
and Gitea account on first use.

For a non-interactive check that behaves like a browser:

```zsh
curl --silent --show-error --output /dev/null \
  --write-out '%{http_code}\n' \
  --user-agent 'Mozilla/5.0' \
  --header 'Accept: text/html' \
  "https://$app_domain/"
```

Expect `302`. A generic API client may receive `401` instead. Protected ledger
operations require a genuine Access assertion; the Gitea administrator and
`x-admin-token` are not authentication bypasses for end-to-end smoke tests.

## Upgrade and rollback

Before an upgrade, retain revision-specific image, descriptor, and source
rollback points. Include dependency images because these example tags follow
patch releases and may resolve to new image IDs between bundles:

```zsh
rollback=$(cut -c1-12 REVISION)
docker tag beancount-io/backend-v2:selfhosted "beancount-io/backend-v2:rollback-$rollback"
docker tag beancount-io/dashboard:selfhosted "beancount-io/dashboard:rollback-$rollback"
docker tag beancount-io/ledger:selfhosted "beancount-io/ledger:rollback-$rollback"
docker tag caddy:2-alpine "caddy:rollback-$rollback"
docker tag docker.gitea.com/gitea:1.24 "docker.gitea.com/gitea:rollback-$rollback"
docker tag postgres:16-alpine "postgres:rollback-$rollback"
docker tag redis:7-alpine "redis:rollback-$rollback"
cp compose.yaml "compose.yaml.before-$rollback"
cp Caddyfile "Caddyfile.before-$rollback"
cp REVISION "REVISION.before-$rollback"
cp -a source "source.rollback-$rollback"
```

Load and extract the new bundle, move its `source/` into place, review
`.env.example` changes, and run:

```zsh
docker compose config --quiet
docker compose up -d --no-build --wait
```

To roll back, restore the revision-specific descriptors and source directory,
retag the required rollback images to their normal Compose tags, and recreate
the affected services. Bind-mounted data is not removed by image rollback or
`docker compose down`.

## Detailed operations

See [`deploy/docker/README.md`](deploy/docker/README.md) for first boot,
Cloudflare configuration, Python plugin dependencies, backups, routine
operations, optional Git-over-SSH, and service-specific rollback commands.
