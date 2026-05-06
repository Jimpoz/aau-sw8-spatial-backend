/* ═══════════════════════════════════════════════
   Context Menu
   ═══════════════════════════════════════════════ */
function showContextMenu(cx, cy, containerRect) {
  const menu = $('context-menu');
  menu.style.display = 'block';
  const mw = menu.offsetWidth || 150;
  const mh = menu.offsetHeight || 140;
  let left = cx, top = cy;
  if (left + mw > containerRect.width) left = containerRect.width - mw - 4;
  if (top + mh > containerRect.height) top = containerRect.height - mh - 4;
  if (left < 0) left = 4;
  if (top < 0) top = 4;
  menu.style.left = left + 'px';
  menu.style.top = top + 'px';
}

function hideContextMenu() {
  $('context-menu').style.display = 'none';
  state.contextMenu = null;
}

$('ctx-info').addEventListener('click', () => {
  if (!state.contextMenu) return;
  showInfoModal(state.contextMenu.space);
  hideContextMenu();
});
$('ctx-move').addEventListener('click', () => {
  if (!state.contextMenu) return;
  startMoveMode(state.contextMenu.space);
  hideContextMenu();
});
$('ctx-duplicate').addEventListener('click', () => {
  if (!state.contextMenu) return;
  duplicateSpace(state.contextMenu.space);
  hideContextMenu();
});
$('ctx-delete').addEventListener('click', () => {
  if (!state.contextMenu) return;
  deleteSpace(state.contextMenu.space);
  hideContextMenu();
});

/* ═══════════════════════════════════════════════
   Info Modal (Editable)
   ═══════════════════════════════════════════════ */
/* ── Info Modal Tabs ── */
document.querySelectorAll('.info-tab').forEach(tab => {
  tab.addEventListener('click', () => {
    document.querySelectorAll('.info-tab').forEach(t => t.classList.remove('active'));
    document.querySelectorAll('.info-tab-content').forEach(c => c.classList.remove('active'));
    tab.classList.add('active');
    $('tab-' + tab.dataset.tab).classList.add('active');
  });
});

function renderRoomSummary(data) {
  const el = $('info-room-summary');
  const counts = data.room_object_counts || data.room_object_counts_json;
  const textCounts = data.room_text_counts || data.room_text_counts_json;
  const hasObjects = counts && Object.keys(counts).length;
  const hasText = textCounts && Object.keys(textCounts).length;
  if (!hasObjects && !hasText) {
    el.innerHTML = '<span style="color:#aaa">No room analysis yet</span>';
    return;
  }
  const renderTags = (entries) =>
    '<div class="room-objects-list">' +
    entries.map(([k, v]) =>
      `<span class="room-obj-tag">${k.replace(/_/g, ' ')} (${v})</span>`
    ).join('') + '</div>';

  let html = '';
  if (hasObjects) {
    html += renderTags(Object.entries(counts));
  }
  if (hasText) {
    html += renderTags(Object.entries(textCounts));
  }
  el.innerHTML = html;
}

function renderImageViewer(space) {
  const container = $('image-viewer-content');
  const images = space.room_images
    || space.metadata?.room_summary?.room_images;
  const views = space.stored_views || space.metadata?.room_summary?.stored_views;
  const updatedAt = space.room_summary_updated_at || space.metadata?.room_summary?.updated_at;

  if (!images || !images.length) {
    container.innerHTML = '<div style="color:#aaa;font-size:13px;padding:8px 0">No images stored for this space. Upload images below and click "Analyze Room" to store them.</div>';
    return;
  }

  // Map views to direction labels
  const directions = views || [];
  const dirLabels = ['North', 'East', 'South', 'West'];

  let html = '<div class="image-viewer-grid">';
  for (let i = 0; i < images.length; i++) {
    const label = directions[i] ? directions[i].charAt(0).toUpperCase() + directions[i].slice(1) : (dirLabels[i] || `View ${i + 1}`);
    const svgContent = images[i];
    html += `
      <div class="image-viewer-card">
        <div class="iv-label">${label}</div>
        <div class="iv-body">${svgContent}</div>
      </div>`;
  }
  html += '</div>';

  // Metadata section
  html += '<div class="image-meta">';
  html += `<div class="meta-row"><span class="meta-label">Images stored</span><span class="meta-value">${images.length}</span></div>`;
  if (directions.length) {
    html += `<div class="meta-row"><span class="meta-label">Views</span><span class="meta-value">${directions.join(', ')}</span></div>`;
  }
  if (updatedAt) {
    const d = new Date(updatedAt);
    const formatted = isNaN(d.getTime()) ? updatedAt : d.toLocaleString();
    html += `<div class="meta-row"><span class="meta-label">Last updated</span><span class="meta-value">${formatted}</span></div>`;
  }
  html += '</div>';

  container.innerHTML = html;
}

