# Git over Cloudflare Access on macOS

The repository includes a credential helper for Git over HTTPS when the Git
origin is protected by Cloudflare Access Managed OAuth. The first Git request
opens Cloudflare login in the default browser. Later requests reuse or refresh
the grant stored in macOS Keychain.

The helper requires macOS, `/usr/bin/python3`, and a Git version that supports
both `http.proactiveAuth` and the `authtype` credential capability. It installs
no packages and writes no token to Git configuration or the filesystem.

## Configure this Mac

From this repository checkout, run:

```zsh
./scripts/configure-git-cloudflare-access.sh books.example.com
```

The script changes global Git configuration only for
`https://books.example.com`. Other Git hosts continue using their existing
credential helpers.

Start the first login with a normal Git command:

```zsh
git ls-remote \
  https://books.example.com/alice/my-book.git
```

Complete the Authelia login in the browser. Git then receives an opaque Bearer
token; Cloudflare validates it and sends its signed Access assertion to
backend-v2. Backend-v2 maps that identity to the user's internal Gitea account.

Clone normally after the check succeeds:

```zsh
git clone \
  https://books.example.com/alice/my-book.git
```

## Sign out

Revoke the refresh grant and remove the local Keychain record:

```zsh
./scripts/git-credential-cloudflare-access.py \
  --host books.example.com logout
```

The next Git request starts a new browser login. Setting
`GIT_TERMINAL_PROMPT=0` prevents a new interactive login when no reusable grant
exists.

## Server requirements

- In the Access application's **Advanced settings**, **Managed OAuth** and
  **Dynamic client registration** are enabled, and **Allow loopback clients**
  is on for `127.0.0.1` callback URIs.
- Smart HTTP paths on the application hostname route through backend-v2's
  `/git/*` proxy. Gitea itself is not publicly exposed.
- There is no Access bypass for Git or API traffic.

The helper validates protected-resource and authorization-server metadata,
uses S256 PKCE and an ephemeral loopback callback, and accepts OAuth endpoints
only on the discovered issuer. Access and refresh tokens are stored as one
opaque JSON record in macOS Keychain through Git's built-in
`git-credential-osxkeychain` helper.

Confirm DCR before configuring a client. The authorization-server JSON must
contain `registration_endpoint`:

```zsh
curl -sS \
  https://your-team.cloudflareaccess.com/.well-known/oauth-authorization-server
```

If that field is absent, save the Managed OAuth settings again after enabling
dynamic client registration. The helper deliberately refuses to guess an
unadvertised endpoint.
