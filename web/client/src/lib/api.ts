/**
 * Base-path aware API helpers.
 *
 * The server injects window.__BASE_PATH__ into index.html at serve time
 * from the BASE_PATH environment variable (e.g. "/tracker/" or "/btc/").
 * This lets the same build run at any sub-path without being recompiled.
 *
 * Local dev (BASE_PATH "/"): apiUrl("/api/foo") → "/api/foo"
 * Tailscale /tracker  (BASE_PATH "/tracker/"): apiUrl("/api/foo") → "/tracker/api/foo"
 */

// Strip trailing slash so we can cleanly concatenate with paths that start with /
const _base = ((window as any).__BASE_PATH__ || "/").replace(/\/$/, "");

/** Prepend the deployment base path to an absolute-rooted API path. */
export function apiUrl(path: string): string {
  return `${_base}${path}`;
}

/** Build a WebSocket URL that respects the deployment base path. */
export function wsUrl(path: string): string {
  const proto = window.location.protocol === "https:" ? "wss" : "ws";
  return `${proto}://${window.location.host}${_base}${path}`;
}
