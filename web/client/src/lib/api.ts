/**
 * Base-path aware API helpers.
 *
 * Works automatically for two access methods without any build-time config:
 *
 *   Direct port-forward  http://localhost:8080/
 *     pathname = "/"  →  _base = ""  →  apiUrl("/api/foo") = "/api/foo"
 *
 *   Tailscale sub-path   https://host/tracker/
 *     pathname = "/tracker/"  →  _base = "/tracker"
 *     →  apiUrl("/api/foo") = "/tracker/api/foo"
 *     →  Tailscale strips /tracker before forwarding to the container
 *
 * Since this SPA has no client-side routing, window.location.pathname always
 * equals the mount point, making pathname-based detection reliable.
 *
 * window.__BASE_PATH__ (injected by the server when BASE_PATH env var is set
 * to a non-root value) takes priority and can override auto-detection.
 */
const _base = (() => {
  const injected = (window as any).__BASE_PATH__ as string | undefined;
  if (injected && injected !== "/") {
    const base = injected.replace(/\/+$/, ""); // e.g. "/tracker"
    const p = window.location.pathname;
    // Only use the injected base when the current URL is actually served under
    // that path (Tailscale sub-path access).  When port-forwarding directly
    // (localhost:PORT → "/"), the injected value is irrelevant and we fall
    // through to auto-detection so no stale prefix leaks into API/WS URLs.
    if (p === base || p.startsWith(base + "/")) {
      return base;
    }
  }
  // Auto-detect from the current URL (no client-side routing, so pathname = mount point)
  const p = window.location.pathname;
  return p.endsWith("/") ? p.slice(0, -1) : p.replace(/\/[^/]*$/, "");
})();

/** Prepend the deployment base path to an absolute-rooted API path. */
export function apiUrl(path: string): string {
  return `${_base}${path}`;
}

/** Build a WebSocket URL that respects the deployment base path. */
export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${_base}${path}`;
}
