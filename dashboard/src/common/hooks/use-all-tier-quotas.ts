import { useQuery } from "@apollo/client/react";
import {
  GetAllTierQuotasDocument,
  type GetAllTierQuotasQuery,
} from "@/graphql/definitions";
import { config } from "@/config/config";

export type TierQuota = {
  tier: string;
  aiCfoTokensMax: number;
  maxLedgers: number;
  maxCollaboratorsPerLedger: number;
  // -1 = unlimited (paid tiers); a finite value caps directives (FREE = 1000)
  maxDirectives: number;
};

export function useAllTierQuotas() {
  const { data, loading, error } = useQuery<GetAllTierQuotasQuery>(
    GetAllTierQuotasDocument,
    {
      fetchPolicy: "cache-first",
      skip: config.selfHostedUnlimited,
    },
  );

  const quotas = config.selfHostedUnlimited
    ? null
    : (data?.allTierQuotas ?? null);

  function getQuotaForTier(userTier: string): TierQuota | null {
    return quotas?.find((q) => q.tier === userTier) ?? null;
  }

  return {
    quotas,
    getQuotaForTier,
    isLoading: config.selfHostedUnlimited ? false : loading,
    error: config.selfHostedUnlimited ? undefined : error,
  };
}
