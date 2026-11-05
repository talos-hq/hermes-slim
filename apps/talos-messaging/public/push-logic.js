// Shared by the worker and app tests; notification destinations stay inside this PWA.
export function notificationURL(value, origin) {
  const fallback = new URL('/talos/', origin)
  try {
    const url = new URL(value || fallback.href, fallback)
    return url.origin === fallback.origin && url.pathname.startsWith('/talos/') ? url.href : fallback.href
  } catch {
    return fallback.href
  }
}
