(() => {
  const el = id => document.getElementById(id);
  const status = text => { el('jellyfinStatus').textContent = text; };
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
    const {jellyfin: cfg} = await apiFetch('/api/settings');
    el('jellyfinUrl').value = cfg.url;
    choices('jellyfinMovie', [], cfg.movie_library_id);
    choices('jellyfinSeries', [], cfg.series_library_id);
    choices('jellyfinMusic', [], cfg.music_library_id);
    status(`API-Key ${cfg.key_set ? 'gespeichert' : 'fehlt'} · ${cfg.url ? 'Server konfiguriert' : 'Deaktiviert'}`);
  }
  el('loadJellyfinLibraries').addEventListener('click', async () => {
    const button = el('loadJellyfinLibraries');
    button.disabled = true;
    button.textContent = 'Bibliotheken werden geladen …';
    status('Verbindung zu Jellyfin wird geprüft …');
    const url = el('jellyfinUrl').value;
    const key = el('jellyfinKey').value;
    const clearing = el('jellyfinClearKey').checked;
    try {
      const body = {url};
      if (clearing) body.api_key = '';
      else if (key) body.api_key = key;
      const data = await apiSend('/api/settings/jellyfin/libraries', 'POST', body);
      if (url !== el('jellyfinUrl').value || key !== el('jellyfinKey').value || clearing !== el('jellyfinClearKey').checked) {
        status('Zugangsdaten geändert. Bibliotheken bitte erneut laden.'); return;
      }
      if (!Array.isArray(data.libraries)) throw new Error('Ungültige Serverantwort. Downloader aktualisieren und neu starten.');
      choices('jellyfinMovie', data.libraries.filter(row => row.type === 'movies'), el('jellyfinMovie').value);
      choices('jellyfinSeries', data.libraries.filter(row => row.type === 'tvshows'), el('jellyfinSeries').value);
      choices('jellyfinMusic', data.libraries.filter(row => row.type === 'music'), el('jellyfinMusic').value);
      status(data.libraries.length ? `${data.libraries.length} Bibliotheken geladen. Jetzt oben auswählen und speichern.` : 'Keine passenden Bibliotheken gefunden. In Jellyfin muss der Inhaltstyp Filme, Serien oder Musik eingestellt sein.');
    } catch (error) { status(error.message); }
    finally { button.disabled = false; button.textContent = 'Bibliotheken laden'; }
  });
  el('saveJellyfin').addEventListener('click', async () => {
    el('saveJellyfin').disabled = true;
    try {
      const cfg = {url: el('jellyfinUrl').value, movie_library_id: el('jellyfinMovie').value, series_library_id: el('jellyfinSeries').value, music_library_id: el('jellyfinMusic').value};
      if (el('jellyfinClearKey').checked) cfg.api_key = '';
      else if (el('jellyfinKey').value) cfg.api_key = el('jellyfinKey').value;
      await apiSend('/api/settings', 'PUT', {jellyfin: cfg});
      el('jellyfinKey').value = ''; el('jellyfinClearKey').checked = false;
      await load();
    } catch (error) { status(error.message); }
    finally { el('saveJellyfin').disabled = false; }
  });
  load().catch(error => status(error.message));
})();
