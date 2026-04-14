// ============================================
// Following Page - View and manage followed models
// ============================================

// State
let trackedModels = new Set();
let isLoggedIn = false;

// ============================================
// Check Chaturbate login status
// ============================================
async function checkChaturbateStatus() {
  try {
    var res = await fetch('/api/chaturbate/status');
    if (res.ok) {
      var data = await res.json();
      return data.isLoggedIn === true;
    }
  } catch (e) {
    // Chaturbate status API not available
  }
  return false;
}

// ============================================
// Load tracked models
// ============================================
async function loadTrackedModels() {
  try {
    var res = await fetch('/api/models');
    if (res.ok) {
      var data = await res.json();
      trackedModels = new Set((data.models || []).map(function(m) { return m.username; }));
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
  var onlineGrid = document.getElementById('onlineGrid');
  var offlineGrid = document.getElementById('offlineGrid');
  var onlineSection = document.getElementById('onlineSection');
  var offlineSection = document.getElementById('offlineSection');
  var onlineCount = document.getElementById('onlineCount');
  var offlineCount = document.getElementById('offlineCount');
  var emptyFollowing = document.getElementById('emptyFollowing');

  if (!models || models.length === 0) {
    onlineSection.style.display = 'none';
    offlineSection.style.display = 'none';
    emptyFollowing.style.display = 'flex';
    return;
  }

  emptyFollowing.style.display = 'none';

  var online = models.filter(function(m) { return m.isOnline || m.is_online; });
  var offline = models.filter(function(m) { return !m.isOnline && !m.is_online; });

  // Online section
  if (online.length > 0) {
    onlineSection.style.display = 'block';
    onlineCount.textContent = online.length;
    onlineGrid.innerHTML = online.map(function(model) { return renderFollowingCard(model, true); }).join('');
  } else {
    onlineSection.style.display = 'none';
  }

  // Offline section
  if (offline.length > 0) {
    offlineSection.style.display = 'block';
    offlineCount.textContent = offline.length;
    offlineGrid.innerHTML = offline.map(function(model) { return renderFollowingCard(model, false); }).join('');
  } else {
    offlineSection.style.display = 'none';
  }
}

function renderFollowingCard(model, isOnline) {
  var username = model.username || model.name || '';
  var thumbUrl = model.thumbnail_url || model.thumbnail || ('https://roomimg.stream.highwebmedia.com/ri/' + username + '.jpg');
  var isTracked = trackedModels.has(username);
  var isRecording = model.isRecording || model.is_recording || false;

  var statusBadge = '';
  if (isRecording) {
    statusBadge = '<span class="recording-badge-sm">REC</span>';
  } else if (isOnline) {
    statusBadge = '<span class="online-badge-sm">LIVE</span>';
  } else {
    statusBadge = '<span class="offline-badge-sm">OFFLINE</span>';
  }

  var imgFilter = isOnline ? '' : 'filter: grayscale(60%) brightness(0.75);';

  // Last seen info for offline models
  var subtitleHtml = '';
  if (isOnline && model.viewers > 0) {
    subtitleHtml = '<span style="font-size: 0.8rem; color: var(--text-secondary);">&#128065; ' + Number(model.viewers).toLocaleString() + ' viewers</span>';
  } else if (!isOnline && model.last_seen_online_at) {
    subtitleHtml = '<span style="font-size: 0.8rem; color: var(--text-muted);">' + formatLastSeen(model.last_seen_online_at) + '</span>';
  }

  return '<div class="following-card ' + (isOnline ? 'is-online' : 'is-offline') + '">' +
    '<div class="following-card-thumb" onclick="window.location.href=\'/watch/' + escapeHtml(username) + '\'">' +
      '<img src="' + escapeHtml(thumbUrl) + '" alt="' + escapeHtml(username) + '" style="' + imgFilter + '" ' +
        'onerror="this.src=\'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22280%22 height=%22180%22%3E%3Crect fill=%22%231a1f3a%22 width=%22280%22 height=%22180%22/%3E%3Ctext x=%2250%25%22 y=%2250%25%22 dominant-baseline=%22middle%22 text-anchor=%22middle%22 fill=%22%23a0aec0%22 font-family=%22system-ui%22 font-size=%2216%22%3E' + escapeHtml(username) + '%3C/text%3E%3C/svg%3E\'" loading="lazy" />' +
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
async function trackFollowedModel(username, btn) {
  if (trackedModels.has(username)) return;

  btn.textContent = '...';
  btn.disabled = true;

  try {
    // Try the dedicated following track endpoint first
    var res = await fetch('/api/following/' + username + '/track', {
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
          retentionDays: 30
        })
      });
    }

    if (res.ok || res.status === 409) {
      trackedModels.add(username);
      btn.textContent = 'Tracked';
      btn.classList.add('tracked');
      btn.disabled = false;
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

// ============================================
// Sync following list
// ============================================
async function syncFollowing() {
  var syncBtn = document.getElementById('syncBtn');
  var syncIcon = document.getElementById('syncIcon');

  syncBtn.classList.add('syncing');
  syncIcon.style.animation = 'spin 1s linear infinite';

  try {
    var res = await fetch('/api/following/sync', { method: 'POST' });
    if (res.ok) {
      showNotification('Following list synced!', 'success');
      // Reload the page data
      var models = await loadFollowing();
      renderFollowing(models);
      updateLastSynced();
    } else {
      showNotification('Failed to sync following list', 'error');
    }
  } catch (e) {
    console.error('Error syncing following:', e);
    showNotification('Connection error', 'error');
  } finally {
    syncBtn.classList.remove('syncing');
    syncIcon.style.animation = '';
  }
}

function updateLastSynced() {
  var el = document.getElementById('lastSynced');
  if (el) {
    var now = new Date();
    el.textContent = 'Last synced: ' + now.toLocaleTimeString();
    localStorage.setItem('following_last_synced', now.toISOString());
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
// Initialization
// ============================================
window.addEventListener('DOMContentLoaded', function() {
  // Add animation keyframes
  var style = document.createElement('style');
  style.textContent = '@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } } @keyframes spin { from { transform: rotate(0deg); } to { transform: rotate(360deg); } }';
  document.head.appendChild(style);

  // Show last synced time from localStorage
  var lastSynced = localStorage.getItem('following_last_synced');
  if (lastSynced) {
    var el = document.getElementById('lastSynced');
    if (el) {
      var date = new Date(lastSynced);
      el.textContent = 'Last synced: ' + date.toLocaleString();
    }
  }

  var loadingState = document.getElementById('loadingState');
  var loginBanner = document.getElementById('loginBanner');
  var syncControls = document.getElementById('syncControls');

  // Load data
  Promise.all([loadTrackedModels(), checkChaturbateStatus()]).then(function(results) {
    isLoggedIn = results[1];

    loadingState.style.display = 'none';

    if (!isLoggedIn) {
      // Not logged in - show banner but still try to load following
      loginBanner.style.display = 'block';
      syncControls.style.display = 'none';

      // Try loading following anyway (might have cached data or public API)
      loadFollowing().then(function(models) {
        if (models.length > 0) {
          syncControls.style.display = 'block';
          renderFollowing(models);
        }
      });
    } else {
      // Logged in - show sync controls and load data
      loginBanner.style.display = 'none';
      syncControls.style.display = 'block';

      loadFollowing().then(function(models) {
        renderFollowing(models);
      });
    }
  }).catch(function(e) {
    console.error('Error initializing following page:', e);
    loadingState.innerHTML = '<div class="icon">&#9888;</div><p>Failed to load. Please try refreshing.</p>';
  });
});
