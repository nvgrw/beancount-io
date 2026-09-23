import { createFileRoute, redirect } from "@tanstack/react-router";
import ResetPasswordPage from "@/features/auth/pages/reset-password-page";
import { getSEOMetadata, createHeadMeta } from "@/common/lib/seo/seo-helpers";

export const Route = createFileRoute("/auth/reset-password")({
  component: ResetPasswordPage,
  beforeLoad: ({ context }) => {
    if (context.userProfile) throw redirect({ to: "/auth/welcome" });
  },
  head: ({ match }) =>
    createHeadMeta(
      match.context.localization.i18n,
      getSEOMetadata(
        match.context.localization.i18n,
        "seo.resetPassword.title",
        "seo.resetPassword.description",
      ),
      { noIndex: true },
    ),
});
