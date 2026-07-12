// ============================================
// Following Page - View and manage followed models
// ============================================

// Chaturbate room statuses that mean the model is still broadcasting but
// not publicly watchable (private/group/spy/password/hidden). Treated as
// "Private" instead of "Offline" in the UI.
const PRIVATE_STATUSES = [
  'private', 'group', 'password_protected', 'password protected',
  'hidden', 'true_private', 'private_spy'
];

function isPrivateRoomStatus(roomStatus) {
  return PRIVATE_STATUSES.indexOf((roomStatus || '').toLowerCase()) !== -1;
}

function isPubliclyOnline(model) {
  return !isPrivateRoomStatus(model.room_status || model.roomStatus) &&
    Boolean(model.isOnline || model.is_online);
}

function isPrivateModel(model) {
  return isPrivateRoomStatus(model.room_status || model.roomStatus);
}

function isLiveFollowingModel(model) {
  return isPubliclyOnline(model) || isPrivateModel(model);
}

// Render a small platform badge overlaid on the thumbnail.
function renderPlatformBadge(sourceType) {
  var t = (sourceType || '').toLowerCase();
  var label = t.charAt(0).toUpperCase() + t.slice(1);
  var cls = 'platform-badge platform-' + (t || 'unknown');
  return '<span class="' + cls + '" title="' + label + '">' + label + '</span>';
}

function renderProviderBadge(sourceType) {
  var t = (sourceType || '').toLowerCase();
  var label = t.charAt(0).toUpperCase() + t.slice(1);
  var cls = 'following-provider-badge platform-' + (t || 'unknown');
  return '<span class="' + cls + '">' + escapeHtml(label) + '</span>';
}

function sourceKey(username, sourceType) {
  return normalizeSourceType(sourceType || 'chaturbate') + ':' + (username || '');
}

// State
let trackedModels = new Set();
let followingProviders = [];
let followingProviderMap = {};
let currentFollowingModels = [];
let followingFilters = {
  search: '',
  source: 'all',
  status: 'all'
};

// ============================================
// Load provider capabilities
// ============================================
async function loadFollowingProviders() {
  try {
    var res = await fetch('/api/providers', { cache: 'no-store' });
    if (res.ok) {
      var data = await res.json();
      setFollowingProviders(data.providers || []);
      return true;
    }
  } catch (e) {
    console.error('Error loading provider status:', e);
  }
  followingProviders = [];
  followingProviderMap = {};
  return false;
}

function setFollowingProviders(providers) {
  followingProviders = providers || [];
  followingProviderMap = {};
  followingProviders.forEach(function(provider) {
    var sourceType = normalizeSourceType(provider.sourceType || provider.source_type);
    if (sourceType) followingProviderMap[sourceType] = provider;
  });
}

function mergeFollowingProviderSummaries(providers) {
  if (!providers || !providers.length) return;
  var merged = followingProviders.slice();
  var indexBySource = {};
  merged.forEach(function(provider, index) {
    indexBySource[normalizeSourceType(provider.sourceType || provider.source_type)] = index;
  });
  providers.forEach(function(provider) {
    var sourceType = normalizeSourceType(provider.sourceType || provider.source_type);
    if (!sourceType) return;
    if (indexBySource[sourceType] == null) {
      indexBySource[sourceType] = merged.length;
      merged.push(provider);
    } else {
      merged[indexBySource[sourceType]] = Object.assign({}, merged[indexBySource[sourceType]], provider);
    }
  });
  setFollowingProviders(merged);
}

// ============================================
// Load tracked models
// ============================================
async function loadTrackedModels() {
  try {
    var res = await fetch('/api/models');
    if (res.ok) {
      var data = await res.json();
      trackedModels = new Set((data.models || []).map(function(m) {
        return sourceKey(m.username, m.sourceType || m.source_type || 'chaturbate');
      }));
    }
  } catch (e) {
    console.error('Error loading tracked models:', e);
  }
}

