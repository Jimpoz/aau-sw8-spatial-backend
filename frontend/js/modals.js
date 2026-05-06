/* ═══════════════════════════════════════════════
   New Map Modal
   ═══════════════════════════════════════════════ */
function uuid() { return crypto.randomUUID(); }

function refreshNewMapOrgDropdown() {
  const sel = $('new-org-select');
  sel.innerHTML = '';
  for (const o of state.organizations) {
    const opt = document.createElement('option');
    opt.value = o.id;
    opt.textContent = o.name;
    sel.appendChild(opt);
  }
  const newOpt = document.createElement('option');
  newOpt.value = '__new__';
  newOpt.textContent = '+ Create new organization…';
  sel.appendChild(newOpt);

  if (state.selectedOrganizationId) {
    sel.value = state.selectedOrganizationId;
  } else {
    sel.value = '__new__';
  }
  updateNewOrgFieldsVisibility();
}

function updateNewOrgFieldsVisibility() {
  const creating = $('new-org-select').value === '__new__';
  $('new-org-fields').style.display = creating ? 'block' : 'none';
}

$('new-org-select').addEventListener('change', updateNewOrgFieldsVisibility);

$('new-map-btn').addEventListener('click', () => {
  $('modal-overlay').style.display = 'flex';
  $('new-org-name').value = '';
  $('new-org-entity-type').value = 'UNIVERSITY';
  $('new-campus-name').value = '';
  $('new-building-name').value = '';
  $('new-floor-name').value = 'Ground Floor';
  $('new-floor-index').value = '0';
  refreshNewMapOrgDropdown();
  setTimeout(() => {
    const focusTarget = $('new-org-select').value === '__new__'
      ? $('new-org-name')
      : $('new-campus-name');
    focusTarget.focus();
  }, 100);
});

$('modal-cancel').addEventListener('click', () => {
  $('modal-overlay').style.display = 'none';
});

$('modal-overlay').addEventListener('click', e => {
  if (e.target === $('modal-overlay')) $('modal-overlay').style.display = 'none';
});

$('modal-create').addEventListener('click', async () => {
  const campusName = $('new-campus-name').value.trim();
  const buildingName = $('new-building-name').value.trim();
  const floorName = $('new-floor-name').value.trim();
  const floorIndex = parseInt($('new-floor-index').value) || 0;

  const orgChoice = $('new-org-select').value;
  const creatingNewOrg = orgChoice === '__new__';
  const orgName = $('new-org-name').value.trim();
  const entityType = $('new-org-entity-type').value;

  if (creatingNewOrg && !orgName) {
    notify('Organization name is required', 'error');
    return;
  }
  if (!campusName) { notify('Campus name is required', 'error'); return; }
  if (!buildingName) { notify('Building name is required', 'error'); return; }
  if (!floorName) { notify('Floor name is required', 'error'); return; }

  const organizationId = creatingNewOrg ? uuid() : orgChoice;
  const campusId = uuid();
  const buildingId = uuid();
  const floorId = uuid();

  const organization = creatingNewOrg
    ? {
        id: organizationId,
        name: orgName,
        entity_type: entityType,
      }
    : (state.organizations.find(o => o.id === organizationId) || null);

  const importData = {
    schema_version: '1.0',
    organization: organization,
    campus: {
      id: campusId,
      name: campusName,
      organization_id: organizationId,
      buildings: [{
        id: buildingId,
        name: buildingName,
        organization_id: organizationId,
        floors: [{
          id: floorId,
          floor_index: floorIndex,
          display_name: floorName,
          spaces: []
        }]
      }],
      outdoor_spaces: [],
      connections: []
    }
  };

  try {
    $('modal-create').disabled = true;
    $('modal-create').textContent = 'Creating...';
    await api(`/campuses/${campusId}/import`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(importData),
    });
    notify(`Map "${campusName}" created`);
    $('modal-overlay').style.display = 'none';

    await loadOrganizations();
    $('organization-select').value = organizationId;
    state.selectedOrganizationId = organizationId;
    await loadCampuses();
    $('campus-select').value = campusId;
    $('campus-select').dispatchEvent(new Event('change'));
  } catch (e) { /* handled */ }
  finally {
    $('modal-create').disabled = false;
    $('modal-create').textContent = 'Create Map';
  }
});

/* ═══════════════════════════════════════════════
   Add Building Modal
   ═══════════════════════════════════════════════ */
$('add-building-btn').addEventListener('click', () => {
  $('building-modal-overlay').style.display = 'flex';
  $('modal-building-name').value = '';
  setTimeout(() => $('modal-building-name').focus(), 100);
});

$('building-modal-cancel').addEventListener('click', () => {
  $('building-modal-overlay').style.display = 'none';
});

$('building-modal-overlay').addEventListener('click', e => {
  if (e.target === $('building-modal-overlay')) $('building-modal-overlay').style.display = 'none';
});

