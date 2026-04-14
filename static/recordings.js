// ============================================
// Recordings Page - Model cards + recording list
// ============================================

// State
let allRecordings = {};
let currentDetailUser = '';
let currentPlayer = null;
let showTsFiles = false;
let currentPlayingRecordingId = '';
let currentPlayingUsername = '';
let currentPlayingFilename = '';

// ============================================
// Load recordings grouped by model
// ============================================
async function loadShowTsSetting() {
  try {
    var res = await fetch('/api/settings/recording');
    if (res.ok) {
      var data = await res.json();
      showTsFiles = !!data.show_ts_files;
    }
  } catch (e) {
    console.error('Error loading show_ts setting:', e);
  }
}

async function loadRecordingsByModel() {
  try {
    var res = await fetch('/api/recordings-by-model?show_ts=' + showTsFiles);
    if (res.ok) {
      var data = await res.json();
      return data.models || [];
    }
  } catch (e) {
    console.error('Error loading recordings by model:', e);
  }
  return [];
}

// ============================================
// Load all recordings for flat stats
// ============================================
async function loadAllRecordings() {
  try {
    var res = await fetch('/api/all-recordings?limit=10000&show_ts=' + showTsFiles);
    if (res.ok) {
      var data = await res.json();
      return data;
    }
  } catch (e) {
    console.error('Error loading all recordings:', e);
  }
  return { recordings: [], total: 0, totalSize: 0 };
}

// ============================================
// Format helpers
// ============================================
function formatSize(bytes) {
  if (!bytes || bytes === 0) return '0 B';
  var units = ['B', 'KB', 'MB', 'GB', 'TB'];
  var i = 0;
  var size = bytes;
  while (size >= 1024 && i < units.length - 1) { size /= 1024; i++; }
  return size.toFixed(i === 0 ? 0 : 1) + ' ' + units[i];
}

function formatDuration(seconds) {
  if (!seconds || seconds === 0) return '-';
  var h = Math.floor(seconds / 3600);
  var m = Math.floor((seconds % 3600) / 60);
  var s = Math.floor(seconds % 60);
  if (h > 0) return h + 'h ' + m + 'm';
  return m + 'm ' + s + 's';
}

function formatDate(timestamp) {
  if (!timestamp) return '-';
  try {
    var date = new Date(timestamp * 1000);
    return date.toLocaleDateString() + ' ' + date.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  } catch (e) {
    return '-';
  }
}

// ============================================
// Render model cards grid
// ============================================
function renderModelGrid(models) {
  var grid = document.getElementById('modelGrid');
  var emptyEl = document.getElementById('emptyRecordings');

  if (!models || models.length === 0) {
    grid.innerHTML = '';
    emptyEl.style.display = 'flex';
    return;
  }

  emptyEl.style.display = 'none';

  grid.innerHTML = models.map(function(model) {
    var thumbUrl = model.thumbnail || '/api/thumbnail/' + model.username;

    var countLabel = model.recordingCount + ' rec';
    if (model.recordingCount === 0) {
      countLabel = 'No recordings';
    }

    return '<div class="rec-model-card" onclick="showModelRecordings(\'' + escapeHtml(model.username) + '\')">' +
      '<div class="rec-model-card-thumb">' +
        '<img src="' + escapeHtml(thumbUrl) + '" alt="' + escapeHtml(model.username) + '" ' +
          'onerror="this.src=\'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22280%22 height=%22200%22%3E%3Crect fill=%22%231a1f3a%22 width=%22280%22 height=%22200%22/%3E%3Ctext x=%2250%25%22 y=%2250%25%22 dominant-baseline=%22middle%22 text-anchor=%22middle%22 fill=%22%23a0aec0%22 font-family=%22system-ui%22 font-size=%2216%22%3E' + escapeHtml(model.username) + '%3C/text%3E%3C/svg%3E\'" loading="lazy" />' +
        '<span class="rec-model-count">' + countLabel + '</span>' +
      '</div>' +
      '<div class="rec-model-card-info">' +
        '<span class="rec-model-username">' + escapeHtml(model.username) + '</span>' +
        '<span class="rec-model-size">' + formatSize(model.totalSize) + '</span>' +
      '</div>' +
    '</div>';
  }).join('');
}

// ============================================
// Show recordings for a specific model
// ============================================
async function showModelRecordings(username) {
  currentDetailUser = username;

  document.getElementById('modelGrid').style.display = 'none';
  document.getElementById('recordingsDetail').style.display = 'block';
  document.getElementById('detailUsername').textContent = username;

  // Load auto-record status for the detail toggle button
  loadDetailRecordStatus(username);

  var list = document.getElementById('recordingsList');
  list.innerHTML = '<div class="empty-message"><div class="icon">&#9203;</div><p>Loading...</p></div>';

  try {
    var res = await fetch('/api/recordings/' + encodeURIComponent(username) + '?show_ts=' + showTsFiles);
    if (!res.ok) {
      list.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Failed to load recordings.</p></div>';
      return;
    }

    var data = await res.json();
    var recordings = data.recordings || [];

    document.getElementById('detailCount').textContent = recordings.length + ' recording' + (recordings.length !== 1 ? 's' : '');

    if (recordings.length === 0) {
      list.innerHTML = '<div class="empty-message"><div class="icon">&#127910;</div><p>No recordings found.</p></div>';
      return;
    }

    // Sort newest first (already sorted by API, but ensure)
    recordings.sort(function(a, b) {
      return (b.createdAt || b.date || 0) - (a.createdAt || a.date || 0);
    });

    // Load playback positions
    var positions = {};
    for (var i = 0; i < recordings.length; i++) {
      var recId = recordings[i].recordingId;
      if (recId) {
        try {
          var posRes = await fetch('/api/playback-position/' + encodeURIComponent(recId));
          if (posRes.ok) {
            var posData = await posRes.json();
            if (posData.position > 0) {
              positions[recId] = posData;
            }
          }
        } catch (e) {}
      }
    }

    list.innerHTML = recordings.map(function(rec) {
      var thumbUrl = rec.thumbnail || '/api/thumbnail/' + username;
      var recId = rec.recordingId || '';
      var pos = positions[recId];
      var resumeBadge = '';
      if (pos && pos.position > 0 && pos.duration > 0) {
        var pct = Math.round((pos.position / pos.duration) * 100);
        resumeBadge = '<div class="resume-badge">Resume at ' + formatDuration(pos.position) + ' (' + pct + '%)</div>';
        resumeBadge += '<div class="progress-bar"><div class="progress-fill" style="width:' + pct + '%"></div></div>';
      }

      return '<div class="recording-item" onclick="playRecording(\'' + escapeHtml(username) + '\', \'' + escapeHtml(rec.filename) + '\', \'' + escapeHtml(recId) + '\')">' +
        '<div class="recording-item-thumb">' +
          '<img src="' + escapeHtml(thumbUrl) + '" alt="" loading="lazy" ' +
            'onerror="this.style.display=\'none\'" />' +
          '<span class="recording-duration">' + (rec.duration_str || formatDuration(rec.duration)) + '</span>' +
        '</div>' +
        '<div class="recording-item-info">' +
          '<div class="recording-date">' + formatDate(rec.createdAt) + '</div>' +
          '<div class="recording-meta">' +
            '<span>' + (rec.size_display || formatSize(rec.size)) + '</span>' +
          '</div>' +
          resumeBadge +
        '</div>' +
        '<div class="recording-item-actions">' +
          '<button class="rec-action-btn" onclick="event.stopPropagation(); downloadRecording(\'' + escapeHtml(username) + '\', \'' + escapeHtml(rec.filename) + '\')" title="Download">&#11015;</button>' +
          '<button class="rec-action-btn danger" onclick="event.stopPropagation(); deleteRecording(\'' + escapeHtml(username) + '\', \'' + escapeHtml(rec.filename) + '\', this)" title="Delete">&#128465;</button>' +
        '</div>' +
      '</div>';
    }).join('');

  } catch (e) {
    console.error('Error loading recordings:', e);
    list.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Error loading recordings.</p></div>';
  }
}

// ============================================
// Show model grid (back button)
// ============================================
function showModelGrid() {
  document.getElementById('modelGrid').style.display = 'grid';
  document.getElementById('recordingsDetail').style.display = 'none';
  currentDetailUser = '';
}