// ============================================
// Load following list
// ============================================
async function loadFollowing() {
  try {
    var res = await fetch('/api/following');
    if (res.ok) {
      var data = await res.json();
      mergeFollowingProviderSummaries(data.providers || []);
      return data.models || data.following || [];
    }
  } catch (e) {
    console.error('Error loading following:', e);
  }
  return [];
}

// ============================================
// Render following models
// ============================================
function renderFollowing(models) {
  var providerSections = document.getElementById('providerSections');
  var emptyFollowing = document.getElementById('emptyFollowing');

  models = sortFollowingModels(models || []);
  currentFollowingModels = models;

  if (models.length === 0) {
    providerSections.style.display = 'none';
    emptyFollowing.style.display = 'flex';
    return;
  }

  emptyFollowing.style.display = 'none';
  providerSections.style.display = 'block';
  providerSections.innerHTML = renderGlobalFollowingSection(models, filterFollowingModels(models));
}

function normalizeSourceType(sourceType) {
  return (sourceType || '').toString().trim().toLowerCase();
}

function modelSourceType(model) {
  return normalizeSourceType(model.source_type || model.platform || 'chaturbate');
}

function providerLabel(sourceType) {
  var provider = followingProviderMap[normalizeSourceType(sourceType)] || {};
  return provider.displayName || provider.display_name || sourceType || 'Unknown';
}

function providersForFollowing(models) {
  var providerBySource = {};

  followingProviders.forEach(function(provider) {
    var sourceType = normalizeSourceType(provider.sourceType || provider.source_type);
    if (!sourceType) return;
    providerBySource[sourceType] = Object.assign({}, provider, { sourceType: sourceType });
  });

  models.forEach(function(model) {
    var sourceType = modelSourceType(model);
    if (!providerBySource[sourceType]) {
      var known = followingProviderMap[sourceType] || {};
      providerBySource[sourceType] = Object.assign({
        sourceType: sourceType,
        displayName: providerLabel(sourceType),
        capabilities: known.capabilities || {},
        status: known.status || {}
      }, known);
    }
  });

  return Object.keys(providerBySource).map(function(sourceType) {
    return providerBySource[sourceType];
  }).sort(function(a, b) {
    var aModels = modelsForProvider(models, a.sourceType || a.source_type).length;
    var bModels = modelsForProvider(models, b.sourceType || b.source_type).length;
    if (aModels !== bModels) return bModels - aModels;
    return providerLabel(a.sourceType || a.source_type).localeCompare(providerLabel(b.sourceType || b.source_type));
  });
}

function modelsForProvider(models, sourceType) {
  var normalized = normalizeSourceType(sourceType);
  return (models || []).filter(function(model) {
    return modelSourceType(model) === normalized;
  }).sort(compareFollowingModels);
}

function sortFollowingModels(models) {
  return (models || []).slice().sort(compareFollowingModels);
}

function compareFollowingModels(a, b) {
  var viewersA = modelViewers(a);
  var viewersB = modelViewers(b);
  if (viewersA !== viewersB) return viewersB - viewersA;
  var rankA = modelStatusRank(a);
  var rankB = modelStatusRank(b);
  if (rankA !== rankB) return rankA - rankB;
  var sourceCompare = providerLabel(modelSourceType(a)).localeCompare(providerLabel(modelSourceType(b)));
  if (sourceCompare !== 0) return sourceCompare;
  return (a.username || a.name || '').localeCompare(b.username || b.name || '');
}

function modelViewers(model) {
  if (!isPubliclyOnline(model)) return 0;
  var value = Number(model.viewers || model.num_viewers || 0);
  return Number.isFinite(value) ? value : 0;
}

function modelStatusRank(model) {
  if (isPubliclyOnline(model)) return 0;
  if (isPrivateModel(model)) return 1;
  return 2;
}

function providerCountForModels(models) {
  var seen = {};
  (models || []).forEach(function(model) {
    seen[modelSourceType(model)] = true;
  });
  return Object.keys(seen).length;
}

