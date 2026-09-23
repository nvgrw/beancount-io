import { exportJWK, generateKeyPair, SignJWT } from "jose";
import { createLocalJWKSet } from "jose";
import {
  cloudflareAccessUserId,
  verifyCloudflareAccessAssertion,
} from "../cloudflare-access";

const issuer = "https://team.cloudflareaccess.com";
const audience = "access-app-audience";

describe("Cloudflare Access assertions", () => {
  it("verifies issuer and audience and derives a stable local user id", async () => {
    const { privateKey, publicKey } = await generateKeyPair("RS256");
    const publicJwk = await exportJWK(publicKey);
    publicJwk.kid = "test-key";
    const token = await new SignJWT({ email: "Ada@Example.test" })
      .setProtectedHeader({ alg: "RS256", kid: publicJwk.kid })
      .setIssuer(issuer)
      .setAudience(audience)
      .setSubject("access-user-1")
      .setIssuedAt()
      .setExpirationTime("5m")
      .sign(privateKey);

    const claims = await verifyCloudflareAccessAssertion(
      token,
      { issuer, audience },
      createLocalJWKSet({ keys: [publicJwk] }),
    );

    expect(claims).toMatchObject({
      issuer,
      subject: "access-user-1",
      email: "ada@example.test",
    });
    expect(cloudflareAccessUserId(issuer, claims!.subject)).toBe(
      cloudflareAccessUserId(issuer, claims!.subject),
    );
  });

  it("rejects a token issued for another Access application", async () => {
    const { privateKey, publicKey } = await generateKeyPair("RS256");
    const publicJwk = await exportJWK(publicKey);
    const token = await new SignJWT({ email: "ada@example.test" })
      .setProtectedHeader({ alg: "RS256" })
      .setIssuer(issuer)
      .setAudience("another-application")
      .setSubject("access-user-1")
      .setExpirationTime("5m")
      .sign(privateKey);

    await expect(
      verifyCloudflareAccessAssertion(
        token,
        { issuer, audience },
        createLocalJWKSet({ keys: [publicJwk] }),
      ),
    ).resolves.toBeNull();
  });
});
