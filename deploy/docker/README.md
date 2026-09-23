# deploy/docker — single-host Docker Compose deployment

Run the full Beancount.io service on a general-purpose Docker host: dashboard,
backend API, ledger service, Gitea, two PostgreSQL databases, Redis, and Caddy.
This target is intended for a persistent Linux server or VM. For local macOS
development, use [`../docker-mac/`](../docker-mac/) instead.

The topology follows Docker's production guidance: application code stays in
the images, state lives under `./data`, services restart automatically,
readiness is health-gated, and container logs are bounded. Caddy serves HTTP
only on `127.0.0.1:8004`; host-managed cloudflared is the sole public ingress.

## Prerequisites

- A current Docker Engine with Docker Compose.
- A host-managed cloudflared tunnel.
- A checkout of this repository on the deployment host.
- One public DNS name, for example `books.example.com`. It serves the dashboard,
  `/api-gateway`, and smart Git HTTPS endpoints. The tunnel maps it to
  `http://127.0.0.1:8004`; Gitea stays internal.
- No inbound host ports are required.
- Enough resources to build three Node images and run eight long-lived
  containers. Start with 4 GB RAM and monitor the host under your workload.
- At least 50 GB of free disk before the first build. The current backend
  Dockerfile installs its full dependency tree, so its image and BuildKit cache
  alone can consume tens of gigabytes; budget additional space for ledger data.

## First boot

```zsh
cd deploy/docker
cp .env.example .env
chmod 600 .env
```

Edit `.env`:

1. Set `APP_DOMAIN`.
2. Replace every `change-me` value. `openssl rand -hex 32` produces values that
   are both strong and safe inside the generated PostgreSQL URI.
3. Keep `SELF_HOSTED_UNLIMITED=true` to disable Stripe billing and grant every
   local user unlimited ledgers, collaborators, directives, AI quota, and API
   keys. The dashboard omits subscription controls in this mode.
4. Configure optional integrations only when you need them. The committed
   BlockEden placeholder lets the API boot but does not enable AI.

Validate without starting containers, then build and start the stack:

```zsh
docker compose config --quiet
docker compose up -d --build
docker compose ps --all
```

Two one-shot services should finish with exit code 0:

- `gitea-init` creates the Gitea administrator idempotently.
- `backend-migrate` applies pending backend database migrations before the API
  is allowed to start.

Once the health checks pass, open:

| Service   | URL                                        |
| --------- | ------------------------------------------ |
| Dashboard | `https://<APP_DOMAIN>`                     |
| API       | `https://<APP_DOMAIN>/api-gateway/`        |
| Git HTTPS | `https://<APP_DOMAIN>/<owner>/<repo>.git`  |

### OAuth signing key

