#!/usr/bin/env python3
"""Git credential helper for Cloudflare Access Managed OAuth on macOS."""

from __future__ import annotations

import argparse
import base64
import contextlib
import dataclasses
import fcntl
import hashlib
import http.server
import json
import os
import pathlib
import secrets
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.parse
import urllib.request
import webbrowser
from typing import Any, Callable, Dict, Generator, List, Mapping, Optional, TextIO


USER_AGENT = "git-credential-cloudflare-access/1.0"
KEYCHAIN_ACCOUNT = "oauth-session-v1"
MAX_RESPONSE_BYTES = 1024 * 1024
EXPIRY_SKEW_SECONDS = 60


class HelperError(RuntimeError):
    pass


class OAuthHttpError(HelperError):
    def __init__(self, message: str, oauth_error: Optional[str] = None):
        super().__init__(message)
        self.oauth_error = oauth_error


@dataclasses.dataclass(frozen=True)
class OAuthMetadata:
    origin: str
    resource: str
    issuer: str
    authorization_endpoint: str
    token_endpoint: str
    revocation_endpoint: str
    registration_endpoint: str


@dataclasses.dataclass(frozen=True)
class OAuthSession:
    origin: str
    resource: str
    issuer: str
    token_endpoint: str
    revocation_endpoint: str
    client_id: str
    access_token: str
    access_token_expires_at: int
    refresh_token: str

    @classmethod
    def from_json(cls, raw: str) -> Optional["OAuthSession"]:
        try:
            value = json.loads(raw)
        except (TypeError, json.JSONDecodeError):
            return None
        if not isinstance(value, dict) or value.get("version") != 1:
            return None
        string_fields = (
            "origin",
            "resource",
            "issuer",
            "token_endpoint",
            "revocation_endpoint",
            "client_id",
            "access_token",
            "refresh_token",
        )
        if any(not isinstance(value.get(field), str) for field in string_fields):
            return None
        expires_at = value.get("access_token_expires_at")
        if isinstance(expires_at, bool) or not isinstance(expires_at, int):
            return None
        return cls(
            origin=value["origin"],
            resource=value["resource"],
            issuer=value["issuer"],
            token_endpoint=value["token_endpoint"],
            revocation_endpoint=value["revocation_endpoint"],
            client_id=value["client_id"],
            access_token=value["access_token"],
            access_token_expires_at=expires_at,
            refresh_token=value["refresh_token"],
        )

    def to_json(self) -> str:
        return json.dumps(
            {"version": 1, **dataclasses.asdict(self)},
            separators=(",", ":"),
            sort_keys=True,
        )


def _single(attributes: Mapping[str, List[str]], key: str) -> Optional[str]:
    values = attributes.get(key, [])
    return values[-1] if values else None


def read_credential(stream: TextIO) -> Dict[str, List[str]]:
    attributes: Dict[str, List[str]] = {}
    for raw_line in stream:
        line = raw_line.rstrip("\n")
        if line.endswith("\r"):
            line = line[:-1]
        if not line:
            break
        if "=" not in line or "\x00" in line:
            raise HelperError("Git sent an invalid credential record")
        key, value = line.split("=", 1)
        attributes.setdefault(key, []).append(value)
    return attributes


def credential_matches_host(
    attributes: Mapping[str, List[str]], expected_host: str
) -> bool:
    return (
        _single(attributes, "protocol") == "https"
        and (_single(attributes, "host") or "").lower() == expected_host.lower()
    )


def write_bearer(token: str, stream: TextIO) -> None:
    if not token or "\n" in token or "\x00" in token:
        raise HelperError("OAuth server returned an invalid access token")
    stream.write("capability[]=authtype\n")
    stream.write("authtype=Bearer\n")
    stream.write(f"credential={token}\n")
    stream.write("ephemeral=1\n\n")


def origin_for_host(host: str) -> str:
    parsed = urllib.parse.urlsplit(f"https://{host}")
    if (
        not host
        or parsed.scheme != "https"
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in ("", "/")
        or parsed.query
        or parsed.fragment
    ):
        raise HelperError("--host must be a bare HTTPS hostname")
    return f"https://{parsed.netloc.lower()}"


def _well_known(kind: str, issuer: str) -> str:
    parsed = urllib.parse.urlsplit(issuer)
    suffix = parsed.path.strip("/")
    path = f"/.well-known/{kind}"
    if suffix:
        path = f"{path}/{suffix}"
    return urllib.parse.urlunsplit((parsed.scheme, parsed.netloc, path, "", ""))


def _endpoint_within_issuer(value: Any, issuer: str) -> str:
    if not isinstance(value, str):
        raise HelperError("Cloudflare OAuth metadata is missing an endpoint")
    endpoint = urllib.parse.urlsplit(value)
    issuer_url = urllib.parse.urlsplit(issuer)
    issuer_prefix = issuer_url.path.rstrip("/")
    if (
        endpoint.scheme != "https"
        or endpoint.username
        or endpoint.password
        or endpoint.netloc.lower() != issuer_url.netloc.lower()
        or (
            issuer_prefix
            and endpoint.path != issuer_prefix
            and not endpoint.path.startswith(f"{issuer_prefix}/")
        )
    ):
        raise HelperError("Cloudflare OAuth endpoint is outside its issuer")
    return urllib.parse.urlunsplit(endpoint)


def request_json(
    url: str,
    *,
    method: str = "GET",
    json_body: Optional[Mapping[str, Any]] = None,
    form_body: Optional[Mapping[str, str]] = None,
    timeout: float = 20,
) -> Dict[str, Any]:
    headers = {"Accept": "application/json", "User-Agent": USER_AGENT}
    data: Optional[bytes] = None
    if json_body is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(json_body, separators=(",", ":")).encode("utf-8")
    elif form_body is not None:
        headers["Content-Type"] = "application/x-www-form-urlencoded"
        data = urllib.parse.urlencode(form_body).encode("ascii")
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            raw = response.read(MAX_RESPONSE_BYTES + 1)
    except urllib.error.HTTPError as error:
        raw = error.read(MAX_RESPONSE_BYTES + 1)
        oauth_error: Optional[str] = None
        try:
            body = json.loads(raw.decode("utf-8"))
            if isinstance(body, dict) and isinstance(body.get("error"), str):
                oauth_error = body["error"]
        except (UnicodeDecodeError, json.JSONDecodeError):
            pass
        suffix = f": {oauth_error}" if oauth_error else ""
        raise OAuthHttpError(
            f"Cloudflare OAuth request failed with HTTP {error.code}{suffix}",
            oauth_error,
        ) from error
    except urllib.error.URLError as error:
        raise HelperError(f"Could not reach Cloudflare OAuth: {error.reason}") from error
    if len(raw) > MAX_RESPONSE_BYTES:
        raise HelperError("Cloudflare OAuth response is too large")
    try:
        body = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, json.JSONDecodeError) as error:
        raise HelperError("Cloudflare OAuth returned invalid JSON") from error
    if not isinstance(body, dict):
        raise HelperError("Cloudflare OAuth returned an invalid response")
    return body


def discover(origin: str) -> OAuthMetadata:
    resource_metadata = request_json(
        f"{origin}/.well-known/cloudflare-access-protected-resource/"
    )
    if resource_metadata.get("resource") != origin:
        raise HelperError("Cloudflare protected-resource metadata does not match Git")
    authorization_servers = resource_metadata.get("authorization_servers")
    if (
        not isinstance(authorization_servers, list)
        or len(authorization_servers) != 1
        or not isinstance(authorization_servers[0], str)
    ):
        raise HelperError("Cloudflare protected-resource metadata is incomplete")
    issuer = authorization_servers[0].rstrip("/")
    issuer_url = urllib.parse.urlsplit(issuer)
    if issuer_url.scheme != "https" or not issuer_url.hostname:
        raise HelperError("Cloudflare OAuth issuer is invalid")

    metadata = request_json(_well_known("oauth-authorization-server", issuer))
    if metadata.get("issuer", "").rstrip("/") != issuer:
        raise HelperError("Cloudflare OAuth issuer does not match its metadata")
    if "code" not in metadata.get("response_types_supported", []):
        raise HelperError("Cloudflare OAuth does not support authorization code flow")
    grants = metadata.get("grant_types_supported", [])
    if "authorization_code" not in grants or "refresh_token" not in grants:
        raise HelperError("Cloudflare OAuth does not support refresh tokens")
    if "S256" not in metadata.get("code_challenge_methods_supported", []):
        raise HelperError("Cloudflare OAuth does not support S256 PKCE")
    if "none" not in metadata.get("token_endpoint_auth_methods_supported", []):
        raise HelperError("Cloudflare OAuth does not support public clients")

    registration = metadata.get("registration_endpoint")
    if not isinstance(registration, str):
        raise HelperError(
            "Cloudflare Access dynamic client registration is not enabled"
        )
    return OAuthMetadata(
        origin=origin,
        resource=origin,
        issuer=issuer,
        authorization_endpoint=_endpoint_within_issuer(
            metadata.get("authorization_endpoint"), issuer
        ),
        token_endpoint=_endpoint_within_issuer(
            metadata.get("token_endpoint"), issuer
        ),
        revocation_endpoint=_endpoint_within_issuer(
            metadata.get("revocation_endpoint"), issuer
        ),
        registration_endpoint=_endpoint_within_issuer(registration, issuer),
    )


