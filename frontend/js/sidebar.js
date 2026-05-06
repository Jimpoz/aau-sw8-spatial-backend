/* ═══════════════════════════════════════════════
   Sidebar: Selectors
   ═══════════════════════════════════════════════ */
$('organization-select').addEventListener('change', async function() {
  const id = this.value;
  state.selectedOrganizationId = id || null;
  state.selectedCampusId = null;
  state.buildings = [];
  state.floors = [];
  state.spaces = [];
  state.connections = [];

  const bSel = $('building-select');
  bSel.innerHTML = '<option value="">Select building...</option>';
  bSel.disabled = true;
  const fSel = $('floor-select');
  fSel.innerHTML = '<option value="">Select floor...</option>';
  fSel.disabled = true;
  $('add-building-btn').disabled = true;
  $('add-floor-btn').disabled = true;
  $('delete-building-btn').disabled = true;
  $('delete-floor-btn').disabled = true;
  $('delete-org-btn').disabled = !id;
  $('search-input').disabled = true;
  $('export-btn').disabled = true;
  $('chat-input').disabled = true;
  $('chat-btn').disabled = true;

  updateMapVisibility();
  await loadCampuses();
});

$('campus-select').addEventListener('change', async function() {
  const id = this.value;
  state.selectedCampusId = id || null;
  state.buildings = [];
  state.floors = [];
  state.spaces = [];
  state.connections = [];

  const bSel = $('building-select');
  bSel.innerHTML = '<option value="">Select building...</option>';
  bSel.disabled = true;
  const fSel = $('floor-select');
  fSel.innerHTML = '<option value="">Select floor...</option>';
  fSel.disabled = true;
  $('search-input').disabled = !id;
  $('export-btn').disabled = !id;
  $('add-building-btn').disabled = !id;
  $('add-floor-btn').disabled = true;
  $('delete-building-btn').disabled = true;
  $('delete-floor-btn').disabled = true;

  // Enable AI Assistant
  $('chat-input').disabled = !id;
  $('chat-btn').disabled = !id;

  updateMapVisibility();

  if (!id) return;
  try {
    const data = await api(`/campuses/${id}/export`);
    const campus = data.campus || data;
    const buildings = campus.buildings || [];
    state.buildings = buildings.map(b => ({ id: b.id, name: b.name }));
    bSel.disabled = false;
    for (const b of state.buildings) {
      const opt = document.createElement('option');
      opt.value = b.id;
      opt.textContent = b.name;
      bSel.appendChild(opt);
    }
    if (state.buildings.length === 1) {
      bSel.value = state.buildings[0].id;
      bSel.dispatchEvent(new Event('change'));
    }
  } catch (e) { /* handled */ }
});

$('building-select').addEventListener('change', async function() {
  const id = this.value;
  state.selectedBuildingId = id || null;
  state.floors = [];
  state.spaces = [];
  state.connections = [];

  const fSel = $('floor-select');
  fSel.innerHTML = '<option value="">Select floor...</option>';
  fSel.disabled = true;
  $('add-floor-btn').disabled = !id;
  $('delete-building-btn').disabled = !id;
  $('delete-floor-btn').disabled = true;
  updateMapVisibility();

  if (!id) return;
  try {
    const floors = await api(`/buildings/${id}/floors`);
    state.floors = floors;
    fSel.disabled = false;
    for (const f of floors) {
      const opt = document.createElement('option');
      opt.value = f.id;
      opt.textContent = f.display_name;
      opt.dataset.floorIndex = f.floor_index;
      fSel.appendChild(opt);
    }
    if (floors.length === 1) {
      fSel.value = floors[0].id;
      fSel.dispatchEvent(new Event('change'));
    }
  } catch (e) { /* handled */ }
});

$('floor-select').addEventListener('change', async function() {
  const id = this.value;
  state.selectedFloorId = id || null;
  state.spaces = [];
  state.connections = [];
  $('delete-floor-btn').disabled = !id;
  updateMapVisibility();

  if (!id) return;
  try {
    const display = await api(`/floors/${id}/display`);
    const spacesRaw = Array.isArray(display) ? display : (display.spaces || []);
    state.spaces = flattenSpaces(spacesRaw);
    try {
      const floor = await api(`/floors/${id}`);
      state.currentFloorIndex = floor?.floor_index ?? null;
      state.currentBuildingId = floor?.building_id ?? state.selectedBuildingId;
    } catch (e) {
      state.currentFloorIndex = null;
      state.currentBuildingId = state.selectedBuildingId;
    }
    fitToCanvas();
    updateMapVisibility();
    // Load connections for this floor
    await loadFloorConnections(id);
    render();
  } catch (e) { /* handled */ }
});

async function loadFloorConnections(floorId) {
  try {
    state.connections = await api(`/floors/${floorId}/connections`);
  } catch (e) {
    state.connections = [];
  }
}

function flattenSpaces(spaces, depth) {
  depth = depth || 0;
  const result = [];
  for (const s of spaces) {
    if (s.z_index == null) s.z_index = depth;
    if (s.subspaces && s.subspaces.length) {
      s.child_spaces = s.subspaces.map(sub => ({ id: sub.id, name: sub.display_name }));
    }
    result.push(s);
    if (s.subspaces && s.subspaces.length) {
      result.push(...flattenSpaces(s.subspaces, depth + 1));
    }
  }
  if (depth === 0) result.sort((a, b) => (a.z_index || 0) - (b.z_index || 0));
  return result;
}

