import "reflect-metadata";
import { ConflictError } from "@/shared/errors";
import { AuthService } from "../auth-service";

const claims = {
  issuer: "https://team.cloudflareaccess.com",
  subject: "person-1",
  email: "Ada@Example.test",
  issuedAt: 1_790_000_000,
  expiresAt: 1_790_003_600,
};

function fixture(existingEmailOwner: object | null = null) {
  const created = {
    id: "created-user",
    email: "ada@example.test",
    isBlocked: false,
  };
  const user = {
    getById: jest.fn(async () => null),
    getByMail: jest.fn(async () => existingEmailOwner),
    create: jest.fn(async (_tx, input) => ({ ...created, ...input })),
  };
  const db = {
    transaction: jest.fn(async (callback) => callback({ transaction: true })),
  };
  const createUser = jest.fn(async () => undefined);
  const service = new AuthService(
    { user } as never,
    db as never,
    {} as never,
    {} as never,
    { getAdminClient: () => ({ admin: { createUser } }) } as never,
    {
      favaApi: {
        baseUrl: "http://ledger:8000",
        adminUser: "admin",
        adminPassword: "password",
      },
      dashboard: { url: "https://books.example.test" },
      gitea: {},
    } as never,
  );
  return { service, user, createUser };
}

describe("Cloudflare Access user provisioning", () => {
  it("creates a passwordless local user and matching Gitea account", async () => {
    const { service, user, createUser } = fixture();

    const result = await service.ensureCloudflareAccessUser({
      ...claims,
      ip: "192.0.2.10",
    });

    expect(result.id).toMatch(/^cfu_/);
    expect(user.create).toHaveBeenCalledWith(
      expect.anything(),
      expect.objectContaining({
        id: expect.stringMatching(/^cfu_/),
        email: "ada@example.test",
        ledger_username: expect.stringMatching(/^cf_/),
      }),
    );
    expect(user.create.mock.calls[0][1]).not.toHaveProperty("password");
    expect(createUser).toHaveBeenCalledWith(
      expect.objectContaining({
        email: "ada@example.test",
        username: expect.stringMatching(/^cf_/),
      }),
      expect.anything(),
    );
  });

  it("does not silently attach an existing email-only account", async () => {
    const { service } = fixture({ id: "legacy-user" });

    await expect(
      service.ensureCloudflareAccessUser({ ...claims, ip: "192.0.2.10" }),
    ).rejects.toBeInstanceOf(ConflictError);
  });
});
