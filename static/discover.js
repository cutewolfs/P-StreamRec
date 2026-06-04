// ============================================
// Discover Page - Browse live Chaturbate models
// ============================================

// Render a small platform badge overlaid on the thumbnail.
function renderPlatformBadge(sourceType) {
  var t = (sourceType || '').toLowerCase();
  var label = providerLabel(t);
  var cls = 'platform-badge platform-' + (t || 'unknown');
  return '<span class="' + cls + '" title="' + label + '">' + label + '</span>';
}

// State
let currentPage = 1;
let totalPages = 1;
let currentSource = '';
let currentGender = '';
let currentSearch = '';
let activeTags = [];
let searchTimeout = null;
let discoverProviders = [];
let providerCapsBySource = {};
let discoverRequestSeq = 0;
let isDiscoverLoading = false;
let paginationQueryKey = '';
let lockedTotalPages = null;
const DISCOVER_PAGE_LIMIT = 24;
// Set des usernames déjà suivis (toutes plateformes). Rempli au chargement
// par loadFollowedSet(), consulté par renderGrid pour colorer le cœur, et
// mis à jour par toggleFollowOnCard après chaque action.
let followedSet = new Set();

function sourceKey(username, sourceType) {
  return (sourceType || 'chaturbate') + ':' + (username || '');
}

function providerLabel(sourceType) {
  var meta = discoverProviders.find(function(p) { return p.sourceType === sourceType; });
  if (meta && meta.displayName) return meta.displayName;
  var t = (sourceType || '').toLowerCase();
  return t.charAt(0).toUpperCase() + t.slice(1);
}

function fallbackThumbnailUrl(username, sourceType) {
  var source = (sourceType || 'chaturbate').toLowerCase();
  if (source === 'chaturbate') {
    return 'https://roomimg.stream.highwebmedia.com/ri/' + encodeURIComponent(username || '') + '.jpg';
  }
  var label = providerLabel(source) || 'Live';
  var title = username || label;
  var svg = '<svg xmlns="http://www.w3.org/2000/svg" width="320" height="220" viewBox="0 0 320 220">' +
    '<rect fill="#151a24" width="320" height="220"/>' +
    '<rect fill="#2b3342" x="28" y="28" width="264" height="164" rx="8"/>' +
    '<text x="50%" y="46%" dominant-baseline="middle" text-anchor="middle" fill="#f8fafc" font-family="system-ui, -apple-system, sans-serif" font-size="20" font-weight="700">' + escapeHtml(label) + '</text>' +
    '<text x="50%" y="60%" dominant-baseline="middle" text-anchor="middle" fill="#cbd5e1" font-family="system-ui, -apple-system, sans-serif" font-size="15">' + escapeHtml(title) + '</text>' +
    '</svg>';
  return 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(svg);
}

function thumbnailUrlForModel(model, sourceType) {
  var thumbnail = String(model.thumbnail || '').trim();
  return thumbnail || fallbackThumbnailUrl(model.username, sourceType);
}

async function loadFollowedSet() {
  try {
    var res = await fetch('/api/following');
    if (!res.ok) return;
    var data = await res.json();
    followedSet = new Set((data.models || []).map(function(m) {
      return sourceKey(m.username, m.source_type || m.platform || 'chaturbate');
    }));
  } catch (e) {
    // Silencieux: cœurs s'affichent vides par défaut
  }
}

async function loadDiscoverProviders() {
  var select = document.getElementById('sourceFilter');
  try {
    var res = await fetch('/api/providers');
    if (!res.ok) return;
    var data = await res.json();
    discoverProviders = (data.providers || []).filter(function(provider) {
      return provider.capabilities && provider.capabilities.can_discover;
    });
    providerCapsBySource = {};
    discoverProviders.forEach(function(provider) {
      providerCapsBySource[provider.sourceType] = provider.capabilities || {};
    });
    if (!select) return;
    var current = select.value || currentSource;
    select.innerHTML = '<option value="">All sources</option>' + discoverProviders.map(function(provider) {
      return '<option value="' + escapeHtml(provider.sourceType) + '">' + escapeHtml(provider.displayName || provider.sourceType) + '</option>';
    }).join('');
    select.value = current;
  } catch (e) {
    // Discover reste utilisable avec les defaults serveur.
  }
}

// ============================================
// Fetch discover data
// ============================================
function discoverQueryKey() {
  return JSON.stringify({
    source: currentSource || '',
    gender: currentGender || '',
    search: currentSearch || '',
    tags: activeTags.slice().sort(),
    limit: DISCOVER_PAGE_LIMIT
  });
}

function buildDiscoverParams(page) {
  var params = new URLSearchParams({
    page: page || currentPage,
    limit: DISCOVER_PAGE_LIMIT
  });
  if (currentSource) params.set('source', currentSource);
  if (currentGender) params.set('gender', currentGender);
  if (currentSearch) params.set('search', currentSearch);
  if (activeTags.length > 0) params.set('tags', activeTags.join(','));
  return params;
}

async function fetchDiscover() {
  var requestSeq = ++discoverRequestSeq;
  var queryKey = discoverQueryKey();
  setPaginationLoading(true);
  var grid = document.getElementById('discoverGrid');
  grid.innerHTML = '<div class="empty-message"><div class="icon">&#9203;</div><p>Loading models...</p></div>';

  var params = buildDiscoverParams(currentPage);

  try {
    var res = await fetch('/api/discover?' + params.toString());
    if (requestSeq !== discoverRequestSeq) return;
    if (res.ok) {
      var data = await res.json();
      if (requestSeq !== discoverRequestSeq) return;
      renderGrid(data.models || [], data.provider_statuses || []);
      renderPagination(data.page || currentPage, data.total_pages || 1, queryKey);
    } else {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Failed to load models.</p></div>';
      document.getElementById('pagination').style.display = 'none';
    }
  } catch (e) {
    if (requestSeq !== discoverRequestSeq) return;
    console.error('Error loading discover:', e);
    grid.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Connection error.</p></div>';
    document.getElementById('pagination').style.display = 'none';
  } finally {
    if (requestSeq === discoverRequestSeq) {
      setPaginationLoading(false);
    }
  }
}

// ============================================
// Get number of columns in a CSS grid
// ============================================
function getGridColumnCount(grid) {
  var cols = getComputedStyle(grid).gridTemplateColumns;
  if (cols && cols !== 'none') {
    return cols.split(' ').length;
  }
  // Fallback: estimate from container width and min column size (280px + 24px gap)
  var width = grid.clientWidth;
  return Math.max(1, Math.floor((width + 24) / (280 + 24)));
}

// ============================================
// Render model grid
// ============================================
function renderEmpty(providerStatuses) {
  var grid = document.getElementById('discoverGrid');
  var status = null;
  if (currentSource) {
    status = (providerStatuses || []).find(function(item) {
      return item.source_type === currentSource;
    });
  }
  if (!status && providerStatuses && providerStatuses.length === 1) {
    status = providerStatuses[0];
  }

  var title = 'No models found';
  var detail = '';
  var action = '';
  if (status && status.status === 'auth_required') {
    title = (status.display_name || providerLabel(status.source_type)) + ' needs a connection';
    detail = status.detail || 'Connect this provider before loading live models.';
    action = '<button class="btn-primary empty-action" onclick="window.location.href=\'/settings\'">Open Settings</button>';
  } else if (status && status.detail) {
    title = (status.display_name || providerLabel(status.source_type)) + ' is not available';
    detail = status.detail;
  }

  grid.innerHTML = '<div class="empty-message"><div class="icon">&#128269;</div><p>' + escapeHtml(title) + '</p>' +
    (detail ? '<span class="empty-detail">' + escapeHtml(detail) + '</span>' : '') +
    action +
    '</div>';
}

