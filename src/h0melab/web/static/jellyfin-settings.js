(() => {
  const el = id => document.getElementById(id);
  const status = text => { el('jellyfinStatus').textContent = text; };
  async function load() {
    const {jellyfin: cfg} = await apiFetch('/api/settings');
    el('jellyfinUrl').value = cfg.url;
    el('jellyfinMovie').value = cfg.movie_library_id;
    el('jellyfinSeries').value = cfg.series_library_id;
    status(`API-Key ${cfg.key_set ? 'gespeichert' : 'fehlt'} · ${cfg.url ? 'Server konfiguriert' : 'Deaktiviert'}`);
  }
  el('saveJellyfin').addEventListener('click', async () => {
    el('saveJellyfin').disabled = true;
    try {
      const cfg = {url: el('jellyfinUrl').value, movie_library_id: el('jellyfinMovie').value, series_library_id: el('jellyfinSeries').value};
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