function connectedFollowingProviders(models) {
  var seen = {};
  var sourceTypes = {};
  (models || []).forEach(function(model) {
    sourceTypes[modelSourceType(model)] = true;
  });
  return Object.keys(sourceTypes).map(function(sourceType) {
    var provider = followingProviderMap[sourceType] || {};
    return Object.assign({
      sourceType: sourceType,
      displayName: providerLabel(sourceType),
      status: {},
      capabilities: {}
    }, provider);
  }).filter(function(provider) {
    var sourceType = normalizeSourceType(provider.sourceType || provider.source_type);
    if (!sourceType || seen[sourceType]) return false;
    seen[sourceType] = true;
    return true;
  });
}

function modelsMatchingSource(models, sourceType) {
  var normalized = normalizeSourceType(sourceType);
  return (models || []).filter(function(model) {
    return modelSourceType(model) === normalized;
  });
}

function renderConnectedProviderMeta(models) {
  return connectedFollowingProviders(models).map(function(provider) {
    var sourceType = normalizeSourceType(provider.sourceType || provider.source_type);
    var providerModels = modelsMatchingSource(models, sourceType);
    var online = providerModels.filter(function(model) { return isPubliclyOnline(model); }).length;
    var name = providerLabel(sourceType).toUpperCase();
    return '<span class="following-provider-counter">' +
      '<span class="following-provider-counter-name">' + escapeHtml(name) + '</span>' +
      ' <span class="following-provider-counter-count">' + online + '/' + providerModels.length + '</span>' +
    '</span>';
  }).join(' ');
}

function renderGlobalFollowingSection(models, filteredModels) {
  var meta = renderConnectedProviderMeta(models);

  return '<section class="following-provider-section following-global-section">' +
    '<div class="following-provider-header">' +
      '<div>' +
        '<div class="following-provider-title">' +
          '<h2>All providers</h2>' +
          '<span class="count">' + models.length + '</span>' +
        '</div>' +
        (meta ? '<div class="following-provider-meta following-provider-counters">' + meta + '</div>' : '') +
      '</div>' +
    '</div>' +
    renderFollowingManagementToolbar(models, filteredModels) +
    renderFollowingList(filteredModels) +
  '</section>';
}

function filterFollowingModels(models) {
  var search = (followingFilters.search || '').trim().toLowerCase();
  var source = normalizeSourceType(followingFilters.source || 'all');
  var status = (followingFilters.status || 'all').toLowerCase();
  return sortFollowingModels(models || []).filter(function(model) {
    var username = (model.username || model.name || '').toLowerCase();
    var displayName = (model.display_name || model.displayName || '').toLowerCase();
    var sourceType = modelSourceType(model);
    var tracked = trackedModels.has(sourceKey(model.username || model.name || '', sourceType));
    if (search && username.indexOf(search) === -1 && displayName.indexOf(search) === -1) return false;
    if (source && source !== 'all' && sourceType !== source) return false;
    if (status === 'live' && !isPubliclyOnline(model)) return false;
    if (status === 'private' && !isPrivateModel(model)) return false;
    if (status === 'offline' && isLiveFollowingModel(model)) return false;
    if (status === 'tracked' && !tracked) return false;
    if (status === 'untracked' && tracked) return false;
    return true;
  });
}