function renderGrid(models, providerStatuses) {
  var grid = document.getElementById('discoverGrid');

  if (!models.length) {
    renderEmpty(providerStatuses || []);
    return;
  }

  // Detect column count from the grid's computed style and trim to a full row
  var cols = getGridColumnCount(grid);
  if (cols > 1 && models.length > cols) {
    models = models.slice(0, Math.floor(models.length / cols) * cols);
  }

  grid.innerHTML = models.map(function(model) {
    var cardSource = (model.source_type || model.platform || 'chaturbate');
    var thumbUrl = thumbnailUrlForModel(model, cardSource);
    var fallbackThumbUrl = fallbackThumbnailUrl(model.username, cardSource);
    var viewerText = '<span class="discover-viewers">&#128065; ' + Number(model.viewers || 0).toLocaleString() + '</span>';
    var ageText = model.age ? ('<span class="discover-age">' + model.age + '</span>') : '';
    var tagsHtml = '';
    if (model.tags && model.tags.length > 0) {
      var displayTags = model.tags.slice(0, 3);
      tagsHtml = '<div class="discover-tags">' + displayTags.map(function(t) {
        return '<span class="discover-tag" onclick="event.stopPropagation(); addTagFilter(\'' + escapeHtml(t) + '\')">' + escapeHtml(t) + '</span>';
      }).join('') + '</div>';
    }

    var streamAvailable = model.stream_available !== false;
    var cardCaps = providerCapsBySource[cardSource] || {};
    var localFollowAvailable = cardCaps.can_stream !== false || cardCaps.can_record === true;
    var canFollow = model.can_follow !== false && (cardCaps.can_follow !== false || localFollowAvailable);
    var isFollowed = followedSet.has(sourceKey(model.username, cardSource));
    var heartBtn = canFollow ? '<button class="discover-follow-heart ' + (isFollowed ? 'is-followed' : '') + '" ' +
      'title="' + (isFollowed ? 'Unfollow' : 'Follow') + ' ' + escapeHtml(model.username) + '" ' +
      'onclick="event.stopPropagation(); toggleFollowOnCard(\'' + escapeHtml(model.username) + '\', \'' + escapeHtml(cardSource) + '\', this)">&#9829;</button>' : '';
    var streamBadge = streamAvailable ? '' : '<span class="discover-stream-status">Unavailable</span>';
    var cardClass = 'discover-card' + (streamAvailable ? '' : ' is-discover-only');
    var cardAction = streamAvailable
      ? ' onclick="openWatch(\'' + escapeHtml(model.username) + '\', \'' + escapeHtml(cardSource) + '\')"'
      : ' title="Live playback is not available for this provider yet"';

    return '<div class="' + cardClass + '" data-username="' + escapeHtml(model.username) + '" data-source="' + escapeHtml(cardSource) + '"' + cardAction + '>' +
      '<div class="discover-card-thumb">' +
        '<img src="' + escapeHtml(thumbUrl) + '" alt="' + escapeHtml(model.username) + '" ' +
          'onerror="this.onerror=null;this.src=\'' + escapeHtml(fallbackThumbUrl) + '\'" loading="lazy" />' +
        viewerText +
        ageText +
        renderPlatformBadge(model.source_type || model.platform || 'chaturbate') +
        streamBadge +
        heartBtn +
      '</div>' +
      '<div class="discover-card-info">' +
        '<span class="discover-username">' + escapeHtml(model.username) + '</span>' +
        tagsHtml +
      '</div>' +
    '</div>';
  }).join('');
}

// ============================================
// Open watch page
// ============================================
function openWatch(username, sourceType) {
  var qs = sourceType ? ('?source=' + encodeURIComponent(sourceType)) : '';
  window.location.href = '/watch/' + encodeURIComponent(username) + qs;
}

