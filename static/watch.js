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
let profilePlaybackVolume = null;
let volumeSaveTimeout = null;
let hlsPlayer = null;
let streamLoaded = false;
let currentStreamUrl = '';
let statusCheckInterval = null;
let streamProblemStatusTimeout = null;

function sourceQuery() {
  return currentSourceType ? ('?source=' + encodeURIComponent(currentSourceType)) : '';
}

function escapeHtml(value) {
  return String(value || '')
    .replace(/&/g, '&amp;')
    .replace(/</g, '&lt;')
    .replace(/>/g, '&gt;')
    .replace(/"/g, '&quot;')
    .replace(/'/g, '&#39;');
}

function goBackFromWatch() {
  if (window.history.length > 1) {
    window.history.back();
    return;
  }

  window.location.href = '/';
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

  await loadProfileVolume();
  setupVolumePersistence();

  // loadModelStatus doit s'exécuter en premier: il résout currentSourceType,
  // dont dépendent loadFollowStatus et loadTrackStatus pour router vers le
  // bon backend (CAM4 vs Chaturbate).
  await loadModelStatus();
  await Promise.all([
    loadFollowStatus(),
    loadTrackStatus(),
  ]);

  // Start periodic status check
  statusCheckInterval = setInterval(loadModelStatus, 30000);
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

    var retryBtn = document.getElementById('retryBtn');
    var priv = isPrivateRoomStatus(data.roomStatus);
    if (data.sourceType) currentSourceType = data.sourceType;
    updatePlatformBadge(data.sourceType);

    if (priv) {
      statusDot.className = 'status-dot private';
      statusText.textContent = 'Private';
      viewerCount.style.display = 'none';
      renderWatchTags([]);
      if (offlineIcon) offlineIcon.innerHTML = '&#128274;';
      if (offlineTitle) offlineTitle.textContent = 'Model is in a Private Show';
      if (offlineText) offlineText.textContent = formatPrivateText(data.roomStatus);
      if (retryBtn) retryBtn.style.display = 'none';
      offlineOverlay.style.display = 'flex';
      stopStream();
      return;
    }

    if (data.isOnline) {
      statusDot.className = 'status-dot online';
      statusText.textContent = 'Live';
      viewerCount.style.display = 'inline';
      viewerNum.textContent = Number(data.viewers || 0).toLocaleString();
      renderWatchTags(data.tags || []);
      offlineOverlay.style.display = 'none';

      // Start stream if not already playing
      if (!hasActiveStream()) {
        startStream();
      }
    } else {
      if (hasActiveStream()) {
        // The status API can briefly report offline while the HLS stream is
        // still healthy. Keep the existing player alive instead of pausing it.
        statusDot.className = 'status-dot online';
        statusText.textContent = 'Live';
        viewerCount.style.display = 'inline';
        viewerNum.textContent = Number(data.viewers || 0).toLocaleString();
        if (data.tags && data.tags.length) renderWatchTags(data.tags);
        offlineOverlay.style.display = 'none';
        return;
      }

      // Status says offline, but try loading the stream anyway
      // The status API can return false negatives (rate limiting, cache miss)
      if (!hasActiveStream()) {
        var loaded = await tryLoadStream();
        if (loaded) {
          statusDot.className = 'status-dot online';
          statusText.textContent = 'Live';
          viewerCount.style.display = 'inline';
          viewerNum.textContent = Number(data.viewers || 0).toLocaleString();
          if (data.tags && data.tags.length) renderWatchTags(data.tags);
          offlineOverlay.style.display = 'none';
          return;
        }
      }

      statusDot.className = 'status-dot offline';
      statusText.textContent = 'Offline';
      viewerCount.style.display = 'none';
      renderWatchTags([]);
      if (offlineIcon) offlineIcon.innerHTML = '&#128308;';
      if (offlineTitle) offlineTitle.textContent = 'Model is Offline';
      if (offlineText) offlineText.textContent = 'This model is currently not streaming.';
      if (retryBtn) retryBtn.style.display = 'inline-flex';
      offlineOverlay.style.display = 'flex';

      // Stop stream if playing
      stopStream();
    }
  } catch (e) {
    console.error('Error loading model status:', e);
  }
}

function scheduleStatusRefreshAfterStreamProblem() {
  if (streamProblemStatusTimeout) return;
  streamProblemStatusTimeout = setTimeout(function() {
    streamProblemStatusTimeout = null;
    loadModelStatus();
  }, 250);
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

function renderWatchTags(tags) {
  var container = document.getElementById('watchTags');
  if (!container) return;

  var seen = new Set();
  var displayTags = [];
  if (Array.isArray(tags)) {
    tags.forEach(function(tag) {
      tag = String(tag || '').trim().replace(/^#/, '');
      var key = tag.toLowerCase();
      if (!tag || seen.has(key)) return;
      seen.add(key);
      displayTags.push(tag);
    });
  }

  if (displayTags.length === 0) {
    container.style.display = 'none';
    container.innerHTML = '';
    return;
  }

  container.innerHTML = displayTags.slice(0, 8).map(function(tag) {
    return '<span class="watch-tag">#' + escapeHtml(tag) + '</span>';
  }).join('');
  container.style.display = 'flex';
}

function applyLiveMetadata(data) {
  if (!data) return;
  var viewerCount = document.getElementById('viewerCount');
  var viewerNum = document.getElementById('viewerNum');
  if (data.sourceType) {
    currentSourceType = data.sourceType;
    updatePlatformBadge(data.sourceType);
  }
  if (viewerCount && viewerNum && data.viewers !== undefined) {
    viewerCount.style.display = 'inline';
    viewerNum.textContent = Number(data.viewers || 0).toLocaleString();
  }
  renderWatchTags(data.tags || []);
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
    applyLiveMetadata(data);
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

    applyLiveMetadata(data);
    startStreamWithUrl(streamUrl);
  } catch (e) {
    console.error('Error starting stream:', e);
  }
}

function startStreamWithUrl(streamUrl) {
  var video = document.getElementById('videoPlayer');
  if (!video) return;

  if (hasActiveStream() && currentStreamUrl === streamUrl) {
    return;
  }

  stopStream(false);
  currentStreamUrl = streamUrl;
  streamLoaded = true;
  prepareMutedAutoplay(video);

  if (isNativeVideoStream(streamUrl)) {
    resetQualitySelector();
    video.src = streamUrl;
    video.addEventListener('loadedmetadata', function() {
      startMutedAutoplay(video);
    }, { once: true });
    video.addEventListener('canplay', function() {
      startMutedAutoplay(video);
    }, { once: true });
    video.load();
    startMutedAutoplay(video);
  } else if (window.Hls && window.Hls.isSupported()) {
    hlsPlayer = new Hls({
      enableWorker: true,
      lowLatencyMode: false,
      backBufferLength: 90,
    });
    hlsPlayer.loadSource(streamUrl);
    hlsPlayer.attachMedia(video);
    hlsPlayer.on(Hls.Events.MANIFEST_PARSED, function() {
      updateQualitySelector();
      startMutedAutoplay(video);
    });
    hlsPlayer.on(Hls.Events.LEVEL_LOADED, function() {
      if (video.paused) startMutedAutoplay(video);
    });
    hlsPlayer.on(Hls.Events.ERROR, function(event, data) {
      if (data.fatal) {
        console.error('HLS fatal error: ' + [
          data.type,
          data.details || '',
          data.reason || '',
          data.error ? data.error.message : '',
          data.response ? data.response.code : '',
          data.url || ''
        ].filter(Boolean).join(' | '));
        scheduleStatusRefreshAfterStreamProblem();
        switch (data.type) {
          case Hls.ErrorTypes.NETWORK_ERROR:
            hlsPlayer.startLoad();
            break;
          case Hls.ErrorTypes.MEDIA_ERROR:
            hlsPlayer.recoverMediaError();
            break;
          default:
            stopStream(false);
            setTimeout(startStream, 5000);
            break;
        }
      }
    });
  } else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    // Safari native HLS
    resetQualitySelector();
    video.src = streamUrl;
    video.addEventListener('loadedmetadata', function() {
      startMutedAutoplay(video);
    }, { once: true });
    video.addEventListener('canplay', function() {
      startMutedAutoplay(video);
    }, { once: true });
    video.load();
    startMutedAutoplay(video);
  } else {
    console.error('HLS playback is not supported in this browser');
    stopStream(false);
    var offlineOverlay = document.getElementById('offlineOverlay');
    var offlineIcon = document.getElementById('offlineIcon');
    var offlineTitle = document.getElementById('offlineTitle');
    var offlineText = document.getElementById('offlineText');
    var retryBtn = document.getElementById('retryBtn');
    if (offlineIcon) offlineIcon.innerHTML = '&#9888;';
    if (offlineTitle) offlineTitle.textContent = 'Live player unavailable';
    if (offlineText) offlineText.textContent = 'This browser cannot load the HLS player. Check the network connection and retry.';
    if (retryBtn) retryBtn.style.display = 'inline-flex';
    if (offlineOverlay) offlineOverlay.style.display = 'flex';
  }
}

function isNativeVideoStream(streamUrl) {
  var url = String(streamUrl || '').toLowerCase();
  return url.indexOf('/streams/browser/') !== -1 || /\.webm(?:$|[?#])/.test(url);
}

function prepareMutedAutoplay(video) {
  video.autoplay = true;
  video.muted = true;
  video.defaultMuted = true;
  video.playsInline = true;
  video.setAttribute('autoplay', '');
  video.setAttribute('muted', '');
  video.setAttribute('playsinline', '');
  video.setAttribute('webkit-playsinline', '');
}

function startMutedAutoplay(video) {
  if (!video) return;

  prepareMutedAutoplay(video);

  var token = Date.now().toString(36) + Math.random().toString(36).slice(2);
  video.dataset.autoplayToken = token;

  var delays = [0, 200, 600, 1200, 2500];
  function attempt(index) {
    if (video.dataset.autoplayToken !== token) return;
    prepareMutedAutoplay(video);

    var promise = video.play();
    if (!promise || typeof promise.catch !== 'function') return;

    promise.catch(function(error) {
      if (video.dataset.autoplayToken !== token) return;
      if (index < delays.length - 1) {
        setTimeout(function() {
          attempt(index + 1);
        }, delays[index + 1]);
      } else {
        console.warn('Muted autoplay did not start:', error);
      }
    });
  }

  attempt(0);
}

function hasActiveStream() {
  return streamLoaded || !!hlsPlayer;
}

function stopStream(clearVideo) {
  var video = document.getElementById('videoPlayer');
  if (hlsPlayer) {
    hlsPlayer.destroy();
    hlsPlayer = null;
  }
  streamLoaded = false;
  currentStreamUrl = '';
  resetQualitySelector();

  if (clearVideo !== false && video) {
    video.removeAttribute('src');
    video.load();
  }
}

function resetQualitySelector() {
  var select = document.getElementById('watchQualitySelect');
  if (!select) return;
  select.innerHTML = '<option value="-1">Auto</option>';
  select.disabled = true;
}

function updateQualitySelector() {
  var select = document.getElementById('watchQualitySelect');
  if (!select || !hlsPlayer || !hlsPlayer.levels || !hlsPlayer.levels.length) {
    resetQualitySelector();
    return;
  }

  var currentValue = select.value || '-1';
  var html = '<option value="-1">Auto</option>';
  var levels = hlsPlayer.levels
    .map(function(level, index) {
      return { level: level, index: index };
    })
    .sort(function(a, b) {
      return (b.level.height || 0) - (a.level.height || 0);
    });

  levels.forEach(function(item) {
    html += '<option value="' + item.index + '">' + qualityLabel(item.level, item.index) + '</option>';
  });

  select.innerHTML = html;
  select.disabled = levels.length <= 1;
  select.value = optionExists(select, currentValue) ? currentValue : '-1';
}

function qualityLabel(level, index) {
  if (level.height) return level.height + 'p';
  if (level.bitrate) return Math.round(level.bitrate / 1000) + ' kbps';
  return 'Quality ' + (index + 1);
}

function optionExists(select, value) {
  for (var i = 0; i < select.options.length; i++) {
    if (select.options[i].value === value) return true;
  }
  return false;
}

function changeQuality(levelValue) {
  if (!hlsPlayer) return;
  var level = parseInt(levelValue, 10);
  hlsPlayer.currentLevel = Number.isNaN(level) ? -1 : level;
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
  return '/api/providers/' + encodeURIComponent(currentSourceType || 'chaturbate');
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
          sourceType: currentSourceType || 'chaturbate'
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
      body: JSON.stringify({
        autoRecord: newValue,
        sourceType: currentSourceType || 'chaturbate'
      })
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
function normalizeVolume(value) {
  if (value === null || value === undefined || value === '') return null;
  var volume = Number(value);
  if (!Number.isFinite(volume)) return null;
  return Math.min(1, Math.max(0, volume));
}

async function loadProfileVolume() {
  try {
    var res = await fetch('/api/models/' + encodeURIComponent(currentUsername) + '/volume');
    if (!res.ok) return;

    var data = await res.json();
    var saved = normalizeVolume(data.volume);
    if (saved !== null) {
      profilePlaybackVolume = saved;
      localStorage.setItem('video_volume_' + currentUsername, String(saved));
      return;
    }

    var profileVolume = getLocalVolume('video_volume_' + currentUsername);
    if (profileVolume !== null) {
      saveProfileVolume(profileVolume);
    }
  } catch (e) {
    console.warn('Could not load saved profile volume:', e);
  }
}

function persistProfileVolume(volume) {
  volumeSaveTimeout = null;
  fetch('/api/models/' + encodeURIComponent(currentUsername) + '/volume', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ volume: volume }),
    keepalive: true
  }).catch(function(e) {
    console.warn('Could not save profile volume:', e);
  });
}

function saveProfileVolume(volume) {
  var normalized = normalizeVolume(volume);
  if (normalized === null) return;

  profilePlaybackVolume = normalized;
  localStorage.setItem('video_volume_' + currentUsername, String(normalized));

  if (volumeSaveTimeout) {
    clearTimeout(volumeSaveTimeout);
  }
  volumeSaveTimeout = setTimeout(function() {
    persistProfileVolume(normalized);
  }, 250);
}

function getLocalVolume(key) {
  var saved = localStorage.getItem(key);
  return saved === null ? null : normalizeVolume(saved);
}

function getSavedProfileVolume() {
  if (profilePlaybackVolume !== null) return profilePlaybackVolume;

  var profileVolume = getLocalVolume('video_volume_' + currentUsername);
  if (profileVolume !== null) return profileVolume;

  var legacyGlobalVolume = getLocalVolume('video_volume_global');
  if (legacyGlobalVolume !== null) return legacyGlobalVolume;

  return 0.5;
}

function setupVolumePersistence() {
  var video = document.getElementById('videoPlayer');
  if (!video) return;

  video.volume = getSavedProfileVolume();

  if (video.dataset.volumePersistenceReady === 'true') return;
  video.dataset.volumePersistenceReady = 'true';

  // Persist on change
  video.addEventListener('volumechange', function() {
    if (!video.muted || video.volume === 0) {
      saveProfileVolume(video.volume);
    }
  });
}

function setupLiveControls() {
  var video = document.getElementById('videoPlayer');
  var container = document.querySelector('.watch-player-container');
  var playBtn = document.getElementById('livePlayBtn');
  var muteBtn = document.getElementById('liveMuteBtn');
  var fullscreenBtn = document.getElementById('liveFullscreenBtn');
  var volumeSlider = document.getElementById('liveVolumeSlider');
  var qualitySelect = document.getElementById('watchQualitySelect');
  if (!video) return;

  video.controls = false;
  video.playbackRate = 1;

  if (playBtn) {
    playBtn.addEventListener('click', function() {
      if (video.paused) {
        video.play().catch(function() {});
      } else {
        video.pause();
      }
    });
  }

  if (muteBtn) {
    muteBtn.addEventListener('click', function() {
      if (video.muted || video.volume === 0) {
        video.muted = false;
        if (video.volume === 0) video.volume = getSavedProfileVolume();
      } else {
        video.muted = true;
      }
    });
  }

  if (volumeSlider) {
    volumeSlider.value = String(video.volume);
    volumeSlider.addEventListener('input', function() {
      var volume = parseFloat(volumeSlider.value);
      if (Number.isNaN(volume)) return;
      video.volume = volume;
      video.muted = volume === 0;
      saveProfileVolume(volume);
    });
  }

  if (qualitySelect) {
    qualitySelect.addEventListener('change', function() {
      changeQuality(qualitySelect.value);
    });
  }

  if (fullscreenBtn && container) {
    fullscreenBtn.addEventListener('click', function() {
      if (document.fullscreenElement) {
        document.exitFullscreen().catch(function() {});
      } else if (container.requestFullscreen) {
        container.requestFullscreen().catch(function() {});
      }
    });
  }

  video.addEventListener('click', function(event) {
    if (event.target === video) {
      if (video.paused) {
        video.play().catch(function() {});
      } else {
        video.pause();
      }
    }
  });
  video.addEventListener('play', updateLiveControls);
  video.addEventListener('pause', updateLiveControls);
  video.addEventListener('volumechange', updateLiveControls);
  video.addEventListener('ratechange', function() {
    if (video.playbackRate !== 1) video.playbackRate = 1;
  });

  updateLiveControls();
  resetQualitySelector();
}

function updateLiveControls() {
  var video = document.getElementById('videoPlayer');
  var playIcon = document.getElementById('livePlayIcon');
  var muteIcon = document.getElementById('liveMuteIcon');
  var volumeSlider = document.getElementById('liveVolumeSlider');
  if (!video) return;

  if (playIcon) {
    playIcon.innerHTML = video.paused ? '&#9654;' : '&#10074;&#10074;';
  }
  if (muteIcon) {
    muteIcon.innerHTML = (video.muted || video.volume === 0) ? '&#128263;' : '&#128266;';
  }
  if (volumeSlider && document.activeElement !== volumeSlider) {
    volumeSlider.value = String(video.volume);
  }
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
  if (streamProblemStatusTimeout) clearTimeout(streamProblemStatusTimeout);
  if (volumeSaveTimeout && profilePlaybackVolume !== null) {
    clearTimeout(volumeSaveTimeout);
    persistProfileVolume(profilePlaybackVolume);
  }
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

  var video = document.getElementById('videoPlayer');
  if (video) {
    video.addEventListener('error', function() {
      streamLoaded = false;
      currentStreamUrl = '';
      scheduleStatusRefreshAfterStreamProblem();
    });
    video.addEventListener('ended', function() {
      streamLoaded = false;
      currentStreamUrl = '';
      scheduleStatusRefreshAfterStreamProblem();
    });
    video.addEventListener('stalled', function() {
      scheduleStatusRefreshAfterStreamProblem();
    });
  }

  setupLiveControls();
  initWatch();
});
