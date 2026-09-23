import type { Context, Next } from "koa";
import type { AppConfig } from "@/config/config";
import type { AppLayers } from "@/foundation/composition";
import {
  CLOUDFLARE_ACCESS_ASSERTION_HEADER,
  verifyCloudflareAccessAssertion,
  type CloudflareAccessClaims,
} from "@/features/auth/utils/cloudflare-access";

export interface CloudflareAccessState {
  cloudflareAccessClaims?: CloudflareAccessClaims;
}

export function createCloudflareAccessProvisioningMiddleware(
  layers: AppLayers,
  config: AppConfig,
  verify: typeof verifyCloudflareAccessAssertion =
    verifyCloudflareAccessAssertion,
) {
  return async (ctx: Context & { state: CloudflareAccessState }, next: Next) => {
    const accessConfig = config.cloudflareAccess;
    if (!accessConfig) return next();

    const header = ctx.headers[CLOUDFLARE_ACCESS_ASSERTION_HEADER];
    const assertion = Array.isArray(header) ? header[0] : header;
    if (!assertion) return next();

    const claims = await verify(assertion, accessConfig);
    if (!claims) return next();

    const forwardedFor = ctx.headers["x-forwarded-for"];
    const ip =
      (Array.isArray(forwardedFor) ? forwardedFor[0] : forwardedFor)
        ?.split(",")[0]
        .trim() || ctx.ip;
    await layers.services.auth.ensureCloudflareAccessUser({ ...claims, ip });
    ctx.state.cloudflareAccessClaims = claims;
    return next();
  };
}