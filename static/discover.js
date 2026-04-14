// ============================================
// Discover Page - Browse live Chaturbate models
// ============================================

// State
let currentPage = 1;
let totalPages = 1;
let currentGender = '';
let currentSort = 'viewers';
let currentSearch = '';
let activeTags = [];
let searchTimeout = null;

// ============================================
// Fetch discover data
// ============================================
async function fetchDiscover() {
  var grid = document.getElementById('discoverGrid');
  grid.innerHTML = '<div class="empty-message"><div class="icon">&#9203;</div><p>Loading models...</p></div>';

  var params = new URLSearchParams({
    page: currentPage,
    limit: 24
  });
  if (currentGender) params.set('gender', currentGender);
  if (currentSearch) params.set('search', currentSearch);
  if (currentSort) params.set('sort', currentSort);
  if (activeTags.length > 0) params.set('tags', activeTags.join(','));

  try {
    var res = await fetch('/api/discover?' + params.toString());
    if (res.ok) {
      var data = await res.json();
      renderGrid(data.models || []);
      renderPagination(data.page || 1, data.total_pages || 1);
    } else {
      grid.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Failed to load models.</p></div>';
      document.getElementById('pagination').style.display = 'none';
    }
  } catch (e) {
    console.error('Error loading discover:', e);
    grid.innerHTML = '<div class="empty-message"><div class="icon">&#9888;</div><p>Connection error.</p></div>';
    document.getElementById('pagination').style.display = 'none';
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
function renderGrid(models) {
  var grid = document.getElementById('discoverGrid');

  if (!models.length) {
    grid.innerHTML = '<div class="empty-message"><div class="icon">&#128269;</div><p>No models found</p></div>';
    return;
  }

  // Detect column count from the grid's computed style and trim to a full row
  var cols = getGridColumnCount(grid);
  if (cols > 1 && models.length > cols) {
    models = models.slice(0, Math.floor(models.length / cols) * cols);
  }

  grid.innerHTML = models.map(function(model) {
    var thumbUrl = model.thumbnail || ('https://roomimg.stream.highwebmedia.com/ri/' + model.username + '.jpg');
    var viewerText = model.viewers ? ('<span class="discover-viewers">&#128065; ' + Number(model.viewers).toLocaleString() + '</span>') : '';
    var ageText = model.age ? ('<span class="discover-age">' + model.age + '</span>') : '';
    var tagsHtml = '';
    if (model.tags && model.tags.length > 0) {
      var displayTags = model.tags.slice(0, 3);
      tagsHtml = '<div class="discover-tags">' + displayTags.map(function(t) {
        return '<span class="discover-tag" onclick="event.stopPropagation(); addTagFilter(\'' + escapeHtml(t) + '\')">' + escapeHtml(t) + '</span>';
      }).join('') + '</div>';
    }

    return '<div class="discover-card" onclick="openWatch(\'' + escapeHtml(model.username) + '\')">' +
      '<div class="discover-card-thumb">' +
        '<img src="' + escapeHtml(thumbUrl) + '" alt="' + escapeHtml(model.username) + '" ' +
          'onerror="this.src=\'data:image/svg+xml,%3Csvg xmlns=%22http://www.w3.org/2000/svg%22 width=%22280%22 height=%22200%22%3E%3Crect fill=%22%231a1f3a%22 width=%22280%22 height=%22200%22/%3E%3Ctext x=%2250%25%22 y=%2250%25%22 dominant-baseline=%22middle%22 text-anchor=%22middle%22 fill=%22%23a0aec0%22 font-family=%22system-ui%22 font-size=%2216%22%3E' + escapeHtml(model.username) + '%3C/text%3E%3C/svg%3E\'" loading="lazy" />' +
        viewerText +
        ageText +
        '<span class="live-badge">LIVE</span>' +
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
function openWatch(username) {
  window.location.href = '/watch/' + encodeURIComponent(username);
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
function renderPagination(page, pages) {
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

function setSort(sort) {
  currentSort = sort;
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

  // Load data
  fetchDiscover();
});