async function showInfoModal(space) {
  state._editingSpace = space;
  $('info-title').textContent = space.display_name || 'Space Info';

  // Reset to Details tab
  document.querySelectorAll('.info-tab').forEach(t => t.classList.remove('active'));
  document.querySelectorAll('.info-tab-content').forEach(c => c.classList.remove('active'));
  document.querySelector('.info-tab[data-tab="details"]').classList.add('active');
  $('tab-details').classList.add('active');

  const grid = $('info-grid');

  // Compute width/height from bounding box
  const bb = getSpaceBBox(space);
  const spaceW = bb ? (bb.maxX - bb.minX).toFixed(2) : '0';
  const spaceH = bb ? (bb.maxY - bb.minY).toFixed(2) : '0';

  const typeOptions = SPACE_TYPES.map(t =>
    `<option value="${t}"${t === space.space_type ? ' selected' : ''}>${t.replace(/_/g, ' ')}</option>`
  ).join('');

  grid.innerHTML = `
    <span class="lbl">Name</span><input class="val-input" id="info-name" value="${(space.display_name || '').replace(/"/g, '&quot;')}">
    <span class="lbl">Type</span><select class="val-input" id="info-type">${typeOptions}</select>
    <span class="lbl">Width (m)</span><input class="val-input" id="info-width" type="number" step="0.1" value="${spaceW}">
    <span class="lbl">Height (m)</span><input class="val-input" id="info-height" type="number" step="0.1" value="${spaceH}">
    <span class="lbl">X</span><input class="val-input" id="info-cx" type="number" step="0.1" value="${(space.centroid_x||0).toFixed(2)}">
    <span class="lbl">Y</span><input class="val-input" id="info-cy" type="number" step="0.1" value="${(space.centroid_y||0).toFixed(2)}">
    <span class="lbl">Accessible</span><span class="val"><input type="checkbox" id="info-accessible" ${space.is_accessible ? 'checked' : ''}></span>
  `;

  // Parent indicator
  const parentDiv = $('info-parent');
  if (space.parent_space_id) {
    parentDiv.innerHTML = `<div style="margin-bottom:10px;padding:6px 10px;background:#f0f7ff;border-radius:6px;font-size:13px">Subspace of: <a href="#" class="parent-link" data-id="${space.parent_space_id}">${space.parent_space_name || space.parent_space_id}</a></div>`;
    parentDiv.querySelector('.parent-link').addEventListener('click', (e) => {
      e.preventDefault();
      const parent = state.spaces.find(s => s.id === space.parent_space_id);
      if (parent) showInfoModal(parent);
    });
  } else {
    parentDiv.innerHTML = '';
  }

  // Room images tab
  renderRoomSummary(space);
  renderImageViewer(space);
  ['img-north','img-east','img-south','img-west'].forEach(id => { $(id).value = ''; });
  $('analyze-status').textContent = '';
  $('analyze-room-btn').disabled = false;

  // Connections
  const connDiv = $('info-connections');
  connDiv.innerHTML = '<span style="color:#aaa">Loading...</span>';
  $('info-overlay').style.display = 'flex';

  try {
    const conns = await api(`/spaces/${space.id}/connections`);
    if (!conns.length) {
      connDiv.innerHTML = '<span style="color:#aaa">No connections</span>';
    } else {
      connDiv.innerHTML = conns.map(c => {
        const doorOpts = CONN_SPACE_TYPES.map(t =>
          `<option value="${t}"${t === c.door_type ? ' selected' : ''}>${t.replace(/_/g, ' ')}</option>`
        ).join('');
        return `
          <div class="conn-item">
            <span class="conn-dir">&#8594;</span>
            <span>${c.other_space_name || c.other_space_id}</span>
            <div class="conn-controls">
              <select class="conn-type-select" data-door-id="${c.door_node_id}">${doorOpts}</select>
              <label><input type="checkbox" class="conn-accessible-check" data-door-id="${c.door_node_id}" ${c.door_accessible ? 'checked' : ''}> Accessible</label>
            </div>
          </div>
        `;
      }).join('');
    }
  } catch (e) {
    connDiv.innerHTML = '<span style="color:#d9534f">Failed to load connections</span>';
  }

  // Subspaces
  const subDiv = $('info-subspaces');
  if (space.child_spaces && space.child_spaces.length) {
    subDiv.innerHTML = `<h3>Subspaces</h3>` +
      space.child_spaces.map(cs =>
        `<div class="subspace-item"><a href="#" class="subspace-link" data-id="${cs.id}">${cs.name || cs.id}</a></div>`
      ).join('');
    subDiv.querySelectorAll('.subspace-link').forEach(link => {
      link.addEventListener('click', (e) => {
        e.preventDefault();
        const child = state.spaces.find(s => s.id === link.dataset.id);
        if (child) showInfoModal(child);
      });
    });
  } else {
    subDiv.innerHTML = '';
  }
}

