// ============================================
// Watch Page - Live stream viewer
// ============================================

const PRIVATE_STATUSES = [
  'private', 'group', 'password_protected', 'password protected',
  'hidden', 'true_private', 'private_spy'
];

function isPrivateRoomStatus(rs) {
  return PRIVATE_STATUSES.indexOf((rs || '').toLowerCase()) !== -1;
}

let currentUsername = '';
let currentSourceType = '';
let isFollowing = false;
let isAutoRecord = false;
let isModelTracked = false;
let hlsPlayer = null;
let statusCheckInterval = null;

function sourceQuery() {
  return currentSourceType ? ('?source=' + encodeURIComponent(currentSourceType)) : '';
}

// ============================================
// Extract username from URL
// ============================================
function getUsername() {
  var parts = window.location.pathname.split('/');
  // /watch/{username}
  return parts[2] || '';
}

// ============================================
// Initialize page
// ============================================
async function initWatch() {
  currentUsername = getUsername();
  if (!currentUsername) {
    document.getElementById('watchUsername').textContent = 'Error: No username';
    return;
  }

  // Lit le source_type depuis l'URL (?source=cam4) pour les modèles qui ne
  // sont pas encore dans le cache SQLite — évite le fallback par défaut vers
  // Chaturbate qui marque les CAM4 comme Offline.
  try {
    currentSourceType = new URLSearchParams(window.location.search).get('source') || '';
  } catch (e) {
    currentSourceType = '';
  }

  document.title = currentUsername + ' - P-StreamRec';
  document.getElementById('watchUsername').textContent = currentUsername;

  // Load everything in parallel
  await Promise.all([
    loadModelStatus(),
    loadFollowStatus(),
    loadTrackStatus(),
  ]);

  // Start periodic status check
  statusCheckInterval = setInterval(loadModelStatus, 15000);
}

// ============================================
// Load model status and start stream
// ============================================
async function loadModelStatus() {
  try {
    var res = await fetch('/api/model/' + currentUsername + '/status' + sourceQuery());
    if (!res.ok) return;
    var data = await res.json();

    var statusDot = document.getElementById('statusDot');
    var statusText = document.getElementById('statusText');
    var viewerCount = document.getElementById('viewerCount');
    var viewerNum = document.getElementById('viewerNum');
    var offlineOverlay = document.getElementById('offlineOverlay');
    var offlineTitle = document.getElementById('offlineTitle');
    var offlineText = document.getElementById('offlineText');
    var offlineIcon = document.getElementById('offlineIcon');

    var priv = !data.isOnline && isPrivateRoomStatus(data.roomStatus);
    updatePlatformBadge(data.sourceType);

    if (data.isOnline) {
      statusDot.className = 'status-dot online';
      statusText.textContent = 'Live';
      viewerCount.style.display = 'inline';
      viewerNum.textContent = Number(data.viewers || 0).toLocaleString();
      offlineOverlay.style.display = 'none';

      // Start stream if not already playing
      if (!hlsPlayer) {
        startStream();
      }
    } else {
      // Status says offline, but try loading the stream anyway
      // The status API can return false negatives (rate limiting, cache miss)
      if (!hlsPlayer && !priv) {
        var streamLoaded = await tryLoadStream();
        if (streamLoaded) {
          statusDot.className = 'status-dot online';
          statusText.textContent = 'Live';
          viewerCount.style.display = 'inline';
          viewerNum.textContent = Number(data.viewers || 0).toLocaleString();
          offlineOverlay.style.display = 'none';
          return;
        }
      }

      if (priv) {
        statusDot.className = 'status-dot private';
        statusText.textContent = 'Private';
        viewerCount.style.display = 'none';
        if (offlineIcon) offlineIcon.innerHTML = '&#128274;';
        if (offlineTitle) offlineTitle.textContent = 'Model is in a Private Show';
        if (offlineText) offlineText.textContent = formatPrivateText(data.roomStatus);
      } else {
        statusDot.className = 'status-dot offline';
        statusText.textContent = 'Offline';
        viewerCount.style.display = 'none';
        if (offlineIcon) offlineIcon.innerHTML = '&#128308;';
        if (offlineTitle) offlineTitle.textContent = 'Model is Offline';
        if (offlineText) offlineText.textContent = 'This model is currently not streaming.';
      }
      offlineOverlay.style.display = 'flex';

      // Stop stream if playing
      if (hlsPlayer) {
        hlsPlayer.destroy();
        hlsPlayer = null;
      }
    }
  } catch (e) {
    console.error('Error loading model status:', e);
  }
}

