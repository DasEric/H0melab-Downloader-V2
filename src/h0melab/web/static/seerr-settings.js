/* Separate integration controls preserve existing general-settings save semantics. */
(() => {
  const el = id => document.getElementById(id);
  const status = message => { el('seerrSettingsStatus').textContent = message; };
  function generateSecret() {
    const bytes = new Uint8Array(32);
    window.crypto.getRandomValues(bytes);
    el('seerrSecret').value = Array.from(bytes, byte => byte.toString(16).padStart(2, '0')).join('');
    status('Zufälliges Secret erzeugt – noch nicht gespeichert. Kopieren und anschließend speichern.');
  }
  el('generateSeerrSecret').addEventListener('click', () => {
    if (el('seerrSecret').value && !window.confirm('Den noch nicht gespeicherten Secret-Entwurf ersetzen?')) return;
    generateSecret();
  });
  el('showSeerrSecret').addEventListener('click', () => {
    const visible = el('seerrSecret').type === 'password';
    el('seerrSecret').type = visible ? 'text' : 'password';
    el('showSeerrSecret').textContent = visible ? 'Verbergen' : 'Anzeigen';
    el('showSeerrSecret').setAttribute('aria-pressed', String(visible));
  });
  el('copySeerrSecret').addEventListener('click', async () => {
    const secret = el('seerrSecret').value;
    if (!secret) { status('Kein Secret-Entwurf vorhanden. Bei Bedarf ein neues erzeugen.'); return; }
    try {
      if (!navigator.clipboard?.writeText) throw new Error('Clipboard unavailable');
      await navigator.clipboard.writeText(secret);
      status('Secret kopiert. Nach dem Speichern in Seerr als Bearer <Secret> eintragen.');
    } catch {
      el('seerrSecret').type = 'text';
      el('showSeerrSecret').textContent = 'Verbergen';
      el('showSeerrSecret').setAttribute('aria-pressed', 'true');
      el('seerrSecret').focus(); el('seerrSecret').select();
      status('Secret markiert – mit Strg+C kopieren. Danach speichern.');
    }
  });
  async function load() {
    const cfg = await apiFetch('/api/integrations/seerr/status', {timeoutMs: 15000});
    el('seerrEnabled').checked = cfg.enabled;
    el('seerrEndpoint').value = new URL(cfg.endpoint, window.location.origin).href;
    status(`${cfg.enabled ? 'Aktiv' : 'Deaktiviert'} · Secret ${cfg.secret_set ? 'gespeichert' : 'fehlt'} · TMDB ${cfg.tmdb_configured ? 'konfiguriert' : 'fehlt'} · Ziele ${cfg.targets_configured ? 'konfiguriert' : 'fehlen'}`);
    if (!cfg.secret_set && !el('seerrSecret').value) generateSecret();
  }
  el('saveSeerr').addEventListener('click', async () => {
    const button = el('saveSeerr'); button.disabled = true;
    try {
      const body = {enabled: el('seerrEnabled').checked};
      if (el('seerrSecret').value) body.webhook_secret = el('seerrSecret').value;
      await apiSend('/api/settings', 'PUT', {seerr: body});
      el('seerrSecret').value = '';
      el('seerrSecret').type = 'password';
      el('showSeerrSecret').textContent = 'Anzeigen';
      el('showSeerrSecret').setAttribute('aria-pressed', 'false');
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
