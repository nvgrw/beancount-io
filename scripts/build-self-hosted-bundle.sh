#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage: scripts/build-self-hosted-bundle.sh [options]

Build every image required by deploy/docker, then package the images and an
exact source/deployment archive for transfer to a Linux Docker host.

Options:
  --env-file PATH    Build environment (default: deploy/docker/.env.build)
  --output-dir PATH  Artifact directory (default: dist/self-hosted)
  --help             Show this help
EOF
}

repo_root=$(git rev-parse --show-toplevel)
env_file="$repo_root/deploy/docker/.env.build"
output_dir="$repo_root/dist/self-hosted"
compose_file="$repo_root/deploy/docker/docker-compose.yml"

while (($#)); do
  case "$1" in
    --env-file)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      env_file=$2
      shift 2
      ;;
    --output-dir)
      [[ $# -ge 2 ]] || { usage >&2; exit 2; }
      output_dir=$2
      shift 2
      ;;
    --help|-h)
      usage
      exit 0
      ;;
    *)
      printf 'Unknown option: %s\n' "$1" >&2
      usage >&2
      exit 2
      ;;
  esac
done

[[ -f "$env_file" ]] || {
  printf 'Build environment not found: %s\n' "$env_file" >&2
  printf 'Copy deploy/docker/.env.example there and set APP_DOMAIN first.\n' >&2
  exit 2
}

if [[ -n "$(git -C "$repo_root" status --porcelain --untracked-files=normal)" ]]; then
  printf 'Refusing to bundle a dirty worktree; commit the exact source first.\n' >&2
  exit 1
fi

if docker compose version >/dev/null 2>&1; then
  compose=(docker compose)
elif command -v docker-compose >/dev/null 2>&1; then
  compose=(docker-compose)
else
  printf 'Docker Compose is required (docker compose or docker-compose).\n' >&2
  exit 127
fi

for command in docker git gzip tar; do
  command -v "$command" >/dev/null || {
    printf 'Required command not found: %s\n' "$command" >&2
    exit 127
  }
done

if command -v shasum >/dev/null 2>&1; then
  checksum=(shasum -a 256)
elif command -v sha256sum >/dev/null 2>&1; then
  checksum=(sha256sum)
else
  printf 'shasum or sha256sum is required.\n' >&2
  exit 127
fi

compose_args=(--env-file "$env_file" -f "$compose_file")
"${compose[@]}" "${compose_args[@]}" config --quiet
"${compose[@]}" "${compose_args[@]}" build backend-v2 dashboard ledger
"${compose[@]}" "${compose_args[@]}" pull \
  caddy gitea postgres-gitea postgres-backend redis

images=()
while IFS= read -r image; do
  [[ -n "$image" ]] && images+=("$image")
done < <("${compose[@]}" "${compose_args[@]}" config --images | sort -u)
[[ ${#images[@]} -gt 0 ]] || {
  printf 'Compose returned no image names.\n' >&2
  exit 1
}

for image in "${images[@]}"; do
  platform=$(docker image inspect "$image" --format '{{.Os}}/{{.Architecture}}')
  [[ "$platform" == "linux/amd64" ]] || {
    printf 'Image %s has platform %s, expected linux/amd64.\n' "$image" "$platform" >&2
    exit 1
  }
done

mkdir -p "$output_dir"
temporary=$(mktemp -d "${TMPDIR:-/tmp}/beancount-self-hosted.XXXXXX")
trap 'rm -rf "$temporary"' EXIT

revision=$(git -C "$repo_root" rev-parse HEAD)
short_revision=$(git -C "$repo_root" rev-parse --short=12 HEAD)
images_archive="$output_dir/beancount-io-images-linux-amd64-$short_revision.tar.gz"
deploy_archive="$output_dir/beancount-io-deploy-$short_revision.tar.gz"
manifest="$output_dir/beancount-io-transfer-$short_revision.sha256"

printf 'Saving %s images...\n' "${#images[@]}"
docker save "${images[@]}" | gzip -1 > "$images_archive"

stage="$temporary/beancount-io"
mkdir -p "$stage/source"
git -C "$repo_root" archive HEAD | tar -xf - -C "$stage/source"
cp "$repo_root/deploy/docker/docker-compose.yml" "$stage/compose.yaml"
cp "$repo_root/deploy/docker/Caddyfile" "$stage/Caddyfile"
cp "$repo_root/deploy/docker/README.md" "$stage/README.md"
awk '
  /^SOURCE_ROOT=/ { print "SOURCE_ROOT=./source"; next }
  { print }
' "$repo_root/deploy/docker/.env.example" > "$stage/.env.example"
printf '%s\n' "$revision" > "$stage/REVISION"

tar -czf "$deploy_archive" -C "$temporary" beancount-io

(
  cd "$output_dir"
  "${checksum[@]}" \
    "$(basename "$images_archive")" \
    "$(basename "$deploy_archive")" \
    > "$(basename "$manifest")"
)

printf '\nCreated transfer artifacts:\n'
printf '  %s\n' "$images_archive" "$deploy_archive" "$manifest"
printf '\nCopy these files to the server, verify the manifest, load the images,\n'
printf 'extract the deployment archive under /srv/docker, configure .env, then run:\n'
printf '  docker compose up -d --no-build --wait\n'