// ============================================
// Follow / Unfollow depuis la card Discover
// ============================================
async function toggleFollowOnCard(username, sourceType, btn) {
  if (!username || btn.classList.contains('busy')) return;
  var key = sourceKey(username, sourceType);
  var wasFollowing = followedSet.has(key);
  var base = '/api/providers/' + encodeURIComponent(sourceType || 'chaturbate');
  var endpoint = base + (wasFollowing ? '/unfollow/' : '/follow/') + encodeURIComponent(username);

  btn.classList.add('busy');
  try {
    var res = await fetch(endpoint, { method: 'POST' });
    if (res.ok) {
      if (wasFollowing) {
        followedSet.delete(key);
        btn.classList.remove('is-followed');
        btn.title = 'Follow ' + username;
      } else {
        followedSet.add(key);
        btn.classList.add('is-followed');
        btn.title = 'Unfollow ' + username;
      }
      showNotification(
        wasFollowing ? 'Unfollowed ' + username : 'Now following ' + username,
        'success'
      );
    } else {
      var detail = wasFollowing ? 'Failed to unfollow' : 'Failed to follow';
      try { var d = await res.json(); if (d && d.detail) detail = d.detail; } catch (e) {}
      showNotification(detail, 'error');
    }
  } catch (e) {
    showNotification('Connection error', 'error');
  } finally {
    btn.classList.remove('busy');
  }
}

// ============================================
// Tag filtering
// ============================================
function addTagFilter(tag) {
  tag = tag.toLowerCase().trim();
  if (!tag || activeTags.indexOf(tag) !== -1) return;
  activeTags.push(tag);
  currentPage = 1;
  renderActiveTagFilters();
  fetchDiscover();
}

function removeTagFilter(tag) {
  activeTags = activeTags.filter(function(t) { return t !== tag; });
  currentPage = 1;
  renderActiveTagFilters();
  fetchDiscover();
}

function clearAllTags() {
  activeTags = [];
  currentPage = 1;
  renderActiveTagFilters();
  fetchDiscover();
}

function renderActiveTagFilters() {
  var container = document.getElementById('activeTagFilters');
  if (!container) return;

  if (activeTags.length === 0) {
    container.style.display = 'none';
    return;
  }

  container.style.display = 'flex';
  container.innerHTML = activeTags.map(function(tag) {
    return '<span class="active-tag-chip">' +
      escapeHtml(tag) +
      '<button onclick="event.stopPropagation(); removeTagFilter(\'' + escapeHtml(tag) + '\')">&times;</button>' +
    '</span>';
  }).join('') +
  '<button class="clear-tags-btn" onclick="clearAllTags()">Clear all</button>';
}

function handleTagInput(e) {
  if (e.key === 'Enter') {
    var input = document.getElementById('tagInput');
    var val = input.value.trim();
    if (val) {
      addTagFilter(val);
      input.value = '';
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

// ============================================
// Pagination
// ============================================
function setPaginationLoading(isLoading) {
  isDiscoverLoading = isLoading;
  var prevBtn = document.getElementById('prevBtn');
  var nextBtn = document.getElementById('nextBtn');
  if (!prevBtn || !nextBtn) return;
  prevBtn.disabled = isLoading || currentPage <= 1;
  nextBtn.disabled = isLoading || currentPage >= totalPages;
}

function renderPagination(page, pages, queryKey) {
  page = Math.max(1, Number(page) || 1);
  pages = Math.max(1, Number(pages) || 1);
  if (queryKey && paginationQueryKey !== queryKey) {
    paginationQueryKey = queryKey;
    lockedTotalPages = null;
  }
  if (page <= 1 || lockedTotalPages === null) {
    lockedTotalPages = pages;
  } else {
    pages = lockedTotalPages;
  }
  pages = Math.max(page, pages);

  currentPage = page;
  totalPages = pages;

  var paginationEl = document.getElementById('pagination');
  var prevBtn = document.getElementById('prevBtn');
  var nextBtn = document.getElementById('nextBtn');
  var pageInfo = document.getElementById('pageInfo');

  if (pages <= 1) {
    paginationEl.style.display = 'none';
    return;
  }

  paginationEl.style.display = 'flex';
  prevBtn.disabled = page <= 1;
  nextBtn.disabled = page >= pages;
  pageInfo.textContent = 'Page ' + page + ' of ' + pages;
}

function changePage(delta) {
  if (isDiscoverLoading) return;
  var newPage = currentPage + delta;
  if (newPage >= 1 && newPage <= totalPages) {
    currentPage = newPage;
    fetchDiscover();
    window.scrollTo({ top: 0, behavior: 'smooth' });
  }
}

// ============================================
// Filter handlers
// ============================================
function setGender(gender, btn) {
  currentGender = gender;
  currentPage = 1;
  var pills = document.querySelectorAll('.filter-pill');
  pills.forEach(function(pill) { pill.classList.remove('active'); });
  if (btn) btn.classList.add('active');
  fetchDiscover();
}

function setSource(sourceType) {
  currentSource = sourceType || '';
  currentPage = 1;
  fetchDiscover();
}

function searchModels(query) {
  currentSearch = query;
  currentPage = 1;
  fetchDiscover();
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
// Re-fetch l'API et update uniquement les src des miniatures. L'API renvoie
// à chaque appel une URL avec signature/timestamp frais (Chaturbate:
// ?1776964320, CAM4: ?s=...), donc le browser re-télécharge sans avoir à
// cache-buster manuellement.
async function refreshLiveThumbnails() {
  if (document.hidden) return; // suspend en background tab
  var grid = document.getElementById('discoverGrid');
  if (!grid || !grid.querySelector('.discover-card')) return;

  var params = buildDiscoverParams(currentPage);

  try {
    var res = await fetch('/api/discover?' + params.toString());
    if (!res.ok) return;
    var data = await res.json();
    (data.models || []).forEach(function(model) {
      var card = grid.querySelector('.discover-card[data-username="' + CSS.escape(model.username) + '"]');
      if (!card) return;
      var img = card.querySelector('img');
      if (!img) return;
      var cardSource = card.getAttribute('data-source') || model.source_type || model.platform || currentSource || 'chaturbate';
      var newThumb = thumbnailUrlForModel(model, cardSource);
      if (img.getAttribute('src') !== newThumb) img.src = newThumb;
    });
  } catch (e) {
    // Silencieux: on réessaiera au prochain tick
  }
}

// ============================================
// Initialization
// ============================================
window.addEventListener('DOMContentLoaded', function() {
  var style = document.createElement('style');
  style.textContent = '@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }';
  document.head.appendChild(style);

  // Set up search with debounce
  var searchInput = document.getElementById('searchInput');
  if (searchInput) {
    searchInput.addEventListener('input', function() {
      clearTimeout(searchTimeout);
      searchTimeout = setTimeout(function() {
        searchModels(searchInput.value.trim());
      }, 400);
    });
    searchInput.addEventListener('keydown', function(e) {
      if (e.key === 'Enter') {
        clearTimeout(searchTimeout);
        searchModels(searchInput.value.trim());
      }
    });
  }

  // Set up tag input
  var tagInput = document.getElementById('tagInput');
  if (tagInput) {
    tagInput.addEventListener('keydown', handleTagInput);
  }

  var sourceFilter = document.getElementById('sourceFilter');
  if (sourceFilter) {
    sourceFilter.addEventListener('change', function() {
      setSource(sourceFilter.value);
    });
  }

  // Charger la liste des follows avant le premier render pour que les cœurs
  // soient coloriés correctement dès l'affichage.
  Promise.all([loadFollowedSet(), loadDiscoverProviders()]).finally(fetchDiscover);

  // Refresh live thumbnails toutes les 30s
  setInterval(refreshLiveThumbnails, 30000);
});
