#!/bin/sh
set -eu

host=${1:-books.example.com}

case "$host" in
  "" | *[!A-Za-z0-9.-]*)
    printf 'usage: %s [git-hostname]\n' "$0" >&2
    exit 2
    ;;
esac

script_dir=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
helper="$script_dir/git-credential-cloudflare-access.py"

case "$helper" in
  *"'"*)
    printf 'helper path cannot contain a single quote: %s\n' "$helper" >&2
    exit 1
    ;;
esac

if ! /usr/bin/git help --config | /usr/bin/grep -qx 'http.proactiveAuth'; then
  printf 'Git does not support http.proactiveAuth; upgrade Git before continuing.\n' >&2
  exit 1
fi

/usr/bin/python3 "$helper" --host "$host" capability >/dev/null

credential_key="credential.https://$host.helper"
proactive_key="http.https://$host.proactiveAuth"
helper_command="!exec /usr/bin/python3 '$helper' --host '$host'"

# The empty helper resets the system-wide osxkeychain helper for this host only.
/usr/bin/git config --global --replace-all "$credential_key" ''
/usr/bin/git config --global --add "$credential_key" "$helper_command"
/usr/bin/git config --global "$proactive_key" auto

printf 'Configured Cloudflare Access Git authentication for https://%s\n' "$host"
