import { describe, expect, it, vi } from "vitest";
import { redirectToLogin } from "../redirect-to-login";

describe("redirectToLogin", () => {
  it("ends the Cloudflare Access session", () => {
    const replace = vi.fn();

    redirectToLogin({ replace });

    expect(replace).toHaveBeenCalledWith("/cdn-cgi/access/logout");
  });
});
