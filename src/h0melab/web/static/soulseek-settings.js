(() => {
  const el = id => document.getElementById(id);
  if (!el('soulseekSettings')) return;
  const status = text => { el('soulseekStatus').textContent = text; };

  function credentials() {
    const cfg = {url: el('soulseekUrl').value, music_path: el('soulseekMusicPath').value};
    if (el('soulseekClearKey').checked) cfg.api_key = '';
    else if (el('soulseekKey').value) cfg.api_key = el('soulseekKey').value;
    return cfg;
  }

  async function load() {
    const {soulseek: cfg} = await apiFetch('/api/settings');
    if (!cfg) return;
    el('soulseekUrl').value = cfg.url;
    el('soulseekMusicPath').value = cfg.music_path || '';
    el('soulseekMusicPath').placeholder = cfg.music_path_effective || '';
    status(`API-Key ${cfg.key_set ? 'gespeichert' : 'fehlt'} · ${cfg.url ? 'Server konfiguriert' : 'Deaktiviert'}`);
  }

  el('testSoulseek').addEventListener('click', async () => {
    const button = el('testSoulseek');
    button.disabled = true;
    status('Verbindung zu slskd wird geprüft …');
    try {
      const data = await apiSend('/api/settings/soulseek/test', 'POST', credentials());
      if (data.logged_in) status(`Verbunden und bei Soulseek angemeldet${data.username ? ` als ${data.username}` : ''}.`);
      else if (data.connected) status('slskd erreichbar und mit Soulseek verbunden, aber noch nicht angemeldet.');
      else status(`slskd erreichbar, aber nicht mit Soulseek verbunden (${data.state || 'unbekannt'}). Zugangsdaten in slskd prüfen.`);
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  });

  el('saveSoulseek').addEventListener('click', async () => {
    el('saveSoulseek').disabled = true;
    try {
      await apiSend('/api/settings', 'PUT', {soulseek: credentials()});
      el('soulseekKey').value = ''; el('soulseekClearKey').checked = false;
      await load();
    } catch (error) { status(error.message); }
    finally { el('saveSoulseek').disabled = false; }
  });

  load().catch(error => status(error.message));
})();
