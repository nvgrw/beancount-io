import importlib.util
import io
import pathlib
import sys
import unittest
from unittest import mock
from urllib.parse import parse_qs, urlsplit


HELPER_PATH = (
    pathlib.Path(__file__).parents[1] / "git-credential-cloudflare-access.py"
)
SPEC = importlib.util.spec_from_file_location("cloudflare_git_helper", HELPER_PATH)
assert SPEC is not None and SPEC.loader is not None
helper = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = helper
SPEC.loader.exec_module(helper)


class CredentialProtocolTests(unittest.TestCase):
    def test_reads_repeated_capabilities_and_values_containing_equals(self):
        attributes = helper.read_credential(
            io.StringIO(
                "protocol=https\nhost=git.example.com\n"
                "capability[]=authtype\ncapability[]=state\nstate[]=a=b\n\n"
            )
        )

        self.assertEqual(attributes["capability[]"], ["authtype", "state"])
        self.assertEqual(attributes["state[]"], ["a=b"])

    def test_writes_git_bearer_fields(self):
        output = io.StringIO()

        helper.write_bearer("opaque-token", output)

        self.assertEqual(
            output.getvalue(),
            "capability[]=authtype\nauthtype=Bearer\n"
            "credential=opaque-token\nephemeral=1\n\n",
        )

    def test_ignores_credentials_for_another_host(self):
        attributes = helper.read_credential(
            io.StringIO("protocol=https\nhost=example.com\n\n")
        )

        self.assertFalse(helper.credential_matches_host(attributes, "git.example.com"))


class DiscoveryTests(unittest.TestCase):
    def test_discovers_and_validates_cloudflare_contract(self):
        issuer = "https://team.cloudflareaccess.com"
        responses = [
            {
                "resource": "https://git.example.com",
                "authorization_servers": [issuer],
            },
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/cdn-cgi/access/oauth/authorization",
                "token_endpoint": f"{issuer}/cdn-cgi/access/oauth/token",
                "revocation_endpoint": f"{issuer}/cdn-cgi/access/oauth/revoke",
                "registration_endpoint": f"{issuer}/cdn-cgi/access/oauth/registration",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
            },
        ]

        with mock.patch.object(helper, "request_json", side_effect=responses) as request:
            metadata = helper.discover("https://git.example.com")

        self.assertEqual(metadata.resource, "https://git.example.com")
        self.assertEqual(
            metadata.registration_endpoint,
            f"{issuer}/cdn-cgi/access/oauth/registration",
        )
        self.assertEqual(
            request.call_args_list[0].args[0],
            "https://git.example.com/.well-known/cloudflare-access-protected-resource/",
        )

    def test_rejects_when_dynamic_registration_is_disabled(self):
        issuer = "https://team.cloudflareaccess.com"
        responses = [
            {
                "resource": "https://git.example.com",
                "authorization_servers": [issuer],
            },
            {
                "issuer": issuer,
                "authorization_endpoint": f"{issuer}/authorize",
                "token_endpoint": f"{issuer}/token",
                "revocation_endpoint": f"{issuer}/revoke",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
            },
        ]

        with mock.patch.object(helper, "request_json", side_effect=responses):
            with self.assertRaisesRegex(helper.HelperError, "is not enabled"):
                helper.discover("https://git.example.com")

    def test_rejects_cross_origin_oauth_endpoint(self):
        issuer = "https://team.cloudflareaccess.com"
        responses = [
            {
                "resource": "https://git.example.com",
                "authorization_servers": [issuer],
            },
            {
                "issuer": issuer,
                "authorization_endpoint": "https://attacker.example/authorize",
                "token_endpoint": f"{issuer}/token",
                "revocation_endpoint": f"{issuer}/revoke",
                "registration_endpoint": f"{issuer}/register",
                "response_types_supported": ["code"],
                "grant_types_supported": ["authorization_code", "refresh_token"],
                "token_endpoint_auth_methods_supported": ["none"],
                "code_challenge_methods_supported": ["S256"],
            },
        ]

        with mock.patch.object(helper, "request_json", side_effect=responses):
            with self.assertRaisesRegex(helper.HelperError, "outside its issuer"):
                helper.discover("https://git.example.com")

    def test_authorization_url_uses_pkce_and_resource(self):
        issuer = "https://team.cloudflareaccess.com"
        metadata = helper.OAuthMetadata(
            origin="https://git.example.com",
            resource="https://git.example.com",
            issuer=issuer,
            authorization_endpoint=f"{issuer}/authorize?injected=value",
            token_endpoint=f"{issuer}/token",
            revocation_endpoint=f"{issuer}/revoke",
            registration_endpoint=f"{issuer}/register",
        )

        url = helper.build_authorization_url(
            metadata, "client", "http://127.0.0.1:1234/callback", "state", "challenge"
        )
        query = parse_qs(urlsplit(url).query)

        self.assertEqual(query["resource"], ["https://git.example.com"])
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["code_challenge"], ["challenge"])
        self.assertNotIn("injected", query)

    def test_pkce_challenge_starts_with_alphanumeric_character(self):
        with mock.patch.object(
            helper, "_pkce_challenge", side_effect=["_invalid", "Avalid"]
        ):
            with mock.patch.object(
                helper.secrets, "token_urlsafe", side_effect=["first", "second"]
            ):
                verifier, challenge = helper._new_pkce_pair()

        self.assertEqual((verifier, challenge), ("second", "Avalid"))


class SessionTests(unittest.TestCase):
    def session(self, **changes):
        values = {
            "origin": "https://git.example.com",
            "resource": "https://git.example.com",
            "issuer": "https://team.cloudflareaccess.com",
            "token_endpoint": "https://team.cloudflareaccess.com/token",
            "revocation_endpoint": "https://team.cloudflareaccess.com/revoke",
            "client_id": "client",
            "access_token": "old-access",
            "access_token_expires_at": 100,
            "refresh_token": "old-refresh",
        }
        values.update(changes)
        return helper.OAuthSession(**values)

    def test_round_trips_session_without_losing_tokens(self):
        session = self.session()

        self.assertEqual(helper.OAuthSession.from_json(session.to_json()), session)

    def test_refresh_rotates_refresh_token_before_returning_access_token(self):
        session = self.session()
        response = {
            "access_token": "new-access",
            "token_type": "Bearer",
            "expires_in": 3600,
            "refresh_token": "new-refresh",
        }

        with mock.patch.object(helper, "request_json", return_value=response) as request:
            refreshed = helper.refresh_session(session, now=lambda: 1000)

        self.assertEqual(refreshed.access_token, "new-access")
        self.assertEqual(refreshed.refresh_token, "new-refresh")
        self.assertEqual(refreshed.access_token_expires_at, 4600)
        self.assertEqual(request.call_args.kwargs["form_body"]["resource"], session.resource)

    def test_access_token_uses_unexpired_keychain_session(self):
        session = self.session(access_token_expires_at=5000)
        store = mock.Mock()
        store.load.return_value = session

        token = helper.access_token(
            "https://git.example.com", store, timeout=1, now=lambda: 1000
        )

        self.assertEqual(token, "old-access")
        store.save.assert_not_called()


class MainTests(unittest.TestCase):
    def test_capability_announces_authtype(self):
        output = io.StringIO()

        with mock.patch("sys.stdout", output):
            result = helper.main(
                ["--host", "git.example.com", "capability"]
            )

        self.assertEqual(result, 0)
        self.assertEqual(output.getvalue(), "version 0\ncapability authtype\n")


if __name__ == "__main__":
    unittest.main()
