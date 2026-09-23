import { BadUserInputError, ResourceLimitReachedError } from "@/shared/errors";
import { assertSafeRepoPath } from "@/shared/safe-repo-path";

export const LEDGER_CONFIG_PATH = ".beancountio.json";
export const DEFAULT_ENTRY_POINT = "main.bean";
export const MAX_LEDGER_CONFIG_BYTES = 16 * 1024;

export interface BeancountIoConfig {
  entrypoint?: string;
}

/** Parse the repository-root config that selects the Beancount parse root. */
export function parseBeancountIoConfig(source: string): BeancountIoConfig {
  const bytes = Buffer.byteLength(source, "utf-8");
  if (bytes > MAX_LEDGER_CONFIG_BYTES) {
    throw new ResourceLimitReachedError(
      "Ledger configuration bytes",
      MAX_LEDGER_CONFIG_BYTES,
      bytes,
    );
  }

  let value: unknown;
  try {
    value = JSON.parse(source);
  } catch {
    throw new BadUserInputError(
      `${LEDGER_CONFIG_PATH} must contain valid JSON`,
      LEDGER_CONFIG_PATH,
    );
  }
  if (typeof value !== "object" || value === null || Array.isArray(value)) {
    throw new BadUserInputError(
      `${LEDGER_CONFIG_PATH} must contain a JSON object`,
      LEDGER_CONFIG_PATH,
    );
  }

  const entrypoint = (value as Record<string, unknown>).entrypoint;
  if (entrypoint === undefined) return {};
  if (
    typeof entrypoint !== "string" ||
    entrypoint.length === 0 ||
    entrypoint.includes("\n") ||
    entrypoint.includes("\r")
  ) {
    throw new BadUserInputError(
      `${LEDGER_CONFIG_PATH}.entrypoint must be a non-empty string`,
      "entrypoint",
    );
  }
  assertSafeRepoPath(entrypoint, "entrypoint");
  if (!/\.(?:bean|beancount)$/u.test(entrypoint)) {
    throw new BadUserInputError(
      `${LEDGER_CONFIG_PATH}.entrypoint must name a .bean or .beancount file`,
      "entrypoint",
    );
  }

  return { entrypoint };
}

export function resolveLedgerEntryPoint(
  configuredEntryPoint?: string,
  explicitEntryPoint?: string,
): string {
  return explicitEntryPoint ?? configuredEntryPoint ?? DEFAULT_ENTRY_POINT;
}
