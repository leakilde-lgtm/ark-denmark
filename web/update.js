/* Check for updates, in About (desktop app only). It runs only when the person presses Check for updates, or chooses
   Lastbook > Check for Updates… (the menu calls LBUpdate.fromMenu; no address starts it). It only says whether a newer version
   is on the website: nothing is downloaded or installed. engine/update.py does the check. */
(() => {
  const E = s => String(s ?? '').replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
  let timer = null, menuAsked = false;

  async function call(action) {
    const r = await fetch('/api/update/' + action, action === 'state' ? {} : { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' });
    return r.json();
  }

  function view(s) {
    const btn = (t) => `<button class="btn plain" data-upd="check">${t}</button>`;
    const ver = s.current ? `Version ${E(s.current)}.` : 'This copy has no version number (it was not built as an app).';
    switch (s.state) {
      case 'checking': return `${ver} Checking for updates…`;
      case 'up_to_date': return `${ver} ARK is up to date. ${btn('Check again')}`;
      case 'available': return `${ver} Version ${E(s.version)} is available. Download it from ${E(s.website)}. Your library, local AI and chats stay. ${btn('Check again')}`;
      case 'not_published': return `${ver} No update information yet. ${btn('Try again')}`;
      case 'too_old_os': case 'error': return `${ver} ${E(s.message)} ${btn('Try again')}`;
      default: return `${ver} ${btn('Check for updates')}`;
    }
  }

  function paint(el, s) {
    el.innerHTML = view(s);
    clearTimeout(timer);
    if (s.state === 'checking') timer = setTimeout(() => refresh(el), 400);
  }

  async function refresh(el) {
    if (!el.isConnected) return;
    try { paint(el, await call('state')) } catch { el.textContent = 'ARK could not ask about updates.' }
  }

  async function check(el) {
    try {
      const s = await call('check');
      if (s.error && !s.state) { el.textContent = s.error; return }
      paint(el, s);
    } catch { el.textContent = 'ARK could not ask about updates.' }
  }

  window.LBUpdate = {
    card: () => '<div class="card"><b>ARK</b><small id="updBox">…</small></div>',
    mount(root) {
      const el = root.querySelector('#updBox'); if (!el) return;
      el.addEventListener('click', e => { if (e.target.closest('[data-upd]')) check(el) });
      if (menuAsked) { menuAsked = false; return check(el) }
      refresh(el);
    },
    fromMenu() {     // Lastbook > Check for Updates…: About, then one check
      const el = document.getElementById('updBox');
      if (el && location.hash.startsWith('#/about')) return check(el);
      menuAsked = true;
      location.hash = '#/about';
    },
  };
})();
