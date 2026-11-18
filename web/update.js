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
    const ver = s.current ? `Version ${E(s.current)}.` : 'Denne kopi har ikke et versionsnummer (den er ikke bygget som en app).';
    switch (s.state) {
      case 'checking': return `${ver} Tjekker efter opdateringer…`;
      case 'up_to_date': return `${ver} ARK er ajour. ${btn('Tjek igen')}`;
      case 'available': return `${ver} Version ${E(s.version)} er klar. Hent den fra ${E(s.website)}. Dit bibliotek, din lokale AI og dine chats beholdes. ${btn('Tjek igen')}`;
      case 'not_published': return `${ver} Ingen opdateringsoplysninger endnu. ${btn('Prøv igen')}`;
      case 'too_old_os': case 'error': return `${ver} ${E(s.message)} ${btn('Prøv igen')}`;
      default: return `${ver} ${btn('Tjek efter opdateringer')}`;
    }
  }

  function paint(el, s) {
    el.innerHTML = view(s);
    clearTimeout(timer);
    if (s.state === 'checking') timer = setTimeout(() => refresh(el), 400);
  }

  async function refresh(el) {
    if (!el.isConnected) return;
    try { paint(el, await call('state')) } catch { el.textContent = 'ARK kunne ikke spørge efter opdateringer.' }
  }

  async function check(el) {
    try {
      const s = await call('check');
      if (s.error && !s.state) { el.textContent = s.error; return }
      paint(el, s);
    } catch { el.textContent = 'ARK kunne ikke spørge efter opdateringer.' }
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
