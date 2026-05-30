// ============================================
// Media Page - profile-first media catalogue
// ============================================

(function() {
  'use strict';

  var state = {
    items: [],
    profiles: [],
    kind: 'video',
    profile: '',
    search: '',
    sort: 'newest',
    loading: false,
    pendingDelete: null,
    pendingProfileDelete: null,
    currentViewerItem: null,
    profileSettings: null
  };

  var searchTimer = null;

  function $(id) {
    return document.getElementById(id);
  }

  function escapeHtml(value) {
    return String(value == null ? '' : value)
      .replace(/&/g, '&amp;')
      .replace(/</g, '&lt;')
      .replace(/>/g, '&gt;')
      .replace(/"/g, '&quot;')
      .replace(/'/g, '&#039;');
  }

  function formatDate(timestamp) {
    if (!timestamp) return '-';
    try {
      return new Date(timestamp * 1000).toLocaleString([], {
        year: 'numeric',
        month: 'short',
        day: 'numeric',
        hour: '2-digit',
        minute: '2-digit'
      });
    } catch (e) {
      return '-';
    }
  }

  function formatType(item) {
    if (item.type === 'image') return 'Photo';
    if (item.type === 'audio') return 'Audio';
    return 'Video';
  }

  function itemById(id) {
    for (var i = 0; i < state.items.length; i++) {
      if (state.items[i].id === id) return state.items[i];
    }
    return null;
  }

  function profileByUsername(username) {
    for (var i = 0; i < state.profiles.length; i++) {
      if (state.profiles[i].username === username) return state.profiles[i];
    }
    return null;
  }

  function profileLabel(profile) {
    if (!profile) return state.profile || '';
    return profile.displayName || profile.username || '';
  }

  function firstLetter(value) {
    value = String(value || '?').trim();
    return (value.charAt(0) || '?').toUpperCase();
  }

  function splitLines(value) {
    return String(value || '')
      .split(/\r?\n/)
      .map(function(line) { return line.trim(); })
      .filter(Boolean);
  }

  function joinLines(value) {
    if (Array.isArray(value)) return value.join('\n');
    return String(value || '');
  }

  function buildQuery() {
    var params = new URLSearchParams();
    params.set('kind', state.kind);
    params.set('sort', state.sort);
    params.set('limit', '1000');
    if (state.profile) params.set('username', state.profile);
    if (state.search) params.set('search', state.search);
    return params.toString();
  }

  async function loadMediaLibrary() {
    if (state.loading) return;
    state.loading = true;
    renderLoading();

    try {
      var res = await fetch('/api/media-library?' + buildQuery(), { cache: 'no-store' });
      if (!res.ok) throw new Error('Failed to load media library');
      var data = await res.json();
      state.items = data.items || [];
      state.profiles = data.profiles || [];
      renderStats(data.libraryStats || data.stats || {});
      renderProfileCarousel();
      renderProfileDetail();
      renderRecentSection(data.total || state.items.length);
    } catch (e) {
      console.error('Error loading media library:', e);
      renderError();
    } finally {
      state.loading = false;
    }
  }

  function renderLoading() {
    var grid = $('mediaGrid');
    var meta = $('mediaResultMeta');
    var rail = $('mediaProfileRail');
    if (meta) meta.textContent = 'Loading...';
    if (!state.profiles.length && rail) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#9203;</div><p>Loading profiles...</p></div>';
    }
    if (grid) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#9203;</div><p>Loading media...</p></div>';
    }
  }

  function renderError() {
    var grid = $('mediaGrid');
    var rail = $('mediaProfileRail');
    var meta = $('mediaResultMeta');
    if (meta) meta.textContent = 'Unable to load media';
    if (rail && !state.profiles.length) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Profiles unavailable</p></div>';
    }
    if (grid) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Media unavailable</p></div>';
    }
  }

  function renderStats(stats) {
    $('mediaTotalCount').textContent = stats.total || 0;
    $('mediaVideoCount').textContent = stats.videos || 0;
    $('mediaImageCount').textContent = stats.images || 0;
    $('mediaTotalSize').textContent = stats.totalSizeFormatted || '0 B';
  }

  function renderProfileCarousel() {
    var rail = $('mediaProfileRail');
    var meta = $('mediaProfileMeta');
    if (!rail) return;

    if (meta) {
      var count = state.profiles.length;
      meta.textContent = count === 1 ? '1 profil' : count + ' profils';
    }

    if (!state.profiles.length) {
      rail.innerHTML = '<div class="empty-message"><div class="icon">&#128444;</div><p>No profiles found</p></div>';
      return;
    }

    rail.innerHTML = state.profiles.map(renderProfileCard).join('');
  }

  function renderProfileCard(profile) {
    var active = state.profile === profile.username;
    var name = profileLabel(profile);
    var image = '';
    if (profile.thumbnail) {
      image = '<img src="' + escapeHtml(profile.thumbnail) + '" alt="' + escapeHtml(name) + '" loading="lazy" onerror="this.style.display=\'none\'; this.parentElement.classList.add(\'missing-thumb\');">';
    }

    var countLabel = (profile.videos || 0) + ' videos';
    if (profile.images) countLabel += ' / ' + profile.images + ' photos';

    return '' +
      '<button class="media-profile-card' + (active ? ' active' : '') + '" type="button" data-profile="' + escapeHtml(profile.username) + '">' +
        '<div class="media-profile-poster">' +
          image +
          '<div class="media-profile-placeholder"><span>' + escapeHtml(firstLetter(name)) + '</span></div>' +
          '<span class="media-profile-total">' + escapeHtml(String(profile.total || 0)) + '</span>' +
        '</div>' +
        '<div class="media-profile-info">' +
          '<div class="media-profile-name">' + escapeHtml(name) + '</div>' +
          (name !== profile.username ? '<div class="media-profile-handle">' + escapeHtml(profile.username) + '</div>' : '') +
          '<div class="media-profile-counts">' + escapeHtml(countLabel) + '</div>' +
        '</div>' +
      '</button>';
  }

  function renderProfileDetail() {
    var detail = $('mediaProfileDetail');
    if (!detail) return;
    if (!state.profile) {
      detail.hidden = true;
      return;
    }

    var profile = profileByUsername(state.profile);
    var name = profileLabel(profile);
    var avatar = $('mediaProfileAvatar');
    var title = $('mediaProfileDetailName');
    var meta = $('mediaProfileDetailMeta');
    if (avatar) avatar.textContent = firstLetter(name);
    if (title) title.textContent = name || state.profile;
    if (meta) {
      var parts = [];
      if (profile && profile.username && profile.displayName) parts.push(profile.username);
      parts.push(((profile && profile.videos) || 0) + ' videos');
      parts.push(((profile && profile.images) || 0) + ' photos');
      if (profile && profile.country) parts.push(profile.country);
      if (profile && profile.recordQuality) parts.push('Qualite ' + profile.recordQuality);
      if (profile && profile.retentionDays != null) parts.push(profile.retentionDays + ' jours');
      meta.textContent = parts.join(' / ');
    }
    detail.hidden = false;
  }

  function renderRecentSection(total) {
    renderRecentTitle();
    renderGrid(total);
    syncFilterControls();
  }

  function renderRecentTitle() {
    var title = $('mediaRecentTitle');
    var meta = $('mediaResultMeta');
    var clearBtn = $('mediaClearProfileBtn');
    if (title) {
      var base = state.kind === 'image' ? 'Photos recentes' : state.kind === 'all' ? 'Medias recents' : 'Videos recentes';
      title.textContent = state.profile ? base + ' / ' + profileLabel(profileByUsername(state.profile)) : base;
    }
    if (clearBtn) {
      clearBtn.hidden = !state.profile;
      clearBtn.textContent = state.profile ? 'All profiles' : 'All profiles';
    }
    if (meta) {
      var label = state.items.length === 1 ? '1 media file' : state.items.length + ' media files';
      if (state.search) label += ' matching search';
      meta.textContent = label;
    }
  }

  function renderGrid(total) {
    var grid = $('mediaGrid');
    var meta = $('mediaResultMeta');
    if (!grid) return;

    if (meta) {
      var label = total === 1 ? '1 media file' : total + ' media files';
      if (state.profile) label += ' in ' + state.profile;
      if (state.search) label += ' matching search';
      meta.textContent = label;
    }

    if (!state.items.length) {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#128444;</div><p>No media found</p></div>';
      return;
    }

    grid.innerHTML = state.items.map(renderCard).join('');
  }

  function renderCard(item) {
    var thumb = '';
    if (item.thumbnail) {
      thumb = '<img src="' + escapeHtml(item.thumbnail) + '" alt="' + escapeHtml(item.title || item.filename) + '" loading="lazy" onerror="this.style.display=\'none\'; this.parentElement.classList.add(\'missing-thumb\');">';
    }

    var marker = item.type === 'image' ? '&#128247;' : item.type === 'audio' ? '&#9835;' : '&#9654;';
    var badges = [
      '<span>' + formatType(item) + '</span>',
      '<span>' + escapeHtml((item.extension || '').toUpperCase()) + '</span>'
    ];
    if (item.isImported) badges.push('<span>Imported</span>');
    if (item.isRecording) badges.push('<span>Recording</span>');
    if (item.type === 'video' && !item.browserPlayable) badges.push('<span>Original</span>');

    return '' +
      '<article class="media-card" role="button" tabindex="0" data-media-id="' + escapeHtml(item.id) + '">' +
        '<div class="media-card-thumb">' +
          thumb +
          '<div class="media-card-placeholder"><span aria-hidden="true">' + marker + '</span></div>' +
          (item.durationStr ? '<span class="media-duration">' + escapeHtml(item.durationStr) + '</span>' : '') +
        '</div>' +
        '<div class="media-card-body">' +
          '<div class="media-card-title" title="' + escapeHtml(item.filename) + '">' + escapeHtml(item.title || item.filename) + '</div>' +
          '<div class="media-card-subtitle">' + escapeHtml(item.username) + '</div>' +
          '<div class="media-card-meta">' +
            '<span>' + escapeHtml(item.sizeFormatted || '') + '</span>' +
            '<span>' + escapeHtml(formatDate(item.createdAt)) + '</span>' +
          '</div>' +
          '<div class="media-badges">' + badges.join('') + '</div>' +
          '<div class="media-card-actions">' +
            '<button class="media-icon-btn danger" type="button" title="Delete" aria-label="Delete" data-media-action="delete" data-media-id="' + escapeHtml(item.id) + '">&#128465;</button>' +
          '</div>' +
        '</div>' +
      '</article>';
  }

  function openViewer(item) {
    if (!item) return;

    var viewer = $('mediaViewer');
    var stage = $('mediaViewerStage');
    var title = $('mediaViewerTitle');
    var meta = $('mediaViewerMeta');
    var deleteBtn = $('mediaViewerDelete');
    if (!viewer || !stage) return;

    state.currentViewerItem = item;
    title.textContent = item.title || item.filename;
    meta.textContent = item.username + ' / ' + formatType(item) + ' / ' + (item.sizeFormatted || '') + ' / ' + formatDate(item.createdAt);
    if (deleteBtn) deleteBtn.dataset.mediaId = item.id;

    stage.innerHTML = '';
    var mediaNode;
    if (item.type === 'image') {
      mediaNode = document.createElement('img');
      mediaNode.src = item.url;
      mediaNode.alt = item.title || item.filename;
      stage.appendChild(mediaNode);
    } else if (item.type === 'audio') {
      mediaNode = document.createElement('audio');
      mediaNode.src = item.url;
      mediaNode.controls = true;
      mediaNode.autoplay = true;
      stage.appendChild(mediaNode);
    } else {
      mediaNode = document.createElement('video');
      mediaNode.src = item.url;
      mediaNode.controls = true;
      mediaNode.autoplay = true;
      mediaNode.playsInline = true;
      stage.appendChild(mediaNode);
      if (!item.browserPlayable) {
        var note = document.createElement('div');
        note.className = 'media-viewer-note';
        note.textContent = 'Format original. Le navigateur peut ne pas le lire directement.';
        stage.appendChild(note);
      }
    }

    viewer.style.display = 'flex';
    viewer.setAttribute('aria-hidden', 'false');
    document.body.classList.add('media-viewer-open');
  }

  function closeViewer() {
    var viewer = $('mediaViewer');
    var stage = $('mediaViewerStage');
    if (!viewer || !stage) return;

    var active = stage.querySelector('video, audio');
    if (active) active.pause();
    stage.innerHTML = '';
    state.currentViewerItem = null;
    viewer.style.display = 'none';
    viewer.setAttribute('aria-hidden', 'true');
    document.body.classList.remove('media-viewer-open');
  }

  function showToast(message, type) {
    if (typeof window.showNotification === 'function') {
      window.showNotification(message, type || 'success');
      return;
    }
    var existing = document.querySelector('.media-toast');
    if (existing) existing.remove();
    var toast = document.createElement('div');
    toast.className = 'media-toast ' + (type || 'success');
    toast.textContent = message;
    document.body.appendChild(toast);
    setTimeout(function() {
      toast.remove();
    }, 2600);
  }

  function openDeleteConfirm(item) {
    if (!item) return;
    state.pendingDelete = item;
    var modal = $('mediaDeleteModal');
    var target = $('mediaDeleteTarget');
    var confirm = $('mediaDeleteConfirm');
    if (target) {
      target.textContent = (item.title || item.filename) + ' / ' + item.username;
    }
    if (confirm) {
      confirm.disabled = false;
      confirm.textContent = 'Supprimer';
    }
    if (modal) {
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('media-delete-open');
    }
  }

  function closeDeleteConfirm() {
    var modal = $('mediaDeleteModal');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-delete-open');
    }
    state.pendingDelete = null;
  }

  async function confirmDeleteMedia() {
    var item = state.pendingDelete;
    if (!item) return;
    var confirm = $('mediaDeleteConfirm');
    if (confirm) {
      confirm.disabled = true;
      confirm.textContent = 'Suppression...';
    }

    try {
      var res = await fetch(item.deleteUrl, { method: 'DELETE' });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok || data.success === false) {
        throw new Error(data.detail || data.message || 'Delete failed');
      }

      if (state.currentViewerItem && state.currentViewerItem.id === item.id) {
        closeViewer();
      }
      closeDeleteConfirm();
      showToast('Media supprime', 'success');
      await loadMediaLibrary();
    } catch (e) {
      console.error('Error deleting media:', e);
      showToast(e.message || 'Delete failed', 'error');
      if (confirm) {
        confirm.disabled = false;
        confirm.textContent = 'Supprimer';
      }
    }
  }

  function ensureSelectOption(select, value) {
    if (!select || !value) return;
    for (var i = 0; i < select.options.length; i++) {
      if (select.options[i].value === value) return;
    }
    var option = document.createElement('option');
    option.value = value;
    option.textContent = value;
    select.appendChild(option);
  }

  function setField(id, value) {
    var field = $(id);
    if (!field) return;
    field.value = value == null ? '' : value;
  }

  function fieldValue(id) {
    var field = $(id);
    return field ? field.value.trim() : '';
  }

  function fillProfileSettings(profile) {
    state.profileSettings = profile;
    var subtitle = $('mediaProfileSettingsSubtitle');
    if (subtitle) subtitle.textContent = profile.username;

    setField('profileDisplayName', profile.displayName || '');
    setField('profileFirstName', profile.firstName || '');
    setField('profileLastName', profile.lastName || '');
    setField('profileAge', profile.age == null ? '' : profile.age);
    setField('profileAliases', profile.aliases || '');
    setField('profileTags', profile.tags || '');
    setField('profileAddress', profile.address || '');
    setField('profileCity', profile.city || '');
    setField('profileRegion', profile.region || '');
    setField('profilePostalCode', profile.postalCode || '');
    setField('profileCountry', profile.country || '');
    setField('profileSocialUrls', joinLines(profile.socialUrls));
    setField('profileStreamUrls', joinLines(profile.streamUrls));
    setField('profileProfileUrls', joinLines(profile.profileUrls));
    setField('profileNotes', profile.notes || '');
    var quality = $('profileRecordQuality');
    ensureSelectOption(quality, profile.recordQuality || 'best');
    if (quality) quality.value = profile.recordQuality || 'best';
    setField('profileRetentionDays', profile.retentionDays == null ? 30 : profile.retentionDays);
    var source = $('profileSourceType');
    ensureSelectOption(source, profile.sourceType || profile.source_type || 'chaturbate');
    if (source) source.value = profile.sourceType || profile.source_type || 'chaturbate';
    var auto = $('profileAutoRecord');
    if (auto) auto.checked = !!profile.autoRecord;
  }

  async function openProfileSettings() {
    if (!state.profile) return;
    var modal = $('mediaProfileSettingsModal');
    var save = $('mediaProfileSettingsSave');
    if (save) save.disabled = true;

    try {
      var res = await fetch('/api/media-profiles/' + encodeURIComponent(state.profile), { cache: 'no-store' });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok) throw new Error(data.detail || 'Profile unavailable');
      fillProfileSettings(data);
      if (modal) {
        modal.style.display = 'flex';
        modal.setAttribute('aria-hidden', 'false');
        document.body.classList.add('media-profile-settings-open');
      }
    } catch (e) {
      console.error('Error loading profile settings:', e);
      showToast(e.message || 'Profile unavailable', 'error');
    } finally {
      if (save) save.disabled = false;
    }
  }

  function closeProfileSettings() {
    var modal = $('mediaProfileSettingsModal');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-profile-settings-open');
    }
  }

  async function saveProfileSettings(ev) {
    if (ev) ev.preventDefault();
    if (!state.profile) return;
    var save = $('mediaProfileSettingsSave');
    if (save) {
      save.disabled = true;
      save.textContent = 'Enregistrement...';
    }

    var retention = parseInt(fieldValue('profileRetentionDays'), 10);
    if (Number.isNaN(retention)) retention = 30;
    retention = Math.max(0, Math.min(365, retention));

    var ageValue = fieldValue('profileAge');
    var age = ageValue ? parseInt(ageValue, 10) : null;
    if (Number.isNaN(age)) age = null;

    var auto = $('profileAutoRecord');
    var source = $('profileSourceType');
    var payload = {
      displayName: fieldValue('profileDisplayName'),
      firstName: fieldValue('profileFirstName'),
      lastName: fieldValue('profileLastName'),
      age: age,
      aliases: fieldValue('profileAliases'),
      tags: fieldValue('profileTags'),
      address: fieldValue('profileAddress'),
      city: fieldValue('profileCity'),
      region: fieldValue('profileRegion'),
      postalCode: fieldValue('profilePostalCode'),
      country: fieldValue('profileCountry'),
      socialUrls: splitLines(fieldValue('profileSocialUrls')),
      streamUrls: splitLines(fieldValue('profileStreamUrls')),
      profileUrls: splitLines(fieldValue('profileProfileUrls')),
      notes: fieldValue('profileNotes'),
      recordQuality: fieldValue('profileRecordQuality') || 'best',
      retentionDays: retention,
      sourceType: source ? source.value : 'chaturbate',
      autoRecord: auto ? auto.checked : false
    };

    try {
      var res = await fetch('/api/media-profiles/' + encodeURIComponent(state.profile), {
        method: 'PUT',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(payload)
      });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok || data.success === false) {
        throw new Error(data.detail || data.message || 'Save failed');
      }
      showToast('Parametres enregistres', 'success');
      closeProfileSettings();
      await loadMediaLibrary();
    } catch (e) {
      console.error('Error saving profile settings:', e);
      showToast(e.message || 'Save failed', 'error');
    } finally {
      if (save) {
        save.disabled = false;
        save.textContent = 'Enregistrer';
      }
    }
  }

  function openProfileDeleteConfirm() {
    if (!state.profile) return;
    var profile = state.profileSettings || profileByUsername(state.profile) || { username: state.profile };
    state.pendingProfileDelete = profile;
    var target = $('mediaProfileDeleteTarget');
    var confirm = $('mediaProfileDeleteConfirm');
    var modal = $('mediaProfileDeleteModal');
    if (target) target.textContent = profileLabel(profile) + ' / ' + profile.username;
    if (confirm) {
      confirm.disabled = false;
      confirm.textContent = 'Supprimer le profil';
    }
    if (modal) {
      modal.style.display = 'flex';
      modal.setAttribute('aria-hidden', 'false');
      document.body.classList.add('media-delete-open');
    }
  }

  function closeProfileDeleteConfirm() {
    var modal = $('mediaProfileDeleteModal');
    if (modal) {
      modal.style.display = 'none';
      modal.setAttribute('aria-hidden', 'true');
      document.body.classList.remove('media-delete-open');
    }
    state.pendingProfileDelete = null;
  }

  async function confirmDeleteProfile() {
    var profile = state.pendingProfileDelete;
    if (!profile || !profile.username) return;
    var confirm = $('mediaProfileDeleteConfirm');
    if (confirm) {
      confirm.disabled = true;
      confirm.textContent = 'Suppression...';
    }

    try {
      var res = await fetch('/api/media-profiles/' + encodeURIComponent(profile.username), { method: 'DELETE' });
      var data = await res.json().catch(function() { return {}; });
      if (!res.ok || data.success === false) {
        throw new Error(data.detail || data.message || 'Delete failed');
      }
      closeProfileDeleteConfirm();
      closeProfileSettings();
      state.profile = '';
      state.profileSettings = null;
      showToast('Profil supprime', 'success');
      await loadMediaLibrary();
    } catch (e) {
      console.error('Error deleting profile:', e);
      showToast(e.message || 'Delete failed', 'error');
      if (confirm) {
        confirm.disabled = false;
        confirm.textContent = 'Supprimer le profil';
      }
    }
  }

  function syncFilterControls() {
    var buttons = document.querySelectorAll('.media-kind-btn');
    buttons.forEach(function(btn) {
      btn.classList.toggle('active', btn.dataset.kind === state.kind);
    });

    var sort = $('mediaSortSelect');
    if (sort) sort.value = state.sort;
  }

  function setKind(kind) {
    state.kind = kind || 'all';
    loadMediaLibrary();
  }

  function setProfile(profile, shouldScroll) {
    state.profile = profile || '';
    if (state.profile) state.kind = 'video';
    loadMediaLibrary();
    if (shouldScroll) {
      var recent = document.querySelector('.media-recent-section');
      if (recent && recent.scrollIntoView) {
        recent.scrollIntoView({ block: 'start', behavior: 'smooth' });
      }
    }
  }

  function scrollProfiles(direction) {
    var rail = $('mediaProfileRail');
    if (!rail) return;
    var amount = Math.max(280, Math.floor(rail.clientWidth * 0.8));
    rail.scrollBy({ left: direction * amount, behavior: 'smooth' });
  }

  function bindEvents() {
    var search = $('mediaSearchInput');
    if (search) {
      search.addEventListener('input', function() {
        clearTimeout(searchTimer);
        searchTimer = setTimeout(function() {
          state.search = search.value.trim();
          loadMediaLibrary();
        }, 180);
      });
    }

    var sortSelect = $('mediaSortSelect');
    if (sortSelect) {
      sortSelect.addEventListener('change', function() {
        state.sort = sortSelect.value;
        loadMediaLibrary();
      });
    }

    document.querySelectorAll('.media-kind-btn').forEach(function(btn) {
      btn.addEventListener('click', function() {
        setKind(btn.dataset.kind || 'all');
      });
    });

    var prev = $('mediaProfilePrev');
    if (prev) prev.addEventListener('click', function() { scrollProfiles(-1); });

    var next = $('mediaProfileNext');
    if (next) next.addEventListener('click', function() { scrollProfiles(1); });

    var clearProfile = $('mediaClearProfileBtn');
    if (clearProfile) clearProfile.addEventListener('click', function() { setProfile('', false); });

    var profileSettings = $('mediaProfileSettingsBtn');
    if (profileSettings) profileSettings.addEventListener('click', openProfileSettings);

    var rail = $('mediaProfileRail');
    if (rail) {
      rail.addEventListener('click', function(ev) {
        var card = ev.target.closest('.media-profile-card');
        if (!card) return;
        setProfile(card.dataset.profile || '', true);
      });
    }

    var grid = $('mediaGrid');
    if (grid) {
      grid.addEventListener('click', function(ev) {
        var action = ev.target.closest('[data-media-action]');
        if (action) {
          if (action.dataset.mediaAction === 'delete') {
            ev.preventDefault();
            ev.stopPropagation();
            openDeleteConfirm(itemById(action.dataset.mediaId));
          }
          return;
        }
        var card = ev.target.closest('.media-card');
        if (card) openViewer(itemById(card.dataset.mediaId));
      });
      grid.addEventListener('keydown', function(ev) {
        if (ev.key !== 'Enter' && ev.key !== ' ') return;
        var card = ev.target.closest('.media-card');
        if (!card) return;
        ev.preventDefault();
        openViewer(itemById(card.dataset.mediaId));
      });
    }

    var close = $('mediaViewerClose');
    if (close) close.addEventListener('click', closeViewer);

    var viewerDelete = $('mediaViewerDelete');
    if (viewerDelete) {
      viewerDelete.addEventListener('click', function() {
        openDeleteConfirm(state.currentViewerItem);
      });
    }

    var deleteCancel = $('mediaDeleteCancel');
    if (deleteCancel) deleteCancel.addEventListener('click', closeDeleteConfirm);

    var deleteConfirm = $('mediaDeleteConfirm');
    if (deleteConfirm) deleteConfirm.addEventListener('click', confirmDeleteMedia);

    var profileSettingsForm = $('mediaProfileSettingsForm');
    if (profileSettingsForm) profileSettingsForm.addEventListener('submit', saveProfileSettings);

    var profileSettingsClose = $('mediaProfileSettingsClose');
    if (profileSettingsClose) profileSettingsClose.addEventListener('click', closeProfileSettings);

    var profileSettingsCancel = $('mediaProfileSettingsCancel');
    if (profileSettingsCancel) profileSettingsCancel.addEventListener('click', closeProfileSettings);

    var profileDelete = $('mediaProfileDeleteBtn');
    if (profileDelete) profileDelete.addEventListener('click', openProfileDeleteConfirm);

    var profileDeleteCancel = $('mediaProfileDeleteCancel');
    if (profileDeleteCancel) profileDeleteCancel.addEventListener('click', closeProfileDeleteConfirm);

    var profileDeleteConfirm = $('mediaProfileDeleteConfirm');
    if (profileDeleteConfirm) profileDeleteConfirm.addEventListener('click', confirmDeleteProfile);

    var viewer = $('mediaViewer');
    if (viewer) {
      viewer.addEventListener('click', function(ev) {
        if (ev.target === viewer) closeViewer();
      });
    }

    var deleteModal = $('mediaDeleteModal');
    if (deleteModal) {
      deleteModal.addEventListener('click', function(ev) {
        if (ev.target === deleteModal) closeDeleteConfirm();
      });
    }

    var profileSettingsModal = $('mediaProfileSettingsModal');
    if (profileSettingsModal) {
      profileSettingsModal.addEventListener('click', function(ev) {
        if (ev.target === profileSettingsModal) closeProfileSettings();
      });
    }

    var profileDeleteModal = $('mediaProfileDeleteModal');
    if (profileDeleteModal) {
      profileDeleteModal.addEventListener('click', function(ev) {
        if (ev.target === profileDeleteModal) closeProfileDeleteConfirm();
      });
    }

    document.addEventListener('keydown', function(ev) {
      if (ev.key === 'Escape') {
        if (state.pendingProfileDelete) {
          closeProfileDeleteConfirm();
        } else if (state.pendingDelete) {
          closeDeleteConfirm();
        } else if ($('mediaProfileSettingsModal') && $('mediaProfileSettingsModal').style.display !== 'none') {
          closeProfileSettings();
        } else {
          closeViewer();
        }
      }
    });
  }

  document.addEventListener('DOMContentLoaded', function() {
    bindEvents();
    syncFilterControls();
    loadMediaLibrary();
  });
})();