// ============================================
// Play recording with resume support
// ============================================
async function playRecording(username, filename, recordingId) {
  var modal = document.getElementById('playerModal');
  var video = document.getElementById('recordingPlayer');
  var title = document.getElementById('playerTitle');

  // Track current playing recording for auto-delete
  currentPlayingRecordingId = recordingId;
  currentPlayingUsername = username;
  currentPlayingFilename = filename;

  title.textContent = username + ' - ' + filename;
  modal.style.display = 'flex';

  var url = '/streams/records/' + encodeURIComponent(username) + '/' + encodeURIComponent(filename);

  // Clean up previous player
  if (currentPlayer) {
    currentPlayer.destroy();
    currentPlayer = null;
  }
  video.removeAttribute('src');

  // TS files are raw MPEG-TS, not HLS streams - use direct playback
  video.src = url;
  video.onloadedmetadata = function() {
    loadAndSeek(video, recordingId, username);
  };
  // Fallback: if native playback fails for TS, try with type hint
  video.onerror = function() {
    if (filename.endsWith('.ts') && !video.dataset.retried) {
      video.dataset.retried = 'true';
      var source = document.createElement('source');
      source.src = url;
      source.type = 'video/mp2t';
      video.removeAttribute('src');
      video.appendChild(source);
      video.load();
      video.onloadedmetadata = function() {
        loadAndSeek(video, recordingId, username);
      };
    }
  };

  // Save position periodically
  var saveInterval = setInterval(function() {
    if (video.currentTime > 0 && !video.paused && recordingId) {
      savePosition(recordingId, username, video.currentTime, video.duration);
    }
  }, 5000);

  // Save on pause
  video.onpause = function() {
    if (recordingId && video.currentTime > 0) {
      savePosition(recordingId, username, video.currentTime, video.duration);
    }
  };

  // Clean up interval when modal closes
  modal.dataset.saveInterval = saveInterval;
}

async function loadAndSeek(video, recordingId, username) {
  if (!recordingId) { video.play().catch(function(){}); return; }
  try {
    var res = await fetch('/api/playback-position/' + encodeURIComponent(recordingId));
    if (res.ok) {
      var data = await res.json();
      if (data.position > 5) {
        video.currentTime = data.position;
      }
    }
  } catch (e) {}
  video.play().catch(function(){});
}

function savePosition(recordingId, username, position, duration) {
  fetch('/api/playback-position/' + encodeURIComponent(recordingId), {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ position: position, duration: duration, username: username })
  }).catch(function(){});
}

async function closePlayer() {
  var modal = document.getElementById('playerModal');
  var video = document.getElementById('recordingPlayer');

  // Save final position and check auto-delete
  var shouldAutoDelete = false;
  if (currentPlayingRecordingId && video.currentTime > 0 && video.duration > 0) {
    try {
      var res = await fetch('/api/playback-position/' + encodeURIComponent(currentPlayingRecordingId), {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({
          position: video.currentTime,
          duration: video.duration,
          username: currentPlayingUsername
        })
      });
      if (res.ok) {
        var data = await res.json();
        shouldAutoDelete = !!data.autoDelete;
      }
    } catch (e) {}
  }

  video.pause();

  if (currentPlayer) {
    currentPlayer.destroy();
    currentPlayer = null;
  }
  video.removeAttribute('src');
  delete video.dataset.retried;
  // Remove any <source> elements added for TS fallback
  while (video.firstChild) { video.removeChild(video.firstChild); }
  video.load();

  var interval = modal.dataset.saveInterval;
  if (interval) clearInterval(Number(interval));

  modal.style.display = 'none';

  // Auto-delete if threshold was reached
  if (shouldAutoDelete && currentPlayingUsername && currentPlayingFilename) {
    showNotification('Auto-deleting watched recording...', 'success');
    try {
      var delRes = await fetch('/api/recordings/' + encodeURIComponent(currentPlayingUsername) + '/' + encodeURIComponent(currentPlayingFilename), {
        method: 'DELETE'
      });
      if (delRes.ok) {
        showNotification('Recording auto-deleted', 'success');
      }
    } catch (e) {}
    // Refresh view
    if (currentDetailUser) {
      showModelRecordings(currentDetailUser);
    }
  }

  // Reset tracking
  currentPlayingRecordingId = '';
  currentPlayingUsername = '';
  currentPlayingFilename = '';
}

// ============================================
// Download recording
// ============================================
function downloadRecording(username, filename) {
  var url = '/streams/records/' + encodeURIComponent(username) + '/' + encodeURIComponent(filename);
  var a = document.createElement('a');
  a.href = url;
  a.download = filename;
  document.body.appendChild(a);
  a.click();
  document.body.removeChild(a);
}