$('building-modal-create').addEventListener('click', async () => {
  const name = $('modal-building-name').value.trim();
  if (!name) { notify('Building name is required', 'error'); return; }
  if (!state.selectedCampusId) { notify('Select a campus first', 'error'); return; }

  const buildingId = uuid();
  try {
    $('building-modal-create').disabled = true;
    await api('/buildings', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        id: buildingId,
        campus_id: state.selectedCampusId,
        organization_id: state.selectedOrganizationId || null,
        name,
      }),
    });
    notify(`Building "${name}" created`);
    $('building-modal-overlay').style.display = 'none';

    // Refresh building list by re-dispatching campus change
    $('campus-select').dispatchEvent(new Event('change'));
    // Auto-select new building after refresh
    setTimeout(() => {
      $('building-select').value = buildingId;
      $('building-select').dispatchEvent(new Event('change'));
    }, 500);
  } catch (e) { /* handled */ }
  finally {
    $('building-modal-create').disabled = false;
  }
});

/* ═══════════════════════════════════════════════
   Add Floor Modal
   ═══════════════════════════════════════════════ */
$('add-floor-btn').addEventListener('click', () => {
  $('floor-modal-overlay').style.display = 'flex';
  $('modal-floor-name').value = '';
  $('modal-floor-index').value = '0';
  setTimeout(() => $('modal-floor-name').focus(), 100);
});

$('floor-modal-cancel').addEventListener('click', () => {
  $('floor-modal-overlay').style.display = 'none';
});

$('floor-modal-overlay').addEventListener('click', e => {
  if (e.target === $('floor-modal-overlay')) $('floor-modal-overlay').style.display = 'none';
});

$('floor-modal-create').addEventListener('click', async () => {
  const name = $('modal-floor-name').value.trim();
  const floorIndex = parseInt($('modal-floor-index').value) || 0;
  if (!name) { notify('Floor name is required', 'error'); return; }
  if (!state.selectedBuildingId) { notify('Select a building first', 'error'); return; }

  const floorId = uuid();
  try {
    $('floor-modal-create').disabled = true;
    await api('/floors', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ id: floorId, building_id: state.selectedBuildingId, floor_index: floorIndex, display_name: name }),
    });
    notify(`Floor "${name}" created`);
    $('floor-modal-overlay').style.display = 'none';

    // Refresh floor list by re-dispatching building change
    $('building-select').dispatchEvent(new Event('change'));
    // Auto-select new floor after refresh
    setTimeout(() => {
      $('floor-select').value = floorId;
      $('floor-select').dispatchEvent(new Event('change'));
    }, 500);
  } catch (e) { /* handled */ }
  finally {
    $('floor-modal-create').disabled = false;
  }
});

/* ═══════════════════════════════════════════════
   Delete Building
   ═══════════════════════════════════════════════ */
$('delete-building-btn').addEventListener('click', async () => {
  const buildingId = state.selectedBuildingId;
  if (!buildingId) return;
  const selected = state.buildings.find(b => b.id === buildingId);
  const name = selected ? selected.name : buildingId;
  if (!window.confirm(
    `Delete building "${name}" and every floor, space, and connection inside it? This cannot be undone.`
  )) return;
  try {
    $('delete-building-btn').disabled = true;
    await api(`/buildings/${buildingId}`, { method: 'DELETE' });
    notify(`Deleted building "${name}"`);
    $('campus-select').dispatchEvent(new Event('change'));
  } catch (e) { /* handled */ }
  finally {
    $('delete-building-btn').disabled = !state.selectedBuildingId;
  }
});

/* ═══════════════════════════════════════════════
   Delete Floor
   ═══════════════════════════════════════════════ */
$('delete-floor-btn').addEventListener('click', async () => {
  const floorId = state.selectedFloorId;
  if (!floorId) return;
  const selected = state.floors.find(f => f.id === floorId);
  const name = selected ? (selected.display_name || floorId) : floorId;
  if (!window.confirm(
    `Delete floor "${name}" and every space and connection on it? This cannot be undone.`
  )) return;
  try {
    $('delete-floor-btn').disabled = true;
    await api(`/floors/${floorId}`, { method: 'DELETE' });
    notify(`Deleted floor "${name}"`);
    $('building-select').dispatchEvent(new Event('change'));
  } catch (e) { /* handled */ }
  finally {
    $('delete-floor-btn').disabled = !state.selectedFloorId;
  }
});

/* ═══════════════════════════════════════════════
   Delete Organization
   ═══════════════════════════════════════════════ */
$('delete-org-btn').addEventListener('click', async () => {
  const orgId = state.selectedOrganizationId;
  if (!orgId) return;
  const selected = (state.organizations || []).find(o => o.id === orgId);
  const name = selected ? selected.name : orgId;
  if (!window.confirm(
    `Delete organization "${name}" and every campus, building, floor, space, and connection it owns? This cannot be undone.`
  )) return;
  const typed = window.prompt(
    `Type the organization name exactly to confirm deletion:\n\n${name}`
  );
  if (typed !== name) {
    notify('Deletion cancelled — name did not match.', 'error');
    return;
  }
  try {
    $('delete-org-btn').disabled = true;
    await api(`/organizations/${orgId}`, { method: 'DELETE' });
    notify(`Deleted organization "${name}"`);
    state.selectedOrganizationId = null;
    await loadOrganizations();
    $('organization-select').value = '';
    $('organization-select').dispatchEvent(new Event('change'));
  } catch (e) { /* handled */ }
  finally {
    $('delete-org-btn').disabled = !state.selectedOrganizationId;
  }
});