function renderFollowingManagementToolbar(models, filteredModels) {
  var sourceOptions = [{ value: 'all', label: 'All providers' }];
  connectedFollowingProviders(models).forEach(function(provider) {
    var sourceType = normalizeSourceType(provider.sourceType || provider.source_type);
    var count = modelsMatchingSource(models, sourceType).length;
    sourceOptions.push({ value: sourceType, label: providerLabel(sourceType) + ' (' + count + ')' });
  });
  var statusOptions = [
    { value: 'all', label: 'All statuses' },
    { value: 'live', label: 'Live' },
    { value: 'private', label: 'Private' },
    { value: 'offline', label: 'Offline' },
    { value: 'tracked', label: 'Tracked' },
    { value: 'untracked', label: 'Untracked' }
  ];
  return '<div class="following-management-toolbar">' +
    '<label class="following-filter-field following-filter-search">' +
      '<span>Search</span>' +
      '<input id="followingSearchInput" type="search" value="' + escapeHtml(followingFilters.search) + '" oninput="updateFollowingFilter(\'search\', this.value)" placeholder="Username">' +
    '</label>' +
    '<label class="following-filter-field">' +
      '<span>Provider</span>' +
      '<select id="followingSourceFilter" onchange="updateFollowingFilter(\'source\', this.value)">' +
        sourceOptions.map(function(option) {
          return '<option value="' + escapeHtml(option.value) + '"' + (followingFilters.source === option.value ? ' selected' : '') + '>' + escapeHtml(option.label) + '</option>';
        }).join('') +
      '</select>' +
    '</label>' +
    '<label class="following-filter-field">' +
      '<span>Status</span>' +
      '<select id="followingStatusFilter" onchange="updateFollowingFilter(\'status\', this.value)">' +
        statusOptions.map(function(option) {
          return '<option value="' + escapeHtml(option.value) + '"' + (followingFilters.status === option.value ? ' selected' : '') + '>' + escapeHtml(option.label) + '</option>';
        }).join('') +
      '</select>' +
    '</label>' +
    '<div class="following-filter-count">' + filteredModels.length + ' shown</div>' +
  '</div>';
}

function updateFollowingFilter(key, value) {
  if (key === 'search') followingFilters.search = value || '';
  if (key === 'source') followingFilters.source = normalizeSourceType(value || 'all') || 'all';
  if (key === 'status') followingFilters.status = (value || 'all').toLowerCase();
  renderFollowing(currentFollowingModels);
}

function renderFollowingList(models) {
  if (!models.length) {
    return '<div class="following-provider-empty">No follows match the current filters.</div>';
  }
  return '<div class="following-list">' + models.map(renderFollowingRow).join('') + '</div>';
}

function renderFollowingRow(model) {
  var username = model.username || model.name || '';
  var thumbUrl = model.thumbnail_url || model.thumbnail || ('https://roomimg.stream.highwebmedia.com/ri/' + username + '.jpg');
  var sourceType = modelSourceType(model);
  var tracked = trackedModels.has(sourceKey(username, sourceType));
  var status = followingStatusLabel(model);
  var statusClass = isPubliclyOnline(model) ? 'is-live' : (isPrivateModel(model) ? 'is-private' : 'is-offline');
  var viewers = modelViewers(model);
  var watchHref = '/watch/' + encodeURIComponent(username) + '?source=' + encodeURIComponent(sourceType);
  var trackAction = tracked
    ? '<span class="following-row-tracked">Tracked</span>'
    : '<button type="button" class="following-row-btn" onclick="trackFollowedModel(\'' + escapeInlineJs(username) + '\', \'' + escapeInlineJs(sourceType) + '\', this)">Track</button>';
  return '<div class="following-list-row ' + statusClass + '" data-username="' + escapeHtml(username) + '" data-source="' + escapeHtml(sourceType) + '">' +
    '<a class="following-row-thumb" href="' + escapeHtml(watchHref) + '">' +
      '<img src="' + escapeHtml(thumbUrl) + '" alt="' + escapeHtml(username) + '" loading="lazy" ' +
        'onerror="this.src=\'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%2296%22 height=%2254%22%3E%3Crect fill=%22%231a1f3a%22 width=%2296%22 height=%2254%22/%3E%3Ctext x=%2250%25%22 y=%2250%25%22 dominant-baseline=%22middle%22 text-anchor=%22middle%22 fill=%22%23a0aec0%22 font-family=%22system-ui%22 font-size=%2211%22%3E' + escapeInlineJs(escapeHtml(username)) + '%3C/text%3E%3C/svg%3E\'" />' +
    '</a>' +
    '<div class="following-row-main">' +
      '<div class="following-row-title">' +
        '<span class="following-row-username">' + escapeHtml(username) + '</span>' +
        renderProviderBadge(sourceType) +
      '</div>' +
      '<div class="following-row-meta">' +
        '<span class="following-row-status ' + statusClass + '">' + escapeHtml(status) + '</span>' +
        (viewers ? '<span>' + viewers.toLocaleString() + ' viewers</span>' : '') +
        (model.last_seen_online_at && !isLiveFollowingModel(model) ? '<span>' + escapeHtml(formatLastSeen(model.last_seen_online_at)) + '</span>' : '') +
      '</div>' +
    '</div>' +
    '<div class="following-row-actions">' +
      '<a class="following-row-btn" href="' + escapeHtml(watchHref) + '">Watch</a>' +
      trackAction +
      '<button type="button" class="following-row-btn danger" onclick="unfollowFollowingModel(\'' + escapeInlineJs(username) + '\', \'' + escapeInlineJs(sourceType) + '\', this)">Unfollow</button>' +
    '</div>' +
  '</div>';
}

