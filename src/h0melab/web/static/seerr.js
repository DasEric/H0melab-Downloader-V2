(() => {
  function field(container, label, value) {
    const p = document.createElement('p');
    const strong = document.createElement('strong'); strong.textContent = label + ': ';
    p.append(strong, document.createTextNode(value == null ? '—' : String(value)));
    container.append(p);
  }
  let nextOffset = null;
  let loading = false;
  const more = document.getElementById('moreSeerr');
  async function load(append = false) {
    if (loading) return;
    loading = true; more.disabled = true;
    const error = document.getElementById('seerrError');
    try {
      const offset = append ? nextOffset : 0;
      const data = await apiFetch(`/api/integrations/seerr/requests?offset=${offset || 0}&limit=50`, {timeoutMs:15000});
      const parent = document.getElementById('seerrRequests');
      if (!append) parent.replaceChildren();
      nextOffset = data.next_offset;
      more.hidden = nextOffset == null;
      for (const row of data.items) {
        const card = document.createElement('section'); card.className='card';
        const identity = row.identity || row.payload;
        const h = document.createElement('h2'); h.textContent=identity.title || `Anfrage #${row.request_id}`; card.append(h);
        for (const [label,value] of Object.entries({
          'Seerr-ID':row.request_id,'Medientyp':identity.media_type,'TMDB-ID':identity.tmdb_id,
          'IMDb-ID':identity.imdb_id,'Jahr':identity.release_year,'Staffeln':row.payload.seasons.join(', '),
          'Identitätsprüfung':row.identity_status,'Verarbeitung':row.status,'Ziel':row.target_path,
          'Automatische Wiederholungen':row.retry_count || 0,
          'Nächste Prüfung':row.status === 'waiting_retry' && row.queue?.not_before ? new Date(row.queue.not_before * 1000).toLocaleString() : '—',
          'Anbieter-URL':row.selected_url,'Fehler':row.error,
          'Übertragen':row.queue ? `${Number(row.queue.current_bytes || 0).toLocaleString()} Bytes / ${row.queue.total_bytes > 0 ? Number(row.queue.total_bytes).toLocaleString() + ' Bytes' : 'Gesamtgröße unbekannt'}` : '—',
          'Download':row.queue ? `${row.queue.status} · ${row.queue.current_episode}/${row.queue.total_episodes}` : 'Kein Queueeintrag'
        })) field(card,label,value);
        for (const episode of row.episode_review?.pending || []) {
          field(card, `Offen S${String(episode.season_number).padStart(2,'0')}E${String(episode.episode_number).padStart(2,'0')}`,
            `${episode.name || ''} · ${episode.reason === 'not_yet_aired' ? 'Noch nicht ausgestrahlt' : 'Anbieterfolge fehlt'} · ${episode.air_date || 'Datum unbekannt'}`);
        }
        if (row.candidates && row.candidates.length) {
          const details=document.createElement('details'), summary=document.createElement('summary');
          summary.textContent='Anbieter-Treffer und Prüfergebnisse'; details.append(summary);
          for (const candidate of row.candidates) {
            const pre=document.createElement('pre'); pre.textContent=JSON.stringify(candidate,null,2); details.append(pre);
          }
          card.append(details);
        }
        if (['manual_review','partial'].includes(row.status) || ['failed','cancelled'].includes(row.queue?.status)) {
          const retry=document.createElement('button'); retry.type='button'; retry.className='btn btn-secondary'; retry.textContent='Metadaten und Anbieter erneut prüfen';
          retry.addEventListener('click',async () => {
            retry.disabled=true;
            try { await apiSend(`/api/integrations/seerr/requests/${row.request_id}/retry`,'POST',{}); await load(); }
            catch (exception) { document.getElementById('seerrError').textContent=exception.message; }
            finally { retry.disabled=false; }
          });
          card.append(retry);
          const form=document.createElement('form');
          const label=document.createElement('label'); label.textContent='Anbieter manuell bestätigen: ';
          const select=document.createElement('select'); select.required=true;
          const placeholder=document.createElement('option'); placeholder.value=''; placeholder.textContent='Treffer wählen'; select.append(placeholder);
          for (const candidate of row.candidates || []) {
            const option=document.createElement('option'); option.value=candidate.url;
            option.textContent=`${candidate.title || candidate.url} (${candidate.source || 'Anbieter'})`;
            option.disabled=Boolean(candidate.assessment?.conflict || candidate.metadata_conflicts?.length);
            select.append(option);
          }
          label.append(select); form.append(label);
          const reason=document.createElement('textarea'); reason.required=true; reason.minLength=5; reason.maxLength=2000;
          reason.placeholder='Begründung der Identitätsprüfung'; reason.setAttribute('aria-label','Begründung'); form.append(reason);
          const mapping=document.createElement('textarea'); mapping.placeholder='Optionale Episodenzuordnung als JSON: [{"url":"…","season_number":1,"episode_number":1}]';
          mapping.setAttribute('aria-label','Explizite Episodenzuordnung'); form.append(mapping);
          const review=row.episode_review || {};
          if (review.provider_url && Array.isArray(review.canonical)) {
            const panel=document.createElement('fieldset');
            const legend=document.createElement('legend'); legend.textContent='TMDB-Episoden mit Anbieterfolgen abgleichen'; panel.append(legend);
            const choices=[];
            for (const canonical of review.canonical) {
              const item=document.createElement('label'); item.className='field';
              const title=document.createElement('span'); title.textContent=`S${String(canonical.season_number).padStart(2,'0')}E${String(canonical.episode_number).padStart(2,'0')} · ${canonical.name || 'Titel unbekannt'}`; item.append(title);
              const choice=document.createElement('select');
              const empty=document.createElement('option'); empty.value=''; empty.textContent='Automatisch prüfen / keine manuelle Zuordnung'; choice.append(empty);
              for (const source of review.source || []) {
                const option=document.createElement('option'); option.value=source.url;
                option.textContent=`S${source.season_number}E${source.episode_number} · ${source.title_de || source.title_en || source.episode_title || 'Titel fehlt'} · ${source.url}`;
                choice.append(option);
              }
              const prior=(row.episode_mapping || []).find(m => m.season_number===canonical.season_number && m.episode_number===canonical.episode_number);
              if (prior) choice.value=prior.url;
              choices.push({choice,canonical}); item.append(choice); panel.append(item);
            }
            function synchronize() {
              mapping.value=JSON.stringify(choices.filter(x=>x.choice.value).map(x=>({url:x.choice.value,season_number:x.canonical.season_number,episode_number:x.canonical.episode_number,source_season_number:(review.source || []).find(source=>source.url===x.choice.value).season_number})),null,2);
            }
            choices.forEach(x=>x.choice.addEventListener('change',synchronize));
            function matchProvider() { panel.disabled=select.value!==review.provider_url; }
            select.addEventListener('change',()=>{ matchProvider(); mapping.value=''; choices.forEach(x=>{x.choice.value='';}); });
            select.value=review.provider_url; matchProvider(); synchronize();
            form.insertBefore(panel,mapping);
          }
          const hint=document.createElement('p'); hint.className='hint'; hint.textContent='Die Auswahl wird vor dem Download erneut geprüft. Widersprüchliche Medien-IDs werden auch nach manueller Freigabe abgewiesen.'; form.append(hint);
          const button=document.createElement('button'); button.type='submit'; button.className='btn btn-primary'; button.textContent='Auswahl prüfen und fortsetzen'; form.append(button);
          const result=document.createElement('p'); result.setAttribute('role','status'); form.append(result);
          form.addEventListener('submit',async event => {
            event.preventDefault(); button.disabled=true;
            try {
              const episode_mapping=mapping.value.trim() ? JSON.parse(mapping.value) : [];
              await apiSend(`/api/integrations/seerr/requests/${row.request_id}/review`,'POST',{
                selected_url:select.value,reason:reason.value,episode_mapping
              });
              await load();
            } catch (exception) { result.textContent=exception.message; }
            finally { button.disabled=false; }
          });
          card.append(form);
        }
        parent.append(card);
      }
      error.textContent=parent.children.length ? '' : 'Noch keine Seerr-Anfragen.';
    } catch (exception) { error.textContent=exception.message; }
    finally { loading = false; more.disabled = false; }
  }
  document.getElementById('refreshSeerr').addEventListener('click',()=>load(false));
  more.addEventListener('click',()=>load(true));
  load();
})();