Set `OAUTH_JWKS` in `.env` to a JSON JWK set stored only on the host (never in
git). Empty or invalid → backend boots and serves legacy login/API traffic, but
every `/oauth/*` and well-known OAuth metadata route returns `503
oauth_not_configured`, so the native mobile app cannot complete discovery.
`DASHBOARD_URL` and `SERVER_URL` are both derived from `APP_DOMAIN` in Compose;
see [`backend-v2` OAuth deployment contract](../../backend-cluster/backend-v2/README.md#oauth-deployment-contract)
for the reverse-proxy well-known routes.

## Cloudflare Tunnel and Access

Point the application tunnel hostname at `http://127.0.0.1:8004`. The Compose
stack publishes no other origin port. Set these values in `.env`:

```dotenv
CLOUDFLARE_ACCESS_ISSUER=https://your-team.cloudflareaccess.com
CLOUDFLARE_ACCESS_AUDIENCE=the-access-application-aud-tag
```

Do not add an origin IP DNS record or another listener, because that would
create a path around Access.

Configure Cloudflare Zero Trust as follows:

1. Keep the Authelia portal in its own Access application, using the desired
   Cloudflare account authentication policy.
2. Create one self-hosted Access application for the Beancount hostname and
  select Authelia as its identity provider.
3. Protect every application path, including `/api-gateway/*`. Do not add an
   API bypass or service-token exception for the iOS app.
4. Enable Managed OAuth and dynamic client registration for the Beancount
  Access application. Allow the exact redirect
  `https://<APP_DOMAIN>/oauth/callback` for iOS, and enable **Allow loopback
  clients** for the macOS Git helper's `127.0.0.1` callback. Both clients
  discover Cloudflare's protected-resource metadata and dynamically register
  a public PKCE client; neither stores a client secret.
5. Add one narrowly scoped bypass for
   `/.well-known/apple-app-site-association` when shipping the iOS app. Apple
   must fetch this static app-link voucher without an interactive login. It
   contains only the Apple Team ID, bundle ID, and allowed paths; no API or
   financial data is exposed.

Access injects `Cf-Access-Jwt-Assertion` after authentication. Backend-v2
validates its signature, issuer, and exact application audience, then
provisions the matching local and Gitea account. In Access mode, missing or
invalid assertions cannot become an application identity, and Beancount's
password, magic-link, signup, reset, and refresh ceremonies are disabled.
Browser traffic uses the Access cookie; iOS uses the Managed OAuth bearer and
refresh token. Both become the same validated Access identity at the origin.

Git over HTTPS uses the same identity path. On macOS, configure the
[repository-provided Cloudflare Access credential helper](../../docs/GIT_CLOUDFLARE_ACCESS.md)
so Git sends a Managed OAuth Bearer token proactively. Smart Git requests then
pass through backend-v2, which translates the verified Access identity to the
user's internal Gitea credentials.

`OAUTH_JWKS` may remain empty in this mode because Cloudflare, rather than
Beancount, is the native authorization server. It is still required if the
built-in Beancount OAuth provider is exposed to any other client.

## AMD64 deployment

Every service defaults to `DOCKER_PLATFORM=linux/amd64`. Builds made on Apple
Silicon therefore run on an x86_64 host, and builds made directly on an x86_64
host remain native. Keep this value in `.env`:

```dotenv
DOCKER_PLATFORM=linux/amd64
```

For a checkout under `/srv/docker/beancount-io` on the target server:

```zsh
cd /srv/docker/beancount-io/deploy/docker
docker compose config --quiet
docker compose build --pull
docker compose up -d
docker compose ps --all
```

Verify a locally built image with:

```zsh
docker image inspect beancount-io/backend-v2:selfhosted \
  --format '{{.Os}}/{{.Architecture}}'
```

The expected value is `linux/amd64`.

## Python ledger runtime

The production `ledger` service is built from
`backend-cluster/ledger-python`. It loads repositories with Python Beancount
and the project-compatible Fava modules, so ordinary Python plugins declared by
the ledger execute during every load. Repository snapshots are read from
Gitea's data directory through a read-only mount; materialized snapshots and
installed plugin dependencies are cached under
`./data/ledger-python-cache`.

The service runs two Uvicorn worker processes by default. Each process owns an
eight-entry parsed-ledger LRU and coalesces concurrent misses for the same
commit; unique parse misses queue to a depth of 32. Materialized snapshots keep
the 16 most recently used commits per repository, and dependency environments
keep 32 requirement digests. Override `LEDGER_WORKERS`,
`PARSED_LEDGER_CACHE_ENTRIES`, `PARSED_LEDGER_QUEUE_DEPTH`,
`MATERIALIZED_SNAPSHOTS_PER_REPO`, or `DEPENDENCY_CACHE_ENTRIES` in `.env` after
measuring memory and workload behavior on the deployment host.

Ledger repositories are trusted code in this self-hosted deployment. A
repository may add `.beancountio-requirements.txt` at its root to install extra
Python packages into a cache keyed by that file's SHA-256 digest. Package
installation runs only when a new digest is first loaded. Keep the file pinned
and review plugin code before granting access to a repository.

To rebuild and recreate only the ledger service:

```zsh
cd /srv/docker/beancount-io
docker compose build ledger
docker compose up -d --no-deps --force-recreate --wait ledger
docker compose ps ledger
```

Before a runtime upgrade, retain the current image under a rollback tag and
copy the Compose file. A rollback does not touch Gitea or either database:

```zsh
docker tag beancount-io/ledger:selfhosted beancount-io/ledger:rollback
cp compose.yaml compose.yaml.rollback

# Restore compose.yaml.rollback, then:
docker tag beancount-io/ledger:rollback beancount-io/ledger:selfhosted
docker compose up -d --no-deps --force-recreate --wait ledger
```

### Build once and transfer to another server

The three project images can be built on one machine, saved in one archive, and
loaded on another Docker host. Persistent volumes and `.env` secrets are not
included in the image archive.

On the build machine, start from a committed revision of the repository. Create
a build-only environment file and set the destination's public hostnames before
building. `APP_DOMAIN` and `SELF_HOSTED_UNLIMITED` are compiled into the
dashboard bundle, so changing either requires rebuilding that image.

```zsh
cd deploy/docker
cp .env.example .env.build
chmod 600 .env.build

# Edit at least these build inputs in .env.build:
# APP_DOMAIN=books.example.com
# DOCKER_PLATFORM=linux/amd64
# SELF_HOSTED_UNLIMITED=true

docker compose --env-file .env.build config --quiet
docker compose --env-file .env.build build backend-v2 dashboard ledger
```

Verify every project image has the destination architecture, then package the
images and the exact committed source revision. Keeping the source beside the
images lets the destination validate Compose and rebuild later; it contains no
`.env` file or Docker volume data.

```zsh
for image in \
  beancount-io/backend-v2:selfhosted \
  beancount-io/dashboard:selfhosted \
  beancount-io/ledger:selfhosted
do
  docker image inspect "$image" --format '{{.RepoTags}} {{.Os}}/{{.Architecture}}'
done

repo_root=$(git rev-parse --show-toplevel)
output_dir="$(dirname "$repo_root")"

docker save \
  beancount-io/backend-v2:selfhosted \
  beancount-io/dashboard:selfhosted \
  beancount-io/ledger:selfhosted \
  | gzip -1 > "$output_dir/beancount-io-images-linux-amd64.tar.gz"

git -C "$repo_root" archive \
  --format=tar.gz \
  --output="$output_dir/beancount-io-source.tar.gz" \
  HEAD

cd "$output_dir"
shasum -a 256 \
  beancount-io-images-linux-amd64.tar.gz \
  beancount-io-source.tar.gz \
  > beancount-io-transfer.sha256
```

The example uses macOS `shasum`. On a Linux build machine, use
`sha256sum` instead. Transfer all three files:

```zsh
scp \
  beancount-io-images-linux-amd64.tar.gz \
  beancount-io-source.tar.gz \
  beancount-io-transfer.sha256 \
  user@server:/tmp/
```

On the destination Linux server, verify before extracting or loading. Adjust
ownership/group arguments to match that host's Docker administration policy.

```zsh
cd /tmp
sha256sum -c beancount-io-transfer.sha256

sudo install -d -o "$USER" -g docker /srv/docker/beancount-io/source
sudo tar -xzf beancount-io-source.tar.gz \
  -C /srv/docker/beancount-io/source

docker load < beancount-io-images-linux-amd64.tar.gz

for image in \
  beancount-io/backend-v2:selfhosted \
  beancount-io/dashboard:selfhosted \
  beancount-io/ledger:selfhosted
do
  docker image inspect "$image" --format '{{.RepoTags}} {{.Os}}/{{.Architecture}}'
done
```

Configure and start from the transferred source. Use `--no-build` so Compose
uses the loaded application images rather than rebuilding them.

```zsh
cd /srv/docker/beancount-io/source/deploy/docker
cp .env.example .env
chmod 600 .env
# Replace every change-me value and configure domains and Access settings.

docker compose config --quiet
docker compose pull caddy gitea postgres-gitea postgres-backend redis
docker compose up -d --no-build --wait
docker compose ps --all
```

For a flattened bundle with `compose.yaml` beside a `source/` directory, set
`SOURCE_ROOT=./source` in its `.env`. This keeps later builds pointed at the
transferred source tree.

## iOS build for this deployment

Use an Apple bundle ID owned by your Developer team. The same bundle ID and
Team ID must be present in the app entitlement and the backend's AASA response.
Set the server and verified HTTPS callback before Expo generates the Xcode
project.

For a local Xcode build, the Mac needs Node 20.19.4+, Yarn Classic, CocoaPods,
Xcode, and Apple signing configured:

```zsh
cd mobile
export EXPO_PUBLIC_SERVER_URL=https://books.example.com/
export EXPO_PUBLIC_OAUTH_REDIRECT_URL=https://books.example.com/oauth/callback
export EXPO_IOS_BUNDLE_IDENTIFIER=com.example.beancount
yarn install
yarn expo prebuild --platform ios --clean
open ios/Beancount.xcworkspace
```

In Xcode, select the Beancount target, choose your Team under Signing &
Capabilities, confirm the bundle identifier, and build for a connected iPhone.
The generated target must retain the Associated Domains entitlement for
`applinks:<APP_DOMAIN>`. `yarn ios:device` performs the prebuild/build/device
flow from the terminal once Xcode signing and CocoaPods are available.

When Node and CocoaPods must not be installed on the Mac, run an EAS build from
a disposable Node container instead. The build itself runs on Expo's macOS
workers; authenticate with Expo and Apple directly at the interactive prompts,
or provide an `EXPO_TOKEN` through your shell without committing it:

```zsh
cd mobile
docker volume create beancount-mobile-modules
docker run --rm -it \
  -e EXPO_PUBLIC_SERVER_URL=https://books.example.com/ \
  -e EXPO_PUBLIC_OAUTH_REDIRECT_URL=https://books.example.com/oauth/callback \
  -e EXPO_IOS_BUNDLE_IDENTIFIER=com.example.beancount \
  -e EXPO_TOKEN \
  -v "$PWD:/app" \
  -v beancount-mobile-modules:/app/node_modules \
  -w /app node:22-bookworm sh -lc \
  'corepack enable && yarn install --frozen-lockfile && npx --yes eas-cli@latest build --platform ios --profile production'
```

Do not pass Expo or Apple credentials on the command line or store them in the
repository. A local Xcode compile cannot be completed with Xcode alone: this
Expo project intentionally builds native camera modules from source, so its
CocoaPods install must run on macOS rather than in a Linux container.

Set the matching deployment values on the target server:

```dotenv
APP_LINKS_APPLE_TEAM_ID=YOUR10CHARTEAMID
APP_LINKS_IOS_BUNDLE_ID=com.example.beancount
```

After deploying and adding the exact AASA bypass, verify the voucher without a
Cloudflare session:

```zsh
curl -fsS https://books.example.com/.well-known/apple-app-site-association
```

It must return JSON containing
`YOUR10CHARTEAMID.com.example.beancount` and `/oauth/callback`. A signed device
build cannot receive the HTTPS OAuth callback until that public file is valid,
the domain entitlement matches, and Cloudflare Managed OAuth allows the same
redirect URI.

If startup stops at either one-shot service, inspect it directly:

```zsh
docker compose logs gitea-init backend-migrate
docker compose logs backend-v2 ledger gitea
```

## Network and data model

Only Caddy publishes HTTP ports. Dashboard, backend-v2, and Gitea share its
edge network but expose no host ports. Ledger, both PostgreSQL services, and
Redis are reachable only on an internal Compose network. Gitea's SSH listener
is also internal unless you explicitly enable the policy-enforcing SSH overlay.

Persistent state lives beside the Compose file under `./data`:

- `data/caddy/data` and `data/caddy/config` — certificates and Caddy state.
- `data/gitea` — Git repositories, attachments, and Gitea configuration.
- `data/postgres-gitea` and `data/postgres-backend` — relational data.
- `data/redis` — append-only Redis state, including authentication data.

`docker compose down` and `docker compose down -v` both preserve bind-mounted
data. A destructive reset requires explicitly deleting `./data`.

For a simple consistent backup, stop the stack and archive the deployment
configuration and data tree together. `sudo` is required because PostgreSQL
keeps its directories private to its container UID.

```zsh
cd /srv/docker/beancount-io
docker compose down
timestamp=$(date -u +%Y%m%dT%H%M%SZ)
sudo tar --acls --xattrs -czf "../beancount-io-$timestamp.tar.gz" \
  compose.yaml Caddyfile .env data
docker compose up -d --no-build --wait
```

Restore only into an empty deployment while the stack is down, then start and
verify every health check. For online backups, use application-aware tools
(`gitea dump`, `pg_dump`, and Redis persistence) and test the restore procedure.
Always include `.env`: its Gitea encryption keys are required to read persisted
secrets.

## Routine operations

```zsh
docker compose ps --all
docker compose logs -f                 # add a service name to filter
docker compose up -d                   # apply environment/config changes
docker compose up -d --build           # rebuild after source changes
docker compose pull caddy gitea postgres-gitea postgres-backend redis
docker compose up -d                   # roll forward pulled dependency images
docker compose down                    # stop while preserving data
```

For a repository upgrade, back up first, review the release diff, pull the
desired commit, and run `docker compose up -d --build`. Compose recreates the
changed app containers; `backend-migrate` reruns its idempotent migration before
the new backend starts. Changing `APP_DOMAIN` requires a dashboard rebuild
because the browser-facing same-origin API URL is compiled into its bundle.

The default dependency image tags follow patch releases within their selected
major/minor lines. Pin full tags or image digests in `.env` if your rollout
policy requires byte-for-byte repeatability.

## Git over SSH (optional)

Git over HTTPS works by default. To publish SSH, backend-v2 must terminate the
connection so its write policy applies. It must present Gitea's existing private
host key; substituting a new key triggers the same client warning as a
man-in-the-middle attack.

After the base stack is healthy:

```zsh
mkdir -p tmp
./print-ssh-host-key.sh > tmp/gitea-host-key
chmod 600 tmp/gitea-host-key
```

Add the key to `.env` as one single-quoted multiline value:

```dotenv
SSH_PROXY_HOST_KEY='-----BEGIN OPENSSH PRIVATE KEY-----
...
-----END OPENSSH PRIVATE KEY-----'
```

Then publish the configured `SSH_PORT` (2222 by default):

```zsh
docker compose -f docker-compose.yml -f docker-compose.ssh.yml config --quiet
docker compose -f docker-compose.yml -f docker-compose.ssh.yml up -d backend-v2
rm -f tmp/gitea-host-key
```

Open that TCP port in the host firewall. Continue using both `-f` arguments for
future Compose operations while SSH is enabled. Disable it by bringing the
overlay project down and starting the base file again; do not rotate the host
key unless you also plan a known-hosts migration for every client.

## Configuration notes and limitations

- `.env` contains live credentials. It is gitignored; keep mode 0600 and never
  paste its values into logs, issues, or Compose files.
- The Gitea administrator is created only when absent. Changing
  `FAVA_API_ADMIN_PASSWORD` later does not change the existing Gitea password;
  update both deliberately with Gitea's admin CLI.
- Changing either database password in `.env` does not rotate the password of
  an already-initialized PostgreSQL role. Perform a coordinated database-role
  rotation; an environment-only change will break connectivity.
- Do not set `ANTHROPIC_BASE_URL` to an empty string. The backend's Anthropic
  SDK rejects an empty URL at startup. Export a real non-empty value in the
  Compose environment only when using a compatible endpoint.
- The optional Discourse OIDC client is not exposed here. Its issuer and
  callback are currently tied to the official `beancount.io` deployment.
- This is a single-host deployment, not an HA control plane. Docker Compose
  does not provide multi-node failover, managed backups, or zero-downtime
  database upgrades.

## Design references

- [Docker: use Compose in production](https://docs.docker.com/compose/how-tos/production/)
- [Docker: health-gated startup order](https://docs.docker.com/compose/how-tos/startup-order/)
- [Gitea: installation with Docker](https://docs.gitea.com/next/installation/install-with-docker/)
- [Caddy: automatic HTTPS](https://caddyserver.com/docs/automatic-https)
