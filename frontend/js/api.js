/* ═══════════════════════════════════════════════
   API Helpers
   ═══════════════════════════════════════════════ */
async function api(path, opts = {}) {
  try {
    const headers = authHeaders(opts.headers ?? {});
    const res = await fetch(API + path, { ...opts, headers });
    if (res.status === 401 || res.status === 403) {
      // Auth either lapsed or the user lacks permission. Drop the token and
      // surface the login modal so they can re-authenticate. Skip this for
      // the /auth/* endpoints themselves to avoid loops.
      if (!path.startsWith('/auth/')) {
        if (res.status === 401) clearAuth();
        if (auth.enforcementOn) showLogin(
          res.status === 401 ? 'Your session expired. Please sign in again.' : ''
        );
      }
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || (res.status === 401 ? 'Sign in required' : 'Forbidden'));
    }
    if (!res.ok) {
      const body = await res.json().catch(() => ({}));
      throw new Error(body.detail || `HTTP ${res.status}`);
    }
    if (res.status === 204) return null;
    return await res.json();
  } catch (e) {
    notify(e.message, 'error');
    throw e;
  }
}

function notify(msg, type = 'success') {
  const el = $('notification');
  el.textContent = msg;
  el.className = type;
  clearTimeout(el._timer);
  el._timer = setTimeout(() => { el.className = ''; el.style.display = 'none'; }, 4000);
}
