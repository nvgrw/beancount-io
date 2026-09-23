import { describe, expect, it, vi } from "vitest";
import { redirectToLoginAfterLogout } from "../redirect-to-login";

describe("redirectToLoginAfterLogout", () => {
  it("ends the Cloudflare Access session", () => {
    const replace = vi.fn();

    redirectToLoginAfterLogout({ replace });

    expect(replace).toHaveBeenCalledWith("/cdn-cgi/access/logout");
  });
});