function renderPlatformBadge(sourceType) {
  var t = (sourceType || '').toLowerCase();
  var label = t.charAt(0).toUpperCase() + t.slice(1);
  return '<span class="platform-badge platform-' + (t || 'unknown') + '" title="' + label + '">' + label + '</span>';
}

function updatePlatformBadge(sourceType) {
  var container = document.getElementById('platformBadgeContainer');
  if (!container) return;
  if (!sourceType) {
    container.style.display = 'none';
    container.innerHTML = '';
    return;
  }
  container.style.display = 'inline-flex';
  container.innerHTML = renderPlatformBadge(sourceType);
}

function formatPrivateText(rs) {
  var s = (rs || '').toLowerCase();
  if (s === 'group') return 'The model is currently in a group show.';
  if (s === 'password_protected' || s === 'password protected') return 'The room is password-protected.';
  if (s === 'hidden') return 'The room is hidden from public viewers.';
  if (s === 'true_private' || s === 'private_spy') return 'The model is in a true private show.';
  return 'The model is in a private session.';
}

// ============================================
// Try loading stream (returns true if successful)
// ============================================
async function tryLoadStream() {
  try {
    var res = await fetch('/api/model/' + currentUsername + '/stream' + sourceQuery());
    if (!res.ok) return false;
    var data = await res.json();
    if (!data.streamUrl) return false;

    // Stream URL is available - start playing
    startStreamWithUrl(data.streamUrl);
    return true;
  } catch (e) {
    return false;
  }
}

// ============================================
// Start HLS stream
// ============================================
async function startStream() {
  try {
    var res = await fetch('/api/model/' + currentUsername + '/stream' + sourceQuery());
    if (!res.ok) {
      console.error('Failed to get stream URL');
      return;
    }
    var data = await res.json();
    var streamUrl = data.streamUrl;

    if (!streamUrl) {
      console.error('No stream URL returned');
      return;
    }

    startStreamWithUrl(streamUrl);
  } catch (e) {
    console.error('Error starting stream:', e);
  }
}

