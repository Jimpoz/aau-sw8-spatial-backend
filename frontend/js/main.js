/* ═══════════════════════════════════════════════
   Escape key handler
   ═══════════════════════════════════════════════ */
document.addEventListener('keydown', e => {
  const tag = (e.target && e.target.tagName) || '';
  const typing = tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT';

  if (e.key === 'Enter' && !typing &&
      state.mode === 'addSpace' && currentShape() === 'polygon' &&
      state.pendingPolygon.length > 0) {
    e.preventDefault();
    finishPendingPolygon();
    return;
  }

  if (e.key === 'Escape') {
    if (state.mode === 'moveSpace') {
      cancelMove();
    }
    if (state.mode === 'placeDuplicate') {
      exitDuplicatePlaceMode();
    }
    if (state.mode === 'addSpace' && state.pendingPolygon.length > 0) {
      cancelPendingPolygon();
    }
    hideContextMenu();
    $('info-overlay').style.display = 'none';
    $('building-modal-overlay').style.display = 'none';
    $('floor-modal-overlay').style.display = 'none';
  }
});

/* ═══════════════════════════════════════════════
   Initialization
   ═══════════════════════════════════════════════ */
async function loadOrganizations() {
  try {
    state.organizations = await api('/organizations');
  } catch (e) {
    state.organizations = [];
  }
  const sel = $('organization-select');
  sel.innerHTML = '<option value="">All organizations</option>';
  for (const o of state.organizations) {
    const opt = document.createElement('option');
    opt.value = o.id;
    const typeLabel = (o.entity_type || 'OTHER').replace(/_/g, ' ').toLowerCase();
    opt.textContent = `${o.name} (${typeLabel})`;
    sel.appendChild(opt);
  }
}

async function loadCampuses() {
  const orgId = state.selectedOrganizationId;
  const path = orgId
    ? `/campuses?organization_id=${encodeURIComponent(orgId)}`
    : '/campuses';
  try {
    state.campuses = await api(path);
  } catch (e) {
    state.campuses = [];
  }
  const sel = $('campus-select');
  sel.innerHTML = '<option value="">Select campus...</option>';
  for (const c of state.campuses) {
    const opt = document.createElement('option');
    opt.value = c.id;
    opt.textContent = c.name;
    sel.appendChild(opt);
  }
}

async function loadSpaceTypes() {
  try {
    const data = await api('/enums/space-types');
    SPACE_TYPES = data.space_types || [];
    CONN_SPACE_TYPES = data.connection_types || [];
  } catch (e) { /* keep empty defaults */ }
}

async function init() {
  resizeCanvas();
  window.addEventListener('resize', () => {
    resizeCanvas();
    if (state.bounds) fitToCanvas();
    render();
  });

  // The mapmaker is an authoring tool that runs against every tenant — there's
  // no per-user identity for it. nginx already injects X-Api-Key on the
  // proxied /api path; that combined with no Authorization header puts the
  // backend into "service mode" (current_is_service=true), which the RLS
  // policy admits unconditionally so authoring can read/write every org's
  // rows. We deliberately do NOT probe /auth/me or surface a login modal.
  wireAuthUI();
  clearAuth();
  auth.enforcementOn = false;
  hideLogin();

  await loadAfterAuth();
}

async function loadAfterAuth() {
  await loadSpaceTypes();
  await loadOrganizations();
  await loadCampuses();
  if (state.organizations.length === 1) {
    $('organization-select').value = state.organizations[0].id;
    state.selectedOrganizationId = state.organizations[0].id;
  }
  if (state.campuses.length === 1) {
    $('campus-select').value = state.campuses[0].id;
    $('campus-select').dispatchEvent(new Event('change'));
  }
}

init();