function followingStatusLabel(model) {
  if (model.isRecording || model.is_recording) return 'Recording';
  if (isPubliclyOnline(model)) return 'Live';
  if (isPrivateModel(model)) return 'Private';
  return 'Offline';
}

function syncCapableFollowingProviders() {
  return (followingProviders || []).filter(function(provider) {
    var caps = provider.capabilities || {};
    return caps.can_sync_following === true;
  });
}

function providerCanStartSync(provider) {
  var status = provider.status || {};
  return status.isLoggedIn === true || status.hasSavedCredentials === true || status.hasSavedSessionData === true;
}

function renderProviderSection(provider, models) {
  var sourceType = normalizeSourceType(provider.sourceType || provider.source_type);
  var displayName = provider.displayName || provider.display_name || providerLabel(sourceType);
  var caps = provider.capabilities || {};
  var status = provider.status || {};
  var online = models.filter(isLiveFollowingModel);
  var offline = models.filter(function(m) {
    return !isLiveFollowingModel(m);
  });
  var meta = providerMetaText(provider, models.length);
  var body = '';

  if (models.length) {
    body += renderProviderStatusGroup('Online', online, 'online');
    body += renderProviderStatusGroup('Offline', offline, 'offline');
  } else {
    body = '<div class="following-provider-empty">' + escapeHtml(emptyProviderText(provider)) + '</div>';
  }

  return '<section class="following-provider-section" data-source="' + escapeHtml(sourceType) + '">' +
    '<div class="following-provider-header">' +
      '<div>' +
        '<div class="following-provider-title">' +
          renderProviderBadge(sourceType) +
          '<h2>' + escapeHtml(displayName) + '</h2>' +
          '<span class="count">' + models.length + '</span>' +
        '</div>' +
        '<div class="following-provider-meta">' + escapeHtml(meta) + '</div>' +
      '</div>' +
    '</div>' +
    body +
  '</section>';
}

function renderProviderStatusGroup(label, models, kind) {
  if (!models.length) return '';
  var dot = kind === 'online' ? 'var(--success)' : 'var(--text-muted)';
  return '<div class="following-provider-status-group">' +
    '<div class="following-provider-status-title">' +
      '<span style="color: ' + dot + ';">&#9679;</span>' +
      '<span>' + label + '</span>' +
      '<span class="count">' + models.length + '</span>' +
    '</div>' +
    '<div class="following-grid">' + models.map(function(model) { return renderFollowingCard(model); }).join('') + '</div>' +
  '</div>';
}

function providerMetaText(provider, modelCount) {
  var pieces = [];
  pieces.push('Local follows');
  if (modelCount === 0) pieces.push('none saved');
  return pieces.join(' / ');
}

function emptyProviderText(provider) {
  return 'No local follows saved for this provider.';
}

