import type { CloudflareAccessClaims } from "@/features/auth/utils/cloudflare-access";
import { resolveCloudflareAccessIdentity } from "../identity";

const accessConfig = {
  issuer: "https://team.cloudflareaccess.com",
  audience: "access-audience",
};

const claims: CloudflareAccessClaims = {
  issuer: accessConfig.issuer,
  subject: "person-1",
  email: "ada@example.test",
  issuedAt: 1_790_000_000,
  expiresAt: 1_790_003_600,
};

describe("Cloudflare Access request identity", () => {
  it("resolves a provisioned Access subject as an interactive session", async () => {
    const verify = jest.fn(async () => claims);
    const database = {
      db: {},
      models: {
        user: {
          getById: jest.fn(async () => ({ isBlocked: false })),
        },
      },
    } as never;

    const identity = await resolveCloudflareAccessIdentity(
      { headers: { "cf-access-jwt-assertion": "signed-assertion" } },
      database,
      { cloudflareAccess: accessConfig },
      verify,
    );

    expect(identity).toMatchObject({
      method: "session",
      assurance: { type: "interactive" },
      issuedAt: claims.issuedAt,
      expiresAt: claims.expiresAt,
    });
    expect(identity?.userId).toMatch(/^cfu_/);
  });

  it("fails closed when the assertion is absent", async () => {
    const verify = jest.fn(async () => claims);
    const database = { db: {}, models: { user: {} } } as never;

    await expect(
      resolveCloudflareAccessIdentity(
        { headers: { authorization: "Bearer legacy-token" } },
        database,
        { cloudflareAccess: accessConfig },
        verify,
      ),
    ).resolves.toBeUndefined();
    expect(verify).not.toHaveBeenCalled();
  });
});