def register_client(metadata: OAuthMetadata, redirect_uri: str) -> str:
    response = request_json(
        metadata.registration_endpoint,
        method="POST",
        json_body={
            "redirect_uris": [redirect_uri],
            "token_endpoint_auth_method": "none",
            "grant_types": ["authorization_code", "refresh_token"],
            "response_types": ["code"],
            "resource": metadata.resource,
        },
    )
    client_id = response.get("client_id")
    if not isinstance(client_id, str) or not client_id:
        raise HelperError("Cloudflare client registration returned no client ID")
    return client_id


def build_authorization_url(
    metadata: OAuthMetadata,
    client_id: str,
    redirect_uri: str,
    state: str,
    code_challenge: str,
) -> str:
    parsed = urllib.parse.urlsplit(metadata.authorization_endpoint)
    query = urllib.parse.urlencode(
        {
            "response_type": "code",
            "client_id": client_id,
            "redirect_uri": redirect_uri,
            "state": state,
            "code_challenge": code_challenge,
            "code_challenge_method": "S256",
            "resource": metadata.resource,
        }
    )
    return urllib.parse.urlunsplit(
        (parsed.scheme, parsed.netloc, parsed.path, query, "")
    )


def _token_values(body: Mapping[str, Any]) -> tuple[str, int, Optional[str]]:
    access_token = body.get("access_token")
    token_type = body.get("token_type")
    expires_in = body.get("expires_in")
    refresh_token = body.get("refresh_token")
    if (
        not isinstance(access_token, str)
        or not access_token
        or not isinstance(token_type, str)
        or token_type.lower() != "bearer"
        or isinstance(expires_in, bool)
    ):
        raise HelperError("Cloudflare OAuth token response is invalid")
    try:
        lifetime = int(expires_in)
    except (TypeError, ValueError) as error:
        raise HelperError("Cloudflare OAuth token expiry is invalid") from error
    if lifetime <= 0 or (
        refresh_token is not None
        and (not isinstance(refresh_token, str) or not refresh_token)
    ):
        raise HelperError("Cloudflare OAuth token response is invalid")
    return access_token, lifetime, refresh_token


def exchange_code(
    metadata: OAuthMetadata,
    client_id: str,
    redirect_uri: str,
    code: str,
    code_verifier: str,
    now: Callable[[], float] = time.time,
) -> OAuthSession:
    body = request_json(
        metadata.token_endpoint,
        method="POST",
        form_body={
            "grant_type": "authorization_code",
            "client_id": client_id,
            "code": code,
            "code_verifier": code_verifier,
            "redirect_uri": redirect_uri,
            "resource": metadata.resource,
        },
    )
    access_token, lifetime, refresh_token = _token_values(body)
    if refresh_token is None:
        raise HelperError("Cloudflare OAuth returned no refresh token")
    return OAuthSession(
        origin=metadata.origin,
        resource=metadata.resource,
        issuer=metadata.issuer,
        token_endpoint=metadata.token_endpoint,
        revocation_endpoint=metadata.revocation_endpoint,
        client_id=client_id,
        access_token=access_token,
        access_token_expires_at=int(now()) + lifetime,
        refresh_token=refresh_token,
    )