function renderFollowingCard(model) {
  var username = model.username || model.name || '';
  var thumbUrl = model.thumbnail_url || model.thumbnail || ('https://roomimg.stream.highwebmedia.com/ri/' + username + '.jpg');
  var sourceType = model.source_type || model.platform || 'chaturbate';
  var isTracked = trackedModels.has(sourceKey(username, sourceType));
  var isRecording = model.isRecording || model.is_recording || false;
  var isPrivate = isPrivateModel(model);
  var isOnline = isPubliclyOnline(model);

  var statusBadge = '';
  if (isRecording) {
    statusBadge = '<span class="recording-badge-sm">REC</span>';
  } else if (isOnline) {
    statusBadge = '<span class="online-badge-sm">Online</span>';
  } else if (isPrivate) {
    statusBadge = '<span class="private-badge-sm">Private</span>';
  } else {
    statusBadge = '<span class="offline-badge-sm">Offline</span>';
  }

  var platformBadge = renderPlatformBadge(model.source_type || model.platform || 'chaturbate');
  var watchHref = '/watch/' + encodeURIComponent(username) + '?source=' + encodeURIComponent(sourceType);
  var privateRibbon = isPrivate ? '<div class="following-private-ribbon">Private show</div>' : '';

  var imgFilter = (isOnline || isPrivate) ? '' : 'filter: grayscale(60%) brightness(0.75);';

  // Last seen info for offline models
  var subtitleHtml = '';
  if (isOnline && model.viewers > 0) {
    subtitleHtml = '<span style="font-size: 0.8rem; color: var(--text-secondary);">&#128065; ' + Number(model.viewers).toLocaleString() + ' viewers</span>';
  } else if (isPrivate) {
    subtitleHtml = '<span style="font-size: 0.8rem; color: #c084fc;">Currently in a private show</span>';
  } else if (!isOnline && model.last_seen_online_at) {
    subtitleHtml = '<span style="font-size: 0.8rem; color: var(--text-muted);">' + formatLastSeen(model.last_seen_online_at) + '</span>';
  }

  var cardClass = isOnline ? 'is-online' : (isPrivate ? 'is-private' : 'is-offline');
  return '<div class="following-card ' + cardClass + '" data-username="' + escapeHtml(username) + '">' +
    '<div class="following-card-thumb" title="' + (isPrivate ? 'Private show' : 'Watch live') + '" onclick="window.location.href=\'' + escapeInlineJs(watchHref) + '\'">' +
      '<img src="' + escapeHtml(thumbUrl) + '" alt="' + escapeHtml(username) + '" style="' + imgFilter + '" ' +
        'onerror="this.src=\'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22280%22 height=%22180%22%3E%3Crect fill=%22%231a1f3a%22 width=%22280%22 height=%22180%22/%3E%3Ctext x=%2250%25%22 y=%2250%25%22 dominant-baseline=%22middle%22 text-anchor=%22middle%22 fill=%22%23a0aec0%22 font-family=%22system-ui%22 font-size=%2216%22%3E' + escapeInlineJs(escapeHtml(username)) + '%3C/text%3E%3C/svg%3E\'" loading="lazy" />' +
      platformBadge +
      privateRibbon +
    '</div>' +
    '<div class="following-card-info">' +
      '<div class="following-card-header">' +
        '<span class="following-username">' + escapeHtml(username) + '</span>' +
        statusBadge +
      '</div>' +
      (subtitleHtml ? '<div style="margin-top: 0.35rem;">' + subtitleHtml + '</div>' : '') +
    '</div>' +
  '</div>';
}

function formatLastSeen(timestamp) {
  if (!timestamp) return '';
  var now = Math.floor(Date.now() / 1000);
  var diff = now - timestamp;
  if (diff < 60) return 'Last seen just now';
  if (diff < 3600) return 'Last seen ' + Math.floor(diff / 60) + 'm ago';
  if (diff < 86400) return 'Last seen ' + Math.floor(diff / 3600) + 'h ago';
  if (diff < 604800) return 'Last seen ' + Math.floor(diff / 86400) + 'd ago';
  var date = new Date(timestamp * 1000);
  return 'Last seen ' + date.toLocaleDateString();
}

