(() => {
  const el = id => document.getElementById(id);
  const status = text => { el('plexStatus').textContent = text; };
  function choices(id, rows, selected) {
    const select = el(id);
    select.replaceChildren();
    const empty = document.createElement('option'); empty.value = ''; empty.textContent = 'Keine ausgewählt'; select.append(empty);
    for (const row of rows) {
      const option = document.createElement('option'); option.value = row.id; option.textContent = row.name; select.append(option);
    }
    if (selected && !rows.some(row => row.id === selected)) {
      const option = document.createElement('option'); option.value = selected; option.textContent = `Gespeicherte Auswahl (nicht abgeglichen): ${selected}`; select.append(option);
    }
    select.value = selected || '';
  }
  async function load() {
    const {plex: cfg} = await apiFetch('/api/settings');
    el('plexUrl').value = cfg.url;
    choices('plexMovie', [], cfg.movie_library_id);
    choices('plexSeries', [], cfg.series_library_id);
    status(`Plex-Token ${cfg.key_set ? 'gespeichert' : 'fehlt'} · ${cfg.url ? 'Server konfiguriert' : 'Deaktiviert'}`);
  }
  el('loadPlexLibraries').addEventListener('click', async () => {
    const button = el('loadPlexLibraries');
    button.disabled = true;
    button.textContent = 'Bibliotheken werden geladen …';
    status('Verbindung zu Plex wird geprüft …');
    const url = el('plexUrl').value;
    const key = el('plexKey').value;
    const clearing = el('plexClearKey').checked;
    try {
      const body = {url};
      if (clearing) body.api_key = '';
      else if (key) body.api_key = key;
      const data = await apiSend('/api/settings/plex/libraries', 'POST', body);
      if (url !== el('plexUrl').value || key !== el('plexKey').value || clearing !== el('plexClearKey').checked) {
        status('Zugangsdaten geändert. Bibliotheken bitte erneut laden.'); return;
      }
      if (!Array.isArray(data.libraries)) throw new Error('Ungültige Serverantwort. Downloader aktualisieren und neu starten.');
      choices('plexMovie', data.libraries.filter(row => row.type === 'movies'), el('plexMovie').value);
      choices('plexSeries', data.libraries.filter(row => row.type === 'tvshows'), el('plexSeries').value);
      status(data.libraries.length ? `${data.libraries.length} Film-/Serienbibliotheken geladen. Jetzt oben auswählen und speichern.` : 'Keine Film-/Serienbibliotheken gefunden. In Plex muss der Inhaltstyp Filme oder Serien eingestellt sein.');
    } catch (error) { status(error.message); }
    finally { button.disabled = false; button.textContent = 'Bibliotheken laden'; }
  });
  el('savePlex').addEventListener('click', async () => {
    el('savePlex').disabled = true;
    try {
      const cfg = {url: el('plexUrl').value, movie_library_id: el('plexMovie').value, series_library_id: el('plexSeries').value};
      if (el('plexClearKey').checked) cfg.api_key = '';
      else if (el('plexKey').value) cfg.api_key = el('plexKey').value;
      await apiSend('/api/settings', 'PUT', {plex: cfg});
      el('plexKey').value = ''; el('plexClearKey').checked = false;
      await load();
    } catch (error) { status(error.message); }
    finally { el('savePlex').disabled = false; }
  });

  let loginGeneration = 0;
  let loginTimer = null;
  function endLogin() {
    loginGeneration++;
    clearTimeout(loginTimer);
    el('plexLogin').disabled = false;
    el('plexLoginCancel').hidden = true;
    el('plexAuthLink').hidden = true;
    el('plexAuthLink').removeAttribute('href');
    el('plexServerPicker').hidden = true;
  }
  el('plexLogin').addEventListener('click', async () => {
    endLogin();
    const generation = loginGeneration;
    const popup = window.open('about:blank', '_blank');
    if (popup) popup.opener = null;
    el('plexLogin').disabled = true;
    el('plexLoginCancel').hidden = false;
    status('Plex-Anmeldung wird gestartet …');
    try {
      const result = await apiSend('/api/settings/plex/login/start', 'POST', {});
      if (generation !== loginGeneration) { if (popup) popup.close(); return; }
      const authUrl = new URL(result.auth_url);
      if (authUrl.origin !== 'https://app.plex.tv' || authUrl.pathname !== '/auth') throw new Error('Ungültige Plex-Anmeldeadresse.');
      el('plexAuthLink').href = authUrl.href;
      el('plexAuthLink').hidden = false;
      if (popup) popup.location.replace(authUrl.href);
      status('Bitte im Plex-Fenster anmelden. Diese Seite wartet auf die Bestätigung.');
      const deadline = Date.now() + result.expires_in * 1000;
      async function pollLogin() {
        if (generation !== loginGeneration) return;
        try {
          if (Date.now() >= deadline) throw new Error('Anmeldung abgelaufen. Bitte erneut anmelden.');
          const data = await apiSend('/api/settings/plex/login/poll', 'POST', {});
          if (generation !== loginGeneration) return;
          if (data.status === 'pending') { loginTimer = setTimeout(pollLogin, 2000); return; }
          const select = el('plexServers'); select.replaceChildren();
          el('plexConnectionUrl').value = '';
          for (const server of data.servers || []) {
            const option = document.createElement('option'); option.value = server.id;
            option.textContent = `${server.name} — ${server.url}`; select.append(option);
          }
          el('plexServerPicker').hidden = !select.options.length;
          el('plexAuthLink').hidden = true;
          status(select.options.length ? 'Angemeldet. Server-Verbindung auswählen und übernehmen.' : 'Keine eigenen Plex-Server gefunden. Mit dem Konto anmelden, das den Server verwaltet.');
        } catch (error) { endLogin(); status(error.message); }
      }
      loginTimer = setTimeout(pollLogin, 2000);
    } catch (error) {
      if (popup) popup.close();
      if (generation === loginGeneration) { endLogin(); status(error.message); }
    }
  });
  el('plexLoginCancel').addEventListener('click', async () => {
    endLogin();
    try { await apiSend('/api/settings/plex/login/cancel', 'POST', {}); status('Plex-Anmeldung abgebrochen. Bestehende Einstellungen unverändert.'); }
    catch (error) { status(error.message); }
  });
  el('plexSelectServer').addEventListener('click', async () => {
    const button = el('plexSelectServer'); button.disabled = true;
    try {
      const data = await apiSend('/api/settings/plex/login/select', 'POST', {server: el('plexServers').value, url: el('plexConnectionUrl').value});
      el('plexUrl').value = data.url;
      el('plexKey').value = ''; el('plexClearKey').checked = false;
      choices('plexMovie', data.libraries.filter(row => row.type === 'movies'), '');
      choices('plexSeries', data.libraries.filter(row => row.type === 'tvshows'), '');
      endLogin();
      status('Plex-Verbindung und Token gespeichert. Film-/Serienbibliotheken auswählen und „Plex speichern“.');
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  });
  window.addEventListener('pagehide', () => { clearTimeout(loginTimer); loginGeneration++; });
  load().catch(error => status(error.message));
})();
