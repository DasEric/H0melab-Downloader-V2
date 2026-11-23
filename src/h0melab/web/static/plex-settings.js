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
  load().catch(error => status(error.message));
})();