$('info-save').addEventListener('click', async () => {
  const space = state._editingSpace;
  if (!space) return;

  const newName = $('info-name').value.trim();
  const newW = parseFloat($('info-width').value) || 1;
  const newH = parseFloat($('info-height').value) || 1;
  const newCx = parseFloat($('info-cx').value) || 0;
  const newCy = parseFloat($('info-cy').value) || 0;

  const halfW = newW / 2, halfH = newH / 2;
  const newPoly = [
    [newCx - halfW, newCy - halfH],
    [newCx + halfW, newCy - halfH],
    [newCx + halfW, newCy + halfH],
    [newCx - halfW, newCy + halfH],
    [newCx - halfW, newCy - halfH],
  ];

  try {
    await api(`/spaces/${space.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        display_name: newName,
        space_type: $('info-type').value,
        is_accessible: $('info-accessible').checked,
        centroid_x: newCx,
        centroid_y: newCy,
        polygon: newPoly,
      }),
    });

    // Save connection (door node) edits
    const connSaves = [...document.querySelectorAll('.conn-type-select')].map(sel => {
      const doorId = sel.dataset.doorId;
      const check = document.querySelector(`.conn-accessible-check[data-door-id="${doorId}"]`);
      return api(`/spaces/${doorId}`, {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ space_type: sel.value, is_accessible: check?.checked ?? true }),
      });
    });
    await Promise.all(connSaves);

    notify(`Updated "${newName}"`);
    $('info-overlay').style.display = 'none';
    await reloadCurrentFloor();
  } catch (e) { /* handled */ }
});

$('analyze-room-btn').addEventListener('click', async () => {
  const space = state._editingSpace;
  if (!space) return;
  const north = $('img-north').files[0];
  const east  = $('img-east').files[0];
  const south = $('img-south').files[0];
  const west  = $('img-west').files[0];
  if (!north || !east || !south || !west) {
    notify('Please select all 4 directional images (N/E/S/W)', 'error');
    return;
  }
  const fd = new FormData();
  fd.append('room_name', space.display_name || space.id);
  fd.append('north_image', north);
  fd.append('east_image', east);
  fd.append('south_image', south);
  fd.append('west_image', west);

  $('analyze-status').textContent = 'Analyzing images...';
  $('analyze-room-btn').disabled = true;
  try {
    const result = await api('/room-summary/room-objects/setup', { method: 'POST', body: fd });
    renderRoomSummary(result);

    // Reload floor data to get the stored room_images from Neo4j
    const spaceId = space.id;
    await reloadCurrentFloor();
    const updatedSpace = state.spaces.find(s => s.id === spaceId);
    if (updatedSpace) {
      state._editingSpace = updatedSpace;
      renderImageViewer(updatedSpace);
    } else {
      renderImageViewer(result);
    }

    $('analyze-status').textContent = '';
    notify('Room analysis complete');
  } catch (e) {
    $('analyze-status').textContent = 'Analysis failed';
  } finally {
    $('analyze-room-btn').disabled = false;
  }
});

$('info-close').addEventListener('click', () => {
  $('info-overlay').style.display = 'none';
});
$('info-overlay').addEventListener('click', e => {
  if (e.target === $('info-overlay')) $('info-overlay').style.display = 'none';
});

/* ═══════════════════════════════════════════════
   Move Mode
   ═══════════════════════════════════════════════ */
function startMoveMode(space) {
  state.mode = 'moveSpace';
  state.moveSpace = space;
  state.moveOrigPoly = space.polygon ? space.polygon.map(p => [...p]) : null;
  state.moveOrigCentroid = [space.centroid_x, space.centroid_y];
  canvas.classList.add('crosshair');
  for (const btn of document.querySelectorAll('.tool-bar button')) {
    btn.classList.remove('active');
  }
  $('add-space-form').style.display = 'none';
  $('connect-form').style.display = 'none';
}

async function finishMove() {
  const space = state.moveSpace;
  if (!space) return;
  state.snapLines = [];
  try {
    const body = { centroid_x: space.centroid_x, centroid_y: space.centroid_y };
    if (space.polygon) body.polygon = space.polygon;
    await api(`/spaces/${space.id}`, {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(body),
    });
    notify(`Moved "${space.display_name}"`);
  } catch (e) {
    if (state.moveOrigPoly) space.polygon = state.moveOrigPoly;
    space.centroid_x = state.moveOrigCentroid[0];
    space.centroid_y = state.moveOrigCentroid[1];
  }
  exitMoveMode();
}

function cancelMove() {
  if (state.moveSpace && state.moveOrigPoly) {
    state.moveSpace.polygon = state.moveOrigPoly;
    state.moveSpace.centroid_x = state.moveOrigCentroid[0];
    state.moveSpace.centroid_y = state.moveOrigCentroid[1];
  }
  state.snapLines = [];
  exitMoveMode();
  render();
}

function exitMoveMode() {
  state.mode = 'select';
  state.moveSpace = null;
  state.moveOrigPoly = null;
  state.moveOrigCentroid = null;
  canvas.classList.remove('crosshair');
  $('tool-select').classList.add('active');
  render();
}

/* ═══════════════════════════════════════════════
   Duplicate Space
   ═══════════════════════════════════════════════ */
function duplicateSpace(space) {
  const cx = space.centroid_x || 0;
  const cy = space.centroid_y || 0;

  // Compute polygon offsets relative to centroid
  const relPoly = space.polygon.map(([x, y]) => [x - cx, y - cy]);

  const spaceData = {
    id: uuid(),
    display_name: (space.display_name || 'Space') + ' (copy)',
    space_type: space.space_type,
    floor_id: state.selectedFloorId,
    building_id: state.selectedBuildingId || space.building_id,
    campus_id: state.selectedCampusId || space.campus_id,
    floor_index: state.currentFloorIndex,
    centroid_x: cx,
    centroid_y: cy,
    polygon: space.polygon.map(p => [...p]),
    is_accessible: space.is_accessible ?? true,
    is_navigable: space.is_navigable ?? true,
    is_outdoor: space.is_outdoor ?? false,
    tags: space.tags || [],
  };

  state.pendingDuplicate = spaceData;
  state.pendingDupOrigPoly = relPoly;
  state.mode = 'placeDuplicate';
  canvas.classList.add('crosshair');
  for (const btn of document.querySelectorAll('.tool-bar button')) {
    btn.classList.remove('active');
  }
  $('add-space-form').style.display = 'none';
  $('connect-form').style.display = 'none';
}

function exitDuplicatePlaceMode() {
  state.pendingDuplicate = null;
  state.pendingDupOrigPoly = null;
  state.snapLines = [];
  state.mode = 'select';
  canvas.classList.remove('crosshair');
  $('tool-select').classList.add('active');
  tooltip.style.display = 'none';
  render();
}

async function finishDuplicatePlace() {
  const spaceData = state.pendingDuplicate;
  if (!spaceData) return;
  exitDuplicatePlaceMode();
  try {
    await api('/spaces', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(spaceData),
    });
    notify(`Duplicated "${spaceData.display_name}"`);
    await reloadCurrentFloor();
  } catch (e) { /* handled */ }
}

/* ═══════════════════════════════════════════════
   Delete Space
   ═══════════════════════════════════════════════ */
async function deleteSpace(space) {
  if (!window.confirm(`Delete "${space.display_name}"? This cannot be undone.`)) return;
  try {
    await api(`/spaces/${space.id}`, { method: 'DELETE' });
    notify(`Deleted "${space.display_name}"`);
    await reloadCurrentFloor();
  } catch (e) { /* handled */ }
}
