/* Separate integration controls preserve existing general-settings save semantics. */
(() => {
  const el = id => document.getElementById(id);
  const status = message => { el('seerrSettingsStatus').textContent = message; };
  async function load() {
    const cfg = await apiFetch('/api/integrations/seerr/status', {timeoutMs: 15000});
    el('seerrEnabled').checked = cfg.enabled;
    el('seerrEndpoint').value = new URL(cfg.endpoint, window.location.origin).href;
    status(`${cfg.enabled ? 'Aktiv' : 'Deaktiviert'} · Secret ${cfg.secret_set ? 'gespeichert' : 'fehlt'} · TMDB ${cfg.tmdb_configured ? 'konfiguriert' : 'fehlt'} · Ziele ${cfg.targets_configured ? 'konfiguriert' : 'fehlen'}`);
  }
  el('saveSeerr').addEventListener('click', async () => {
    const button = el('saveSeerr'); button.disabled = true;
    try {
      const body = {enabled: el('seerrEnabled').checked};
      if (el('seerrSecret').value) body.webhook_secret = el('seerrSecret').value;
      await apiSend('/api/settings', 'PUT', {seerr: body});
      el('seerrSecret').value = '';
      await load();
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  });
  el('testSeerr').addEventListener('click', async () => {
    try {
      const result = await apiSend('/api/integrations/seerr/test', 'POST', {});
      const missing = Object.entries(result.checks).filter(([,ok]) => !ok).map(([name]) => name);
      status(missing.length ? `Konfiguration unvollständig: ${missing.join(', ')}` : 'Lokale Konfiguration vollständig. Für den Erreichbarkeitstest in Seerr „Test Notification“ senden.');
    } catch (error) { status(error.message); }
  });
  load().catch(error => status(error.message));
})();