// ============================================
// Track a followed model
// ============================================
async function trackFollowedModel(username, sourceType, btn) {
  if (typeof sourceType !== 'string') {
    btn = sourceType;
    sourceType = 'chaturbate';
  }
  sourceType = normalizeSourceType(sourceType || 'chaturbate');
  if (trackedModels.has(sourceKey(username, sourceType))) return;

  btn.textContent = '...';
  btn.disabled = true;

  try {
    // Try the dedicated following track endpoint first
    var res = await fetch('/api/following/' + username + '/track?source_type=' + encodeURIComponent(sourceType), {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' }
    });

    // Fallback to general models endpoint
    if (!res.ok && res.status === 404) {
      res = await fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: username,
          autoRecord: true,
          recordQuality: 'best',
          sourceType: sourceType
        })
      });
    }

    if (res.ok || res.status === 409) {
      trackedModels.add(sourceKey(username, sourceType));
      btn.textContent = 'Tracked';
      btn.classList.add('tracked');
      btn.disabled = false;
      renderFollowing(currentFollowingModels);
      showNotification(username + ' added to tracking!', 'success');
    } else {
      btn.textContent = 'Track';
      btn.disabled = false;
      showNotification('Failed to track ' + username, 'error');
    }
  } catch (e) {
    console.error('Error tracking model:', e);
    btn.textContent = 'Track';
    btn.disabled = false;
    showNotification('Connection error', 'error');
  }
}

async function unfollowFollowingModel(username, sourceType, btn) {
  sourceType = normalizeSourceType(sourceType || 'chaturbate');
  var originalText = btn ? btn.textContent : '';
  if (btn) {
    btn.disabled = true;
    btn.textContent = 'Removing...';
  }
  try {
    var res = await fetch('/api/providers/' + encodeURIComponent(sourceType) + '/unfollow/' + encodeURIComponent(username), {
      method: 'POST'
    });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok || data.success === false) {
      if (btn) {
        btn.disabled = false;
        btn.textContent = originalText;
      }
      showNotification(data.detail || data.error || 'Failed to unfollow ' + username, 'error');
      return false;
    }
    currentFollowingModels = currentFollowingModels.filter(function(model) {
      return !(modelSourceType(model) === sourceType && (model.username || model.name || '') === username);
    });
    renderFollowing(currentFollowingModels);
    showNotification('Unfollowed ' + username, 'success');
    return true;
  } catch (e) {
    console.error('Error unfollowing model:', e);
    if (btn) {
      btn.disabled = false;
      btn.textContent = originalText;
    }
    showNotification('Connection error', 'error');
    return false;
  }
}

// ============================================
// Refresh local following list
// ============================================
async function syncFollowing() {
  var options = arguments[0] || {};
  var silent = options.silent === true;

  try {
    if (!followingProviders.length) {
      await loadFollowingProviders();
    }

    var providers = syncCapableFollowingProviders().filter(providerCanStartSync);
    for (var i = 0; i < providers.length; i += 1) {
      var sourceType = normalizeSourceType(providers[i].sourceType || providers[i].source_type);
      await syncSingleProvider(sourceType, null, true);
    }

    var models = await loadFollowing();
    renderFollowing(models);
    if (!silent) showNotification(providers.length ? 'Following synced' : 'Following list refreshed', 'success');
    return models;
  } catch (e) {
    console.error('Error refreshing following:', e);
    if (!silent) showNotification('Connection error', 'error');
    return [];
  }
}

async function syncSingleProvider(sourceType, button, silent) {
  sourceType = normalizeSourceType(sourceType);
  if (!sourceType) return false;
  var originalText = button ? button.textContent : '';
  if (button) {
    button.disabled = true;
    button.textContent = 'Syncing...';
  }
  try {
    var res = await fetch('/api/providers/' + encodeURIComponent(sourceType) + '/following/sync', { method: 'POST' });
    var data = await res.json().catch(function() { return {}; });
    if (!res.ok) {
      if (!silent) showNotification(data.detail || 'Sync failed', 'error');
      return false;
    }
    await loadFollowingProviders();
    var models = await loadFollowing();
    renderFollowing(models);
    if (!silent) {
      if (data.trusted === false) {
        showNotification(data.skippedReason || data.message || 'Following sync skipped', 'error');
      } else {
        showNotification(data.message || 'Following synced', 'success');
      }
    }
    return true;
  } catch (e) {
    console.error('Error syncing provider:', e);
    if (!silent) showNotification('Connection error', 'error');
    return false;
  } finally {
    if (button) {
      button.disabled = false;
      button.textContent = originalText;
    }
  }
}

