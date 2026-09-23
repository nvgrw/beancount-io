import { createCloudflareAccessProvisioningMiddleware } from "../cloudflare-access-provisioning-middleware";

const claims = {
  issuer: "https://team.cloudflareaccess.com",
  subject: "person-1",
  email: "ada@example.test",
};

describe("Cloudflare Access provisioning middleware", () => {
  it("provisions once through the auth service and publishes verified claims", async () => {
    const ensureCloudflareAccessUser = jest.fn(async () => ({ id: "cfu_user" }));
    const verify = jest.fn(async () => claims);
    const middleware = createCloudflareAccessProvisioningMiddleware(
      { services: { auth: { ensureCloudflareAccessUser } } } as never,
      {
        cloudflareAccess: {
          issuer: claims.issuer,
          audience: "access-audience",
        },
      } as never,
      verify as never,
    );
    const next = jest.fn(async () => undefined);
    const ctx = {
      headers: {
        "cf-access-jwt-assertion": "signed-assertion",
        "x-forwarded-for": "192.0.2.10, 198.51.100.4",
      },
      ip: "127.0.0.1",
      state: {},
    } as never;

    await middleware(ctx, next);

    expect(ensureCloudflareAccessUser).toHaveBeenCalledWith({
      ...claims,
      ip: "192.0.2.10",
    });
    expect((ctx as { state: object }).state).toEqual({
      cloudflareAccessClaims: claims,
    });
    expect(next).toHaveBeenCalledTimes(1);
  });

  it("does not provision an invalid assertion", async () => {
    const ensureCloudflareAccessUser = jest.fn();
    const middleware = createCloudflareAccessProvisioningMiddleware(
      { services: { auth: { ensureCloudflareAccessUser } } } as never,
      {
        cloudflareAccess: {
          issuer: claims.issuer,
          audience: "access-audience",
        },
      } as never,
      jest.fn(async () => null) as never,
    );
    const next = jest.fn(async () => undefined);

    await middleware(
      {
        headers: { "cf-access-jwt-assertion": "invalid" },
        state: {},
      } as never,
      next,
    );

    expect(ensureCloudflareAccessUser).not.toHaveBeenCalled();
    expect(next).toHaveBeenCalledTimes(1);
  });
});