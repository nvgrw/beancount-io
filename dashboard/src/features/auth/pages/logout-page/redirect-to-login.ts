export function redirectToLoginAfterLogout(
  location: Pick<Location, "replace"> = window.location,
): void {
  location.replace("/cdn-cgi/access/logout");
}
