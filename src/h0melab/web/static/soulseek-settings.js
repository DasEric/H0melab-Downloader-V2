(() => {
  const el = id => document.getElementById(id);
  if (!el('soulseekSettings')) return;
  const status = text => { el('soulseekStatus').textContent = text; };
  let timer = null;

  const PHASES = {
    idle: 'Noch nicht gestartet (startet beim Öffnen des Musik-Tabs)',
    installing: 'Wird installiert …',
    starting: 'Startet …',
    running: 'Läuft',
    error: 'Fehler',
    disabled: 'Ausgeschaltet',
  };

  function describe(cfg, live) {
    const phase = cfg.status || {};
    const parts = [PHASES[phase.phase] || phase.phase || ''];
    if (phase.version) parts.push(`aioslsk ${phase.version}`);
    if (live && live.logged_in) parts.push(`angemeldet als ${live.username}`);
    else if (live && live.phase === 'running') parts.push('verbindet mit Soulseek …');
    else if (cfg.active_username) parts.push(`Konto ${cfg.active_username}${cfg.generated_account ? ' (automatisch angelegt)' : ''}`);
    if (phase.message && phase.phase !== 'running') parts.push(phase.message);
    if (cfg.convert_mp3 && cfg.ffmpeg_found === false) parts.push('ffmpeg fehlt – MP3-Umwandlung pausiert');
    return parts.filter(Boolean).join(' · ');
  }

  async function load() {
    clearTimeout(timer);
    const {soulseek: cfg} = await apiFetch('/api/settings');
    if (!cfg) return;
    if (document.activeElement === null || !el('soulseekSettings').contains(document.activeElement)) {
      el('soulseekEnabled').checked = cfg.enabled;
      el('soulseekUser').value = cfg.username || '';
      el('soulseekPassword').placeholder = cfg.password_set ? 'Leer = unverändert' : 'Leer = automatisch';
      el('soulseekMusicPath').value = cfg.music_path || '';
      el('soulseekMusicPath').placeholder = cfg.music_path_effective || '';
      el('soulseekPort').value = cfg.listen_port;
      el('soulseekShare').checked = cfg.share_downloads;
      el('soulseekConvert').checked = cfg.convert_mp3;
      el('soulseekBitrate').value = cfg.mp3_bitrate || '320k';
      el('soulseekJellyfin').checked = cfg.jellyfin_layout;
    }
    let live = null;
    if (cfg.status && cfg.status.phase === 'running') {
      try { live = await apiFetch('/api/music/status', { timeoutMs: 15000 }); } catch (_error) { live = null; }
    }
    status(describe(cfg, live));
    // Keep following while it installs or connects
    const settled = cfg.status && ['idle', 'disabled', 'error'].includes(cfg.status.phase);
    if (!settled && !(live && live.logged_in)) timer = setTimeout(() => load().catch(() => {}), 3000);
  }

  el('saveSoulseek').addEventListener('click', async () => {
    const button = el('saveSoulseek');
    button.disabled = true;
    try {
      const cfg = {
        enabled: el('soulseekEnabled').checked,
        username: el('soulseekUser').value,
        music_path: el('soulseekMusicPath').value,
        listen_port: el('soulseekPort').value,
        share_downloads: el('soulseekShare').checked,
        convert_mp3: el('soulseekConvert').checked,
        mp3_bitrate: el('soulseekBitrate').value,
        jellyfin_layout: el('soulseekJellyfin').checked,
      };
      if (el('soulseekPassword').value) cfg.password = el('soulseekPassword').value;
      if (!cfg.username) cfg.password = '';
      await apiSend('/api/settings', 'PUT', {soulseek: cfg});
      el('soulseekPassword').value = '';
      await apiSend('/api/settings/soulseek/restart', 'POST', {});
      await load();
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  });

  el('updateSoulseek').addEventListener('click', async () => {
    const button = el('updateSoulseek');
    button.disabled = true;
    status('Neueste Version wird geladen …');
    try {
      await apiSend('/api/settings/soulseek/update', 'POST', {});
      await load();
    } catch (error) { status(error.message); }
    finally { button.disabled = false; }
  });

  load().catch(error => status(error.message));
})();