// ============================================
// Escape HTML helper
// ============================================
function escapeHtml(text) {
  if (!text) return '';
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(text));
  return div.innerHTML;
}

function escapeInlineJs(value) {
  return String(value == null ? '' : value)
    .replace(/\\/g, '\\\\')
    .replace(/'/g, '\\x27')
    .replace(/"/g, '\\x22')
    .replace(/&/g, '\\x26')
    .replace(/</g, '\\x3c')
    .replace(/>/g, '\\x3e')
    .replace(/\r/g, '\\r')
    .replace(/\n/g, '\\n')
    .replace(/\u2028/g, '\\u2028')
    .replace(/\u2029/g, '\\u2029');
}

// ============================================
// Notifications
// ============================================
function showNotification(message, type) {
  type = type || 'success';
  var notif = document.createElement('div');
  var bgColor = type === 'success' ? '#10b981' : '#ef4444';
  notif.style.cssText = 'position:fixed;top:20px;right:20px;background:' + bgColor + ';color:white;padding:1rem 1.5rem;border-radius:10px;box-shadow:0 10px 30px rgba(0,0,0,0.3);z-index:9999;font-weight:500;animation:slideIn 0.3s ease-out;';
  notif.textContent = message;
  document.body.appendChild(notif);

  setTimeout(function() {
    notif.style.opacity = '0';
    notif.style.transform = 'translateX(100px)';
    notif.style.transition = 'all 0.3s ease-out';
    setTimeout(function() { notif.remove(); }, 300);
  }, 3000);
}

// ============================================
// Auto-refresh live thumbnails
// ============================================
// /api/following lit depuis la DB SQLite, qui n'est rafraîchie que tous les
// 5min par sync_following_task. On ne peut donc pas s'appuyer sur l'API
// pour avoir des URLs fresh. Stratégie:
//   - Chaturbate (mmcdn / highwebmedia): cache-bust local via ?_cb=now,
//     le CDN ignore les query params non-signés et re-sert la version
//     courante au CDN.
//   - CAM4 (xcdnpro.com): l'URL contient une signature ?s=... qu'il ne
//     faut pas altérer. On skip — la miniature sera rafraîchie au prochain
//     sync CAM4.
function _bustChaturbateUrl(src) {
  if (!src) return src;
  var isChaturbate = src.indexOf('mmcdn.com') !== -1 ||
                     src.indexOf('highwebmedia.com') !== -1;
  if (!isChaturbate) return src;
  var base = src.split('#')[0];
  base = base.replace(/([?&])_cb=\d+(&|$)/, '$1').replace(/[?&]$/, '');
  var sep = base.indexOf('?') === -1 ? '?' : '&';
  return base + sep + '_cb=' + Date.now();
}

async function refreshLiveThumbnails() {
  if (document.hidden) return;
  var providerSections = document.getElementById('providerSections');
  if (!providerSections) return;
  var cards = providerSections.querySelectorAll('.following-card.is-online, .following-card.is-private');
  if (!cards.length) return;

  cards.forEach(function(card) {
    var img = card.querySelector('img');
    if (!img || !img.src) return;
    var fresh = _bustChaturbateUrl(img.src);
    if (fresh !== img.src) img.src = fresh;
  });
}

// ============================================
// Initialization
// ============================================
window.addEventListener('DOMContentLoaded', function() {
  // Refresh live thumbnails toutes les 30s
  setInterval(refreshLiveThumbnails, 30000);

  // Add animation keyframes
  var style = document.createElement('style');
  style.textContent = '@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } } @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }';
  document.head.appendChild(style);

  var loadingState = document.getElementById('loadingState');

  // Load data
  Promise.all([loadTrackedModels(), loadFollowingProviders()]).then(function() {
    loadingState.style.display = 'none';

    loadFollowing().then(function(models) {
      renderFollowing(models);
    });
  }).catch(function(e) {
    console.error('Error initializing following page:', e);
    loadingState.innerHTML = '<div class="icon">&#9888;</div><p>Failed to load. Please try refreshing.</p>';
  });
});
