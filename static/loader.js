// Component loader for shared header and footer
(function() {
  function setVersionText(version) {
    const label = `v${version || 'unknown'}`;
    const versionTextEl = document.getElementById('appVersionText');
    const versionEl = document.getElementById('appVersion');

    if (versionTextEl) {
      versionTextEl.textContent = label;
    } else if (versionEl) {
      versionEl.textContent = label;
    }

    if (versionEl && !versionEl.classList.contains('has-update')) {
      versionEl.setAttribute('aria-label', `Current version ${label}`);
    }
  }

  function showUpdateBadge(updateInfo) {
    const versionEl = document.getElementById('appVersion');
    const badge = document.getElementById('appUpdateBadge');
    const versionTextEl = document.getElementById('appVersionText');

    if (!versionEl || !badge) return;

    const currentVersion = versionTextEl ? versionTextEl.textContent : versionEl.textContent;
    const latestVersion = updateInfo && (updateInfo.latest_version || updateInfo.latestVersion);
    const behindBy = updateInfo && Number(updateInfo.behindBy || 0);
    const message = latestVersion
      ? `Update available: v${latestVersion}`
      : behindBy > 0
        ? `${behindBy} update${behindBy === 1 ? '' : 's'} available`
        : 'Update available';

    badge.hidden = false;
    badge.title = message;
    versionEl.classList.add('has-update');
    versionEl.title = message;
    versionEl.setAttribute('aria-label', `${currentVersion}. ${message}`);
  }

  // Load header
  fetch('/static/header.html')
    .then(res => res.text())
    .then(html => {
      const placeholder = document.getElementById('header-placeholder');
      if (placeholder) {
        placeholder.outerHTML = html;

        // After header injection, highlight active nav link
        const currentPath = window.location.pathname;
        const navLinks = document.querySelectorAll('.nav-link');
        navLinks.forEach(link => {
          const href = link.getAttribute('href');
          if (currentPath === href || (currentPath === '/' && href === '/discover')) {
            link.classList.add('active');
          }
        });

        // Load version
        (async function loadVersion() {
          try {
            const res = await fetch('/api/version');
            const data = await res.json();
            setVersionText(data.version);
          } catch (e) {
            console.error('Error loading version:', e);
          }
        })();

        // Check release-based app updates
        (async function checkAppUpdateStatus() {
          try {
            const res = await fetch('/api/system/check-update', { cache: 'no-store' });
            if (!res.ok) return;

            const data = await res.json();
            if (data.update_available) {
              showUpdateBadge(data);
            }
          } catch (e) {
            console.error('Error checking app updates:', e);
          }
        })();

        // Check Git status
        (async function checkGitStatus() {
          try {
            const res = await fetch('/api/git/status');
            if (res.ok) {
              const data = await res.json();
              const btn = document.getElementById('gitStatusBtn');
              if (btn && data.isGitRepo) {
                btn.style.display = 'inline-block';

                if (data.hasUpdates) {
                  document.getElementById('gitStatusIcon').textContent = '🆕';
                  document.getElementById('gitStatusText').textContent = 'Update!';
                  btn.style.borderColor = 'var(--accent)';
                  btn.style.color = 'var(--accent)';
                  showUpdateBadge(data);
                }
              }
            }
          } catch (e) {
            console.error('Error checking git status:', e);
          }
        })();
      }
    })
    .catch(err => console.error('Error loading header:', err));

  // Load footer
  fetch('/static/footer.html')
    .then(res => res.text())
    .then(html => {
      const placeholder = document.getElementById('footer-placeholder');
      if (placeholder) {
        placeholder.outerHTML = html;
      }
    })
    .catch(err => console.error('Error loading footer:', err));
})();
