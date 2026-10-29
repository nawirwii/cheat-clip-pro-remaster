/**
 * Robust file download helper — works in browser, Vite dev (proxy),
 * production (backend serves frontend), and Tauri desktop WebView.
 *
 * Why this exists:
 * Plain `<a href="/api/..." download>` does nothing inside Tauri/WebView
 * and breaks when the WebView origin is `tauri://localhost` (relative
 * `/api/...` resolves to a non-existent tauri:// URL).
 * Fetching as blob + temp anchor click works everywhere the backend
 * is reachable over HTTP.
 */

function resolveDownloadUrl(url: string): string {
  // Already absolute (http/https/blob) — use as-is.
  if (/^(https?:|blob:|tauri:)/i.test(url)) {
    // If page itself runs on tauri:// but URL is absolute http — fine.
    return url;
  }
  // Relative /api/... path
  if (typeof window !== 'undefined') {
    const origin = window.location.origin || '';
    // http(s) origin (dev vite / production backend-served) → same-origin works.
    if (/^https?:\/\//i.test(origin) && !/^tauri/i.test(origin)) {
      return url.startsWith('/') ? `${origin}${url}` : `${origin}/${url}`;
    }
  }
  // Tauri (tauri://localhost) or unknown origin → backend lives on localhost:8000.
  const base = 'http://127.0.0.1:8000';
  return url.startsWith('/') ? `${base}${url}` : `${base}/${url}`;
}

function filenameFromUrl(url: string, fallback: string): string {
  try {
    const clean = url.split('?')[0].split('#')[0];
    const last = clean.split('/').filter(Boolean).pop();
    if (last && last.includes('.')) return decodeURIComponent(last);
  } catch {
    /* ignore */
  }
  return fallback;
}

/**
 * Download a backend file URL as a blob and trigger a save.
 * @param url backend URL (relative `/api/...` or absolute)
 * @param filename suggested filename shown in save dialog
 */
export async function downloadFileFromUrl(url: string, filename?: string): Promise<void> {
  const candidates: string[] = [];
  const primary = resolveDownloadUrl(url);
  candidates.push(primary);
  // Fallback: if same-origin fetch fails (e.g. vite proxy down),
  // retry directly against the backend port.
  if (!primary.startsWith('http://127.0.0.1:8000')) {
    const path = url.startsWith('/') ? url : `/${url}`;
    candidates.push(`http://127.0.0.1:8000${path}`);
  }

  let lastError: unknown = null;
  for (const candidate of candidates) {
    try {
      const res = await fetch(candidate);
      if (!res.ok) {
        lastError = new Error(`HTTP ${res.status} — ${candidate}`);
        continue;
      }
      const blob = await res.blob();
      const objectUrl = URL.createObjectURL(blob);
      try {
        const name = filename || filenameFromUrl(candidate, 'download.mp4');
        const a = document.createElement('a');
        a.href = objectUrl;
        a.download = name;
        // Must be in DOM for Firefox / WebView.
        a.style.display = 'none';
        document.body.appendChild(a);
        a.click();
        a.remove();
      } finally {
        // Revoke after a tick so the download can start.
        setTimeout(() => URL.revokeObjectURL(objectUrl), 4000);
      }
      return;
    } catch (e) {
      lastError = e;
    }
  }
  throw lastError instanceof Error ? lastError : new Error('Download failed');
}
