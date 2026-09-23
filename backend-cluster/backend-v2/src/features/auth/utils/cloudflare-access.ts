import { createHash } from "node:crypto";
import {
  createRemoteJWKSet,
  jwtVerify,
  type JWTVerifyGetKey,
} from "jose";
import type { CloudflareAccessConfig } from "@/config/config";

export const CLOUDFLARE_ACCESS_ASSERTION_HEADER =
  "cf-access-jwt-assertion" as const;

export interface CloudflareAccessClaims {
  issuer: string;
  subject: string;
  email: string;
  issuedAt?: number;
  expiresAt?: number;
}

const remoteKeySets = new Map<string, JWTVerifyGetKey>();

function remoteKeySet(issuer: string): JWTVerifyGetKey {
  const existing = remoteKeySets.get(issuer);
  if (existing) return existing;

  const keys = createRemoteJWKSet(
    new URL("/cdn-cgi/access/certs", `${issuer}/`),
  );
  remoteKeySets.set(issuer, keys);
  return keys;
}

/** Stable local identifier for one Cloudflare account subject. */
export function cloudflareAccessUserId(
  issuer: string,
  subject: string,
): string {
  const digest = createHash("sha256")
    .update(issuer)
    .update("\0")
    .update(subject)
    .digest("base64url")
    .slice(0, 32);
  return `cfu_${digest}`;
}

export async function verifyCloudflareAccessAssertion(
  assertion: string,
  config: CloudflareAccessConfig,
  getKey: JWTVerifyGetKey = remoteKeySet(config.issuer),
): Promise<CloudflareAccessClaims | null> {
  try {
    const { payload } = await jwtVerify(assertion, getKey, {
      issuer: config.issuer,
      audience: config.audience,
      algorithms: ["RS256"],
    });
    if (!payload.sub || typeof payload.email !== "string" || !payload.email) {
      return null;
    }

    return {
      issuer: config.issuer,
      subject: payload.sub,
      email: payload.email.toLowerCase(),
      issuedAt: payload.iat,
      expiresAt: payload.exp,
    };
  } catch {
    return null;
  }
}
