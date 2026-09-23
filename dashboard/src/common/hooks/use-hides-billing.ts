import { useSyncExternalStore } from "react";
import { isAppSession } from "@/common/lib/app-session";
import { useReactNativeContext } from "@/common/providers/react-native-bridge-provider/react-native-bridge-context";
import { config } from "@/config/config";

const noSubscription = () => () => {};

/**
 * Whether pricing, billing, and upgrade prompts stay hidden: in the app's web
 * view, on any page an app opened (ADR 001 D14), or in a Stripe-free
 * self-hosted build. The marker is read after hydration, so the server and
 * client agree.
 */
export function useHidesBilling(): boolean {
  const { isReactNative } = useReactNativeContext();
  const appSession = useSyncExternalStore(
    noSubscription,
    isAppSession,
    () => false,
  );
  return isReactNative || appSession || config.selfHostedUnlimited;
}