function updateMapVisibility() {
  const hasFloor = !!state.selectedFloorId;
  const hasSpaces = state.spaces.length > 0;
  $('tools-panel').style.display = hasFloor ? 'block' : 'none';
  if (hasFloor && !hasSpaces) {
    emptyState.style.display = 'block';
    $('empty-title').textContent = 'Empty floor';
    $('empty-msg').innerHTML = 'Use the <strong>+ Space</strong> tool to add spaces to this floor.';
  } else if (!hasFloor) {
    emptyState.style.display = 'block';
    $('empty-title').textContent = 'No floor selected';
    $('empty-msg').innerHTML = 'Click <strong>+ New Map</strong> to create a map from scratch,<br>or import a map JSON file to get started.';
  } else {
    emptyState.style.display = 'none';
  }
  legend.className = hasSpaces ? 'visible' : '';
}

/* ═══════════════════════════════════════════════
   Search
   ═══════════════════════════════════════════════ */
let searchTimer = null;
$('search-input').addEventListener('input', function() {
  clearTimeout(searchTimer);
  const q = this.value.trim();
  if (q.length < 2) { $('search-results').innerHTML = ''; return; }
  searchTimer = setTimeout(async () => {
    if (!state.selectedCampusId) return;
    try {
      const results = await api(`/campuses/${state.selectedCampusId}/search?q=${encodeURIComponent(q)}`);
      displaySearchResults(results);
    } catch (e) { /* handled */ }
  }, 300);
});

function displaySearchResults(results) {
  const container = $('search-results');
  container.innerHTML = '';
  if (!results.length) {
    container.innerHTML = '<div style="padding:8px;color:#999;font-size:13px">No results found</div>';
    return;
  }
  for (const r of results.slice(0, 10)) {
    const div = document.createElement('div');
    div.className = 'search-result';
    div.innerHTML = `<div class="sr-name">${r.display_name}</div><div class="sr-type">${(r.space_type||'').replace(/_/g,' ').toLowerCase()} &middot; Floor ${r.floor_index ?? '?'}</div>`;
    div.addEventListener('click', () => onSearchResultClick(r));
    container.appendChild(div);
  }
}

function onSearchResultClick(space) {
  if (space.building_id && space.building_id !== state.selectedBuildingId) {
    $('building-select').value = space.building_id;
    $('building-select').dispatchEvent(new Event('change'));
    setTimeout(() => selectFloorByIndex(space.floor_index), 500);
  } else {
    selectFloorByIndex(space.floor_index);
  }
}

function selectFloorByIndex(floorIndex) {
  const fSel = $('floor-select');
  for (const opt of fSel.options) {
    if (opt.dataset.floorIndex === String(floorIndex)) {
      fSel.value = opt.value;
      fSel.dispatchEvent(new Event('change'));
      break;
    }
  }
}

function findSpaceById(id) {
  return state.spaces.find(s => s.id === id) || null;
}

/* ═══════════════════════════════════════════════
   Map Import
   ═══════════════════════════════════════════════ */
$('import-file').addEventListener('change', function() {
  $('import-btn').disabled = !this.files.length;
});

$('import-btn').addEventListener('click', async () => {
  const file = $('import-file').files[0];
  if (!file) return;
  try {
    const text = await file.text();
    const json = JSON.parse(text);
    const campusId = json.campus?.id;
    if (!campusId) { notify('Invalid map JSON: missing campus.id', 'error'); return; }

    $('import-btn').disabled = true;
    $('import-btn').textContent = 'Importing...';
    const result = await api(`/campuses/${campusId}/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: text,
    });
    notify(`Imported: ${result.spaces_imported} spaces, ${result.connections_imported} connections`);
    await loadOrganizations();
    const importedOrgId = result.organization_id || json.organization?.id || json.campus?.organization_id || '';
    if (importedOrgId) {
      $('organization-select').value = importedOrgId;
      state.selectedOrganizationId = importedOrgId;
    }
    await loadCampuses();
    $('campus-select').value = campusId;
    $('campus-select').dispatchEvent(new Event('change'));
  } catch (e) {
    /* notify already called */
  } finally {
    $('import-btn').disabled = false;
    $('import-btn').textContent = 'Import';
  }
});

/* ═══════════════════════════════════════════════
   Export
   ═══════════════════════════════════════════════ */
$('export-btn').addEventListener('click', async () => {
  if (!state.selectedCampusId) return;
  try {
    const data = await api(`/campuses/${state.selectedCampusId}/export`);
    const campusName = (data.campus || data).name || 'export';
    const blob = new Blob([JSON.stringify(data, null, 2)], { type: 'application/json' });
    const url = URL.createObjectURL(blob);
    const a = document.createElement('a');
    a.href = url;
    a.download = `${campusName.replace(/\s+/g, '_')}_export.json`;
    document.body.appendChild(a);
    a.click();
    document.body.removeChild(a);
    URL.revokeObjectURL(url);
    notify('Export downloaded');
  } catch (e) { /* handled */ }
});

/* ═══════════════════════════════════════════════
   Reload Floor
   ═══════════════════════════════════════════════ */
async function reloadCurrentFloor() {
  if (!state.selectedFloorId) return;
  try {
    const display = await api(`/floors/${state.selectedFloorId}/display`);
    const spacesRaw = Array.isArray(display) ? display : (display.spaces || []);
    state.spaces = flattenSpaces(spacesRaw);
    try {
      const floor = await api(`/floors/${state.selectedFloorId}`);
      state.currentFloorIndex = floor?.floor_index ?? null;
      state.currentBuildingId = floor?.building_id ?? state.selectedBuildingId;
    } catch (e) {
      state.currentFloorIndex = null;
      state.currentBuildingId = state.selectedBuildingId;
    }
    fitToCanvas();
    updateMapVisibility();
    await loadFloorConnections(state.selectedFloorId);
    render();
  } catch (e) { /* handled */ }
}