def refresh_session(
    session: OAuthSession,
    now: Callable[[], float] = time.time,
) -> OAuthSession:
    body = request_json(
        session.token_endpoint,
        method="POST",
        form_body={
            "grant_type": "refresh_token",
            "refresh_token": session.refresh_token,
            "client_id": session.client_id,
            "resource": session.resource,
        },
    )
    access_token, lifetime, refresh_token = _token_values(body)
    return dataclasses.replace(
        session,
        access_token=access_token,
        access_token_expires_at=int(now()) + lifetime,
        refresh_token=refresh_token or session.refresh_token,
    )


class KeychainStore:
    def __init__(self, origin: str):
        digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:32]
        self.keychain_host = f"{digest}.git-credential-cloudflare-access.invalid"
        self.helper = self._find_helper()

    @staticmethod
    def _find_helper() -> str:
        git = "/usr/bin/git"
        try:
            result = subprocess.run(
                [git, "--exec-path"],
                check=True,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
            )
        except (OSError, subprocess.CalledProcessError) as error:
            raise HelperError("Could not locate Git's macOS Keychain helper") from error
        helper = pathlib.Path(result.stdout.strip()) / "git-credential-osxkeychain"
        if not helper.is_file() or not os.access(helper, os.X_OK):
            raise HelperError("Git's macOS Keychain helper is unavailable")
        return str(helper)

    def _record(self, password: Optional[str] = None) -> bytes:
        lines = [
            "protocol=https",
            f"host={self.keychain_host}",
            f"username={KEYCHAIN_ACCOUNT}",
        ]
        if password is not None:
            if "\n" in password or "\x00" in password:
                raise HelperError("Refusing to store invalid OAuth state")
            lines.append(f"password={password}")
        return ("\n".join(lines) + "\n\n").encode("utf-8")

    def _run(self, operation: str, password: Optional[str] = None) -> subprocess.CompletedProcess[bytes]:
        try:
            return subprocess.run(
                [self.helper, operation],
                input=self._record(password),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                check=False,
            )
        except OSError as error:
            raise HelperError("Could not access macOS Keychain") from error

    def load(self) -> Optional[OAuthSession]:
        result = self._run("get")
        if result.returncode != 0:
            return None
        try:
            attributes = read_credential(
                iter(result.stdout.decode("utf-8").splitlines(keepends=True))
            )
        except (UnicodeDecodeError, HelperError):
            return None
        password = _single(attributes, "password")
        return OAuthSession.from_json(password) if password is not None else None

    def save(self, session: OAuthSession) -> None:
        if self._run("store", session.to_json()).returncode != 0:
            raise HelperError("Could not save OAuth state in macOS Keychain")

    def erase(self) -> None:
        self._run("erase")


@contextlib.contextmanager
def host_lock(origin: str) -> Generator[None, None, None]:
    digest = hashlib.sha256(origin.encode("utf-8")).hexdigest()[:24]
    path = pathlib.Path(tempfile.gettempdir()) / (
        f"git-credential-cloudflare-access-{os.getuid()}-{digest}.lock"
    )
    descriptor = os.open(path, os.O_CREAT | os.O_RDWR, 0o600)
    try:
        os.fchmod(descriptor, 0o600)
        fcntl.flock(descriptor, fcntl.LOCK_EX)
        yield
    finally:
        fcntl.flock(descriptor, fcntl.LOCK_UN)
        os.close(descriptor)


def _pkce_challenge(verifier: str) -> str:
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def _new_pkce_pair() -> tuple[str, str]:
    while True:
        verifier = secrets.token_urlsafe(64)
        challenge = _pkce_challenge(verifier)
        if challenge[0].isalnum():
            return verifier, challenge


