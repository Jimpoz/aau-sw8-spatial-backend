function getToken() {
  return auth.token ?? localStorage.getItem(AUTH_TOKEN_KEY);
}

function setToken(token) {
  auth.token = token;
  if (token) localStorage.setItem(AUTH_TOKEN_KEY, token);
  else       localStorage.removeItem(AUTH_TOKEN_KEY);
}

function authHeaders(extra = {}) {
  const t = getToken();
  return t ? { ...extra, 'Authorization': `Bearer ${t}` } : extra;
}

function showLogin(message) {
  const overlay = document.getElementById('login-overlay');
  if (!overlay) return;
  overlay.style.display = 'flex';
  const err = document.getElementById('login-error');
  if (err) err.textContent = message ?? '';
  setTimeout(() => document.getElementById('login-email')?.focus(), 0);
}

function hideLogin() {
  const overlay = document.getElementById('login-overlay');
  if (overlay) overlay.style.display = 'none';
}

function renderUserPill() {
  const pill = document.getElementById('user-pill');
  if (!pill) return;
  if (!auth.user) {
    pill.style.display = 'none';
    return;
  }
  pill.style.display = 'block';
  document.getElementById('user-pill-email').textContent = auth.user.email;
  const orgLabel = auth.organizationId
    ? `${auth.organizationId}${auth.role ? ' · ' + auth.role : ''}`
    : 'No active organization';
  document.getElementById('user-pill-org').textContent = orgLabel;
}

function applyPrincipal(principal) {
  auth.user = principal.user ?? { id: principal.id, email: principal.email, full_name: principal.full_name };
  auth.organizationId = principal.organization_id ?? null;
  auth.role = principal.role ?? null;
  renderUserPill();
}

function clearAuth() {
  setToken(null);
  auth.user = null;
  auth.organizationId = null;
  auth.role = null;
  renderUserPill();
}

/** Probe /auth/me. Returns 'authed' | 'unauthed' | 'shadow' | 'error'. */
async function probeAuthState() {
  try {
    const res = await fetch(API + '/auth/me', { headers: authHeaders() });
    if (res.status === 404) return 'shadow';      // auth router not mounted
    if (res.status === 401) return 'unauthed';
    if (res.ok) {
      const me = await res.json();
      applyPrincipal(me);
      return 'authed';
    }
    return 'error';
  } catch (_) {
    return 'error';
  }
}

async function performLogin(email, password, organizationId) {
  const body = { email, password };
  if (organizationId) body.organization_id = organizationId;
  const res = await fetch(API + '/auth/login', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  });
  if (!res.ok) {
    const detail = await res.json().catch(() => ({}));
    throw new Error(detail.detail || `Login failed (${res.status})`);
  }
  const data = await res.json();
  setToken(data.token);
  applyPrincipal(data);
  return data;
}

function logout() {
  clearAuth();
  if (auth.enforcementOn) showLogin();
}

function wireAuthUI() {
  document.getElementById('login-submit')?.addEventListener('click', async () => {
    const email = document.getElementById('login-email').value.trim();
    const password = document.getElementById('login-password').value;
    const orgId = document.getElementById('login-org').value.trim() || null;
    if (!email || !password) {
      document.getElementById('login-error').textContent = 'Email and password are required.';
      return;
    }
    document.getElementById('login-error').textContent = '';
    try {
      await performLogin(email, password, orgId);
      hideLogin();
      // Run the rest of init now that we have a token.
      await loadAfterAuth();
    } catch (e) {
      document.getElementById('login-error').textContent = e.message;
    }
  });
  document.getElementById('login-password')?.addEventListener('keydown', (e) => {
    if (e.key === 'Enter') document.getElementById('login-submit').click();
  });
  document.getElementById('logout-btn')?.addEventListener('click', logout);
}