function startStreamWithUrl(streamUrl) {
  var video = document.getElementById('videoPlayer');

  if (Hls.isSupported()) {
    hlsPlayer = new Hls({
      enableWorker: true,
      lowLatencyMode: true,
      backBufferLength: 90,
    });
    hlsPlayer.loadSource(streamUrl);
    hlsPlayer.attachMedia(video);
    hlsPlayer.on(Hls.Events.MANIFEST_PARSED, function() {
      video.play().catch(function() {});
    });
    hlsPlayer.on(Hls.Events.ERROR, function(event, data) {
      if (data.fatal) {
        console.error('HLS fatal error:', data.type);
        hlsPlayer.destroy();
        hlsPlayer = null;
      }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    // Safari native HLS
    video.src = streamUrl;
    video.addEventListener('loadedmetadata', function() {
      video.play().catch(function() {});
    });
  }
}

// ============================================
// Retry loading stream (called from retry button)
// ============================================
async function retryStream() {
  var retryBtn = document.getElementById('retryBtn');
  if (retryBtn) {
    retryBtn.disabled = true;
    retryBtn.textContent = 'Retrying...';
  }

  await loadModelStatus();

  if (retryBtn) {
    retryBtn.disabled = false;
    retryBtn.textContent = 'Retry';
  }
}

// ============================================
// Follow status
// ============================================
function followBasePath() {
  // Route vers le bon service selon la plateforme.
  return currentSourceType === 'cam4' ? '/api/cam4' : '/api/chaturbate';
}

async function loadFollowStatus() {
  try {
    var res = await fetch(followBasePath() + '/is-following/' + currentUsername);
    if (!res.ok) return;
    var data = await res.json();
    isFollowing = data.isFollowing;
    updateFollowButton();
    document.getElementById('followBtn').style.display = 'inline-flex';
  } catch (e) {
    console.error('Error loading follow status:', e);
  }
}

function updateFollowButton() {
  var btn = document.getElementById('followBtn');
  var icon = document.getElementById('followIcon');
  var text = document.getElementById('followText');

  if (isFollowing) {
    btn.classList.add('active');
    icon.innerHTML = '&#9829;';
    text.textContent = 'Unfollow';
  } else {
    btn.classList.remove('active');
    icon.innerHTML = '&#9825;';
    text.textContent = 'Follow';
  }
}

async function toggleFollow() {
  var btn = document.getElementById('followBtn');
  btn.disabled = true;

  try {
    var endpoint = isFollowing
      ? followBasePath() + '/unfollow/' + currentUsername
      : followBasePath() + '/follow/' + currentUsername;

    var res = await fetch(endpoint, { method: 'POST' });
    if (res.ok) {
      isFollowing = !isFollowing;
      updateFollowButton();
      showNotification(
        isFollowing ? 'Now following ' + currentUsername : 'Unfollowed ' + currentUsername,
        'success'
      );
    } else {
      showNotification('Failed to update follow status', 'error');
    }
  } catch (e) {
    console.error('Error toggling follow:', e);
    showNotification('Connection error', 'error');
  } finally {
    btn.disabled = false;
  }
}

// ============================================
// Track / Auto-record status
// ============================================
async function loadTrackStatus() {
  try {
    var res = await fetch('/api/models');
    if (!res.ok) return;
    var data = await res.json();
    var models = data.models || [];
    var found = null;
    for (var i = 0; i < models.length; i++) {
      if (models[i].username === currentUsername) {
        found = models[i];
        break;
      }
    }

    if (found) {
      isModelTracked = true;
      isAutoRecord = found.autoRecord;
    } else {
      isModelTracked = false;
      isAutoRecord = false;
    }
    updateRecordButton();
    document.getElementById('recordBtn').style.display = 'inline-flex';
  } catch (e) {
    console.error('Error loading track status:', e);
  }
}

function updateRecordButton() {
  var btn = document.getElementById('recordBtn');
  var icon = document.getElementById('recordIcon');
  var text = document.getElementById('recordText');

  if (isAutoRecord) {
    btn.classList.add('active');
    icon.innerHTML = '&#9679;';
    text.textContent = 'Recording On';
  } else {
    btn.classList.remove('active');
    icon.innerHTML = '&#9675;';
    text.textContent = 'Auto-Record';
  }
}

async function toggleAutoRecord() {
  var btn = document.getElementById('recordBtn');
  btn.disabled = true;

  try {
    // If not tracked, add model first
    if (!isModelTracked) {
      var addRes = await fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          username: currentUsername,
          autoRecord: true,
          recordQuality: 'best',
          retentionDays: 30
        })
      });
      if (addRes.ok || addRes.status === 409) {
        isModelTracked = true;
        isAutoRecord = true;
        updateRecordButton();
        showNotification('Auto-record enabled for ' + currentUsername, 'success');
        return;
      } else {
        showNotification('Failed to enable auto-record', 'error');
        return;
      }
    }

    // Toggle auto-record
    var newValue = !isAutoRecord;
    var res = await fetch('/api/models/' + currentUsername + '/auto-record', {
      method: 'PATCH',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ autoRecord: newValue })
    });

    if (res.ok) {
      isAutoRecord = newValue;
      updateRecordButton();
      showNotification(
        isAutoRecord ? 'Auto-record enabled' : 'Auto-record disabled',
        'success'
      );
    } else {
      showNotification('Failed to toggle auto-record', 'error');
    }
  } catch (e) {
    console.error('Error toggling auto-record:', e);
    showNotification('Connection error', 'error');
  } finally {
    btn.disabled = false;
  }
}

// ============================================
// Volume persistence (across sessions)
// ============================================
function setupVolumePersistence() {
  var video = document.getElementById('videoPlayer');
  if (!video) return;

  // Restore last volume/mute state
  var savedVolume = localStorage.getItem('video_volume_global');
  var savedMuted = localStorage.getItem('video_muted_global');
  if (savedVolume !== null) {
    var v = parseFloat(savedVolume);
    if (!isNaN(v) && v >= 0 && v <= 1) video.volume = v;
  }
  if (savedMuted !== null) {
    video.muted = savedMuted === 'true';
  }

  // Persist on change
  video.addEventListener('volumechange', function() {
    localStorage.setItem('video_volume_global', String(video.volume));
    localStorage.setItem('video_muted_global', String(video.muted));
  });
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
// Cleanup
// ============================================
window.addEventListener('beforeunload', function() {
  if (statusCheckInterval) clearInterval(statusCheckInterval);
  if (hlsPlayer) {
    hlsPlayer.destroy();
    hlsPlayer = null;
  }
});

// ============================================
// Initialization
// ============================================
window.addEventListener('DOMContentLoaded', function() {
  var style = document.createElement('style');
  style.textContent = '@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }';
  document.head.appendChild(style);

  setupVolumePersistence();
  initWatch();
});