def interactive_login(metadata: OAuthMetadata, timeout: int) -> OAuthSession:
    callback: Dict[str, str] = {}
    expected_state = secrets.token_urlsafe(32)
    code_verifier, code_challenge = _new_pkce_pair()

    class CallbackHandler(http.server.BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            parsed = urllib.parse.urlsplit(self.path)
            parameters = urllib.parse.parse_qs(parsed.query)
            status = 200
            message = "Authorization received. You can close this tab."
            if parsed.path != "/callback":
                status = 404
                message = "Not found."
            elif parameters.get("state") != [expected_state]:
                status = 400
                message = "Authorization state did not match. Return to the terminal."
            elif "error" in parameters:
                callback["error"] = parameters["error"][0]
                status = 400
                message = "Authorization was not completed. Return to the terminal."
            elif len(parameters.get("code", [])) == 1:
                callback["code"] = parameters["code"][0]
            else:
                status = 400
                message = "Authorization code was missing. Return to the terminal."
            body = (
                "<!doctype html><meta charset=utf-8><title>Beancount Git</title>"
                f"<p>{message}</p>"
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, _format: str, *args: Any) -> None:
            del _format, args

    with http.server.HTTPServer(("127.0.0.1", 0), CallbackHandler) as server:
        redirect_uri = f"http://127.0.0.1:{server.server_port}/callback"
        client_id = register_client(metadata, redirect_uri)
        authorization_url = build_authorization_url(
            metadata,
            client_id,
            redirect_uri,
            expected_state,
            code_challenge,
        )
        print("Opening Cloudflare Access login in your browser...", file=sys.stderr)
        if not webbrowser.open(authorization_url, new=1, autoraise=True):
            print(f"Open this URL to continue:\n{authorization_url}", file=sys.stderr)
        deadline = time.monotonic() + timeout
        while "code" not in callback and "error" not in callback:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                raise HelperError("Timed out waiting for Cloudflare Access login")
            server.timeout = min(1.0, remaining)
            server.handle_request()
    if "error" in callback:
        raise HelperError(f"Cloudflare Access authorization failed: {callback['error']}")
    return exchange_code(
        metadata,
        client_id,
        redirect_uri,
        callback["code"],
        code_verifier,
    )


def _session_matches(session: OAuthSession, origin: str) -> bool:
    if session.origin != origin or session.resource != origin:
        return False
    try:
        _endpoint_within_issuer(session.token_endpoint, session.issuer)
        _endpoint_within_issuer(session.revocation_endpoint, session.issuer)
    except HelperError:
        return False
    return bool(session.client_id and session.refresh_token)


def access_token(
    origin: str,
    store: KeychainStore,
    timeout: int,
    now: Callable[[], float] = time.time,
) -> str:
    session = store.load()
    if session is not None and not _session_matches(session, origin):
        store.erase()
        session = None
    if (
        session is not None
        and session.access_token
        and session.access_token_expires_at > int(now()) + EXPIRY_SKEW_SECONDS
    ):
        return session.access_token
    if session is not None:
        try:
            refreshed = refresh_session(session, now)
        except OAuthHttpError as error:
            if error.oauth_error not in {"invalid_client", "invalid_grant"}:
                raise
            store.erase()
        else:
            store.save(refreshed)
            return refreshed.access_token
    if os.environ.get("GIT_TERMINAL_PROMPT") == "0":
        raise HelperError("Cloudflare Access login is required but prompting is disabled")
    logged_in = interactive_login(discover(origin), timeout)
    store.save(logged_in)
    return logged_in.access_token


def revoke(session: OAuthSession) -> None:
    try:
        request_json(
            session.revocation_endpoint,
            method="POST",
            form_body={
                "token": session.refresh_token,
                "token_type_hint": "refresh_token",
                "client_id": session.client_id,
            },
        )
    except HelperError:
        pass


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(add_help=False)
    parser.add_argument("--host", required=True)
    parser.add_argument("--timeout", type=int, default=300)
    parser.add_argument("operation", nargs="?")
    return parser


def main(argv: Optional[List[str]] = None) -> int:
    try:
        arguments = build_parser().parse_args(argv)
        origin = origin_for_host(arguments.host)
        if arguments.operation == "capability":
            print("version 0")
            print("capability authtype")
            return 0
        if arguments.operation == "logout":
            store = KeychainStore(origin)
            with host_lock(origin):
                session = store.load()
                if session is not None and _session_matches(session, origin):
                    revoke(session)
                store.erase()
            return 0
        attributes = read_credential(sys.stdin)
        if not credential_matches_host(attributes, arguments.host):
            return 0
        if arguments.operation == "get":
            if "authtype" not in attributes.get("capability[]", []):
                raise HelperError("Git does not support Bearer credential helpers")
            store = KeychainStore(origin)
            with host_lock(origin):
                token = access_token(origin, store, arguments.timeout)
            write_bearer(token, sys.stdout)
        elif arguments.operation == "erase":
            store = KeychainStore(origin)
            with host_lock(origin):
                store.erase()
        return 0
    except HelperError as error:
        print(f"git-credential-cloudflare-access: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