// ============================================
// Delete recording
// ============================================
async function deleteRecording(username, filename, btn) {
  if (!confirm('Delete recording "' + filename + '"? This cannot be undone.')) return;

  btn.disabled = true;
  btn.textContent = '...';

  try {
    var res = await fetch('/api/recordings/' + encodeURIComponent(username) + '/' + encodeURIComponent(filename), {
      method: 'DELETE'
    });

    if (res.ok) {
      showNotification('Recording deleted', 'success');
      // Reload detail view
      if (currentDetailUser) {
        showModelRecordings(currentDetailUser);
      }
    } else {
      showNotification('Failed to delete recording', 'error');
      btn.disabled = false;
      btn.innerHTML = '&#128465;';
    }
  } catch (e) {
    console.error('Error deleting recording:', e);
    showNotification('Connection error', 'error');
    btn.disabled = false;
    btn.innerHTML = '&#128465;';
  }
}

// ============================================
// Toggle auto-record from detail view
// ============================================
async function toggleDetailAutoRecord() {
  if (!currentDetailUser) return;

  var btn = document.getElementById('detailRecordBtn');
  btn.disabled = true;

  try {
    // Check if model is tracked
    var modelsRes = await fetch('/api/models');
    var modelsData = modelsRes.ok ? await modelsRes.json() : { models: [] };
    var found = null;
    for (var i = 0; i < (modelsData.models || []).length; i++) {
      if (modelsData.models[i].username === currentDetailUser) {
        found = modelsData.models[i];
        break;
      }
    }

    if (!found) {
      // Not tracked yet - add model with auto-record on
      var addRes = await fetch('/api/models', {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ username: currentDetailUser, autoRecord: true, recordQuality: 'best', retentionDays: 30 })
      });
      if (addRes.ok || addRes.status === 409) {
        updateDetailRecordButton(true);
        showNotification('Auto-record enabled for ' + currentDetailUser, 'success');
      } else {
        showNotification('Failed to enable auto-record', 'error');
      }
    } else {
      // Toggle existing
      var newValue = !found.autoRecord;
      var res = await fetch('/api/models/' + encodeURIComponent(currentDetailUser) + '/auto-record', {
        method: 'PATCH',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ autoRecord: newValue })
      });
      if (res.ok) {
        updateDetailRecordButton(newValue);
        showNotification(newValue ? 'Auto-record enabled' : 'Auto-record disabled', 'success');
      } else {
        showNotification('Failed to toggle auto-record', 'error');
      }
    }
  } catch (e) {
    console.error('Error toggling auto-record:', e);
    showNotification('Connection error', 'error');
  } finally {
    btn.disabled = false;
  }
}

function updateDetailRecordButton(isActive) {
  var btn = document.getElementById('detailRecordBtn');
  var icon = document.getElementById('detailRecordIcon');
  var text = document.getElementById('detailRecordText');

  if (isActive) {
    btn.classList.add('active');
    icon.innerHTML = '&#9679;';
    text.textContent = 'Recording On';
  } else {
    btn.classList.remove('active');
    icon.innerHTML = '&#9675;';
    text.textContent = 'Auto-Record';
  }
}

async function loadDetailRecordStatus(username) {
  try {
    var res = await fetch('/api/models');
    if (!res.ok) return;
    var data = await res.json();
    var found = null;
    for (var i = 0; i < (data.models || []).length; i++) {
      if (data.models[i].username === username) {
        found = data.models[i];
        break;
      }
    }
    updateDetailRecordButton(found ? found.autoRecord : false);
  } catch (e) {
    console.error('Error loading record status:', e);
  }
}

// ============================================
// Helpers
// ============================================
function escapeHtml(text) {
  if (!text) return '';
  var div = document.createElement('div');
  div.appendChild(document.createTextNode(text));
  return div.innerHTML;
}

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
  var style = document.createElement('style');
  style.textContent = '@keyframes slideIn { from { transform: translateX(100%); opacity: 0; } to { transform: translateX(0); opacity: 1; } }';
  document.head.appendChild(style);

  var loadingState = document.getElementById('loadingState');

  // Load show_ts setting first, then load recordings
  loadShowTsSetting().then(function() {
    return Promise.all([loadRecordingsByModel(), loadAllRecordings()]);
  }).then(function(results) {
    var models = results[0];
    var allData = results[1];

    loadingState.style.display = 'none';

    // Update stats
    document.getElementById('totalRecordings').textContent = allData.total || 0;
    document.getElementById('totalSize').textContent = allData.totalSizeFormatted || formatSize(allData.totalSize || 0);
    document.getElementById('totalModels').textContent = models.length;

    // Render model cards
    renderModelGrid(models);
  }).catch(function(e) {
    console.error('Error initializing recordings page:', e);
    loadingState.innerHTML = '<div class="icon">&#9888;</div><p>Failed to load recordings.</p>';
  });
});
