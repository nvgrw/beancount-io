/**
 * Load the login page as a fresh document once the session has ended (logout,
 * or a password reset that revoked it), so no state loaded under the old
 * session survives, and replace the current entry so Back does not return to it.
 */
export function redirectToLogin(
  location: Pick<Location, "replace"> = window.location,
): void {
  location.replace("/cdn-cgi/access/logout");
}
