/* Music page: discover rows plus Soulseek search through slskd, styled like home.js.
   Downloads show up on the queue page (soulseek-queue.js). */
(function () {
  const el = (id) => document.getElementById(id);
  const track = el("musicSourceTrack");
  const thumb = el("musicSourceThumb");
  const form = el("musicSearchForm");
  const query = el("musicQuery");
  const searchButton = el("musicSearchBtn");
  const notice = el("musicNotice");
  const statusRow = el("musicSearchStatus");
  const statusText = el("musicSearchStatusText");
  const pulse = el("musicPulse");
  const results = el("musicResults");
  const browse = el("musicBrowse");
  const filterLossless = el("musicFilterLossless");
  const filterFree = el("musicFilterFree");
  const tracksBox = el("musicTracks");
  const selectAll = el("musicSelectAll");

  const POLL_MS = 1500;

  let searchId = null;
  let searchGeneration = 0;
  let releases = [];
  let current = null;
  let available = false;

  /* ----- helpers ----- */

  function moveThumb() {
    const active = track.querySelector(".segmented-btn.active");
    if (!active) return;
    const trackRect = track.getBoundingClientRect();
    const rect = active.getBoundingClientRect();
    thumb.style.width = `${rect.width}px`;
    thumb.style.transform = `translateX(${rect.left - trackRect.left + track.scrollLeft}px)`;
  }

  function duration(seconds) {
    const total = Math.round(Number(seconds) || 0);
    if (!total) return "";
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = String(total % 60).padStart(2, "0");
    return h ? `${h}:${String(m).padStart(2, "0")}:${s}` : `${m}:${s}`;
  }

  function speed(bytesPerSecond) {
    return bytesPerSecond ? `${formatSize(bytesPerSecond)}/s` : "";
  }

  function quality(item) {
    if (item.bit_depth && item.sample_rate) {
      return `${item.bit_depth} bit / ${(item.sample_rate / 1000).toFixed(1).replace(/\.0$/, "")} kHz`;
    }
    return item.bitrate ? `${item.bitrate} kbps` : "";
  }

  function message(container, text) {
    container.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = text;
    container.append(empty);
  }

  function showNotice(title, text) {
    el("musicNoticeTitle").textContent = title;
    el("musicNoticeText").textContent = text;
    notice.hidden = false;
  }

  /* ----- connection ----- */

  async function checkStatus() {
    try {
      const data = await apiFetch("/api/music/status", { timeoutMs: 20000 });
      if (!data.configured) {
        showNotice(
          t("music.not_configured", "Soulseek is not set up yet"),
          t("music.not_configured_hint", "Add the address and API key of your slskd instance under Settings → Integrations.")
        );
        return false;
      }
      if (data.reachable === false) {
        showNotice(t("music.unreachable", "slskd is not reachable"), data.error || "");
        return false;
      }
      if (!data.logged_in) {
        showNotice(
          t("music.offline", "slskd is not logged in to Soulseek"),
          t("music.offline_hint", "Check the Soulseek username and password in slskd. Searches will fail until it is connected.")
        );
      } else {
        notice.hidden = true;
      }
      return true;
    } catch (error) {
      showNotice(t("music.unreachable", "slskd is not reachable"), error.message);
      return false;
    }
  }

  /* ----- search ----- */

  function forgetSearch() {
    if (searchId) {
      apiFetch(`/api/music/search/${searchId}`, { method: "DELETE" }).catch(() => {});
    }
    searchId = null;
  }

  form.addEventListener("submit", async (event) => {
    event.preventDefault();
    const text = query.value.trim();
    if (text.length < 2) {
      showToast(t("catalog.search_too_short", "Please enter at least two characters."));
      return;
    }
    const generation = ++searchGeneration;
    forgetSearch();
    browse.hidden = true;
    releases = [];
    searchButton.disabled = true;
    results.hidden = false;
    statusRow.hidden = false;
    pulse.classList.add("active");
    statusText.textContent = t("music.searching", "Searching the Soulseek network…");
    message(results, t("music.waiting", "Waiting for answers from other users…"));
    try {
      const data = await apiSend("/api/music/search", "POST", { q: text });
      if (generation !== searchGeneration) return;
      searchId = data.id;
      poll(generation);
    } catch (error) {
      if (generation !== searchGeneration) return;
      pulse.classList.remove("active");
      statusText.textContent = "";
      message(results, error.message);
      searchButton.disabled = false;
    }
  });

  async function poll(generation) {
    if (generation !== searchGeneration || !searchId) return;
    let data;
    try {
      data = await apiFetch(`/api/music/search/${searchId}`, { timeoutMs: 20000 });
    } catch (error) {
      if (generation !== searchGeneration) return;
      pulse.classList.remove("active");
      statusText.textContent = error.message;
      searchButton.disabled = false;
      return;
    }
    if (generation !== searchGeneration) return;
    releases = data.releases || [];
    renderResults(data.complete);
    const summary = t("music.summary", "{releases} folders from {users} users")
      .replace("{releases}", releases.length)
      .replace("{users}", data.response_count || 0);
    if (data.complete) {
      pulse.classList.remove("active");
      statusText.textContent = summary;
      searchButton.disabled = false;
    } else {
      statusText.textContent = `${t("music.searching", "Searching the Soulseek network…")} ${summary}`;
      setTimeout(() => poll(generation), POLL_MS);
    }
  }

  function filtered() {
    return releases.filter(
      (release) => (!filterLossless.checked || release.lossless) && (!filterFree.checked || release.free_slot)
    );
  }

  function renderResults(complete) {
    const items = filtered();
    if (!items.length) {
      if (complete || releases.length) {
        message(results, t("catalog.no_results", "No results found."));
      }
      return;
    }
    results.replaceChildren();
    items.forEach((release) => results.append(releaseCard(release)));
  }

  filterLossless.addEventListener("change", () => renderResults(true));
  filterFree.addEventListener("change", () => renderResults(true));

  function releaseCard(release) {
    const card = document.createElement("div");
    card.className = "poster-card music-card";
    card.tabIndex = 0;
    card.setAttribute("role", "button");

    const cover = document.createElement("div");
    cover.className = "music-cover";
    cover.dataset.release = release.id;
    paintCover(cover, release.cover && release.cover.cached ? release.cover.key : null);
    const badge = document.createElement("span");
    badge.className = `music-badge${release.lossless ? " lossless" : ""}`;
    badge.textContent = release.formats.join(" / ");
    cover.append(badge);
    if (release.free_slot) {
      const slot = document.createElement("span");
      slot.className = "music-slot";
      slot.title = t("music.free_slot", "Free upload slot");
      slot.innerHTML = '<i class="fa-solid fa-bolt" aria-hidden="true"></i>';
      cover.append(slot);
    }

    const info = document.createElement("div");
    info.className = "info";
    const title = document.createElement("div");
    title.className = "title";
    title.title = release.directory;
    title.textContent = release.title;
    const subtitle = document.createElement("div");
    subtitle.className = "subtitle";
    subtitle.textContent = [
      release.parent,
      `${release.file_count} ${t("music.tracks_short", "tracks")}`,
      formatSize(release.size),
    ].filter(Boolean).join(" · ");
    const user = document.createElement("div");
    user.className = "subtitle music-user";
    user.textContent = [release.username, quality(release), speed(release.upload_speed)].filter(Boolean).join(" · ");
    info.append(title, subtitle, user);
    card.append(cover, info);

    const open = () => openRelease(release);
    card.addEventListener("click", open);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        open();
      }
    });
    return card;
  }

  /* ----- covers ----- */

  const COVER_POLL_MS = 1500;
  const COVER_TRIES = 40;

  function coverUrl(key) {
    return `/api/music/cover/${encodeURIComponent(key)}`;
  }

  /* Puts the picture into a cover box, keeping badges; disc icon as fallback. */
  function paintCover(box, key) {
    box.querySelectorAll("img, .music-disc").forEach((node) => node.remove());
    if (key) {
      const image = document.createElement("img");
      image.src = coverUrl(key);
      image.alt = "";
      image.loading = "lazy";
      image.addEventListener("error", () => paintCover(box, null), { once: true });
      box.prepend(image);
    } else {
      const icon = document.createElement("i");
      icon.className = "fa-solid fa-compact-disc music-disc";
      icon.setAttribute("aria-hidden", "true");
      box.prepend(icon);
    }
    box.classList.toggle("has-image", Boolean(key));
  }

  function showReleaseCover(release) {
    const key = release.cover.key;
    release.cover.cached = true;
    if (current === release) paintCover(el("musicModalCover"), key);
    results.querySelectorAll(`.music-cover[data-release="${release.id}"]`).forEach((box) => paintCover(box, key));
  }

  function showing(release) {
    return current === release && el("musicOverlay").classList.contains("open");
  }

  /* Soulseek has no previews: the picture is fetched like any file, then cached. */
  async function loadCover(release) {
    const cover = release.cover;
    if (!cover) return;
    if (cover.cached) {
      showReleaseCover(release);
      return;
    }
    const status = el("musicCoverStatus");
    status.hidden = false;
    for (let attempt = 0; attempt < COVER_TRIES && showing(release); attempt++) {
      try {
        const data = await apiSend("/api/music/cover", "POST", {
          username: release.username,
          filename: cover.filename,
          size: cover.size,
        });
        if (data.state === "ready") {
          if (current === release) status.hidden = true;
          showReleaseCover(release);
          return;
        }
        if (data.state === "failed") break;
      } catch (_error) {
        break;
      }
      await new Promise((resolve) => setTimeout(resolve, COVER_POLL_MS));
    }
    if (current === release) status.hidden = true;
  }

  /* ----- release modal ----- */

  function openRelease(release) {
    current = release;
    paintCover(el("musicModalCover"), null);
    el("musicCoverStatus").hidden = true;
    el("musicModalTitle").textContent = release.title;
    el("musicModalTags").textContent = [release.formats.join(" / "), quality(release)].filter(Boolean).join(" · ");
    el("musicModalMeta").textContent = [
      release.username,
      `${release.file_count} ${t("music.tracks_short", "tracks")}`,
      formatSize(release.size),
      duration(release.length),
      release.free_slot
        ? t("music.free_slot", "Free upload slot")
        : `${t("music.queue", "Queue")}: ${release.queue_length}`,
    ].filter(Boolean).join(" · ");
    el("musicModalPath").textContent = release.directory;

    tracksBox.innerHTML = release.files
      .map((file, index) => `
        <label class="episode-item">
          <input type="checkbox" value="${index}" checked />
          <span class="ep-num">${index + 1}</span>
          <span class="ep-title">${esc(file.name)}</span>
          <span class="music-track-meta">${esc([quality(file), duration(file.length), formatSize(file.size)].filter(Boolean).join(" · "))}</span>
        </label>`)
      .join("");
    selectAll.checked = true;
    openModal("musicOverlay");
    loadCover(release);
  }

  function boxes() {
    return Array.from(tracksBox.querySelectorAll("input[type=checkbox]"));
  }

  selectAll.addEventListener("change", () => {
    boxes().forEach((box) => { box.checked = selectAll.checked; });
  });
  tracksBox.addEventListener("change", () => {
    const all = boxes();
    selectAll.checked = all.length > 0 && all.every((box) => box.checked);
  });

  async function download(onlySelected) {
    if (!current) return;
    const release = current;
    const picked = onlySelected
      ? boxes().filter((box) => box.checked).map((box) => release.files[Number(box.value)])
      : release.files;
    if (!picked.length) {
      showToast(t("music.nothing_selected", "No tracks selected."));
      return;
    }
    const files = picked.map((file) => ({ filename: file.filename, size: file.size }));
    // The album art goes into the folder with the tracks, for Jellyfin/Plex too
    if (release.cover) files.push({ filename: release.cover.filename, size: release.cover.size });
    const buttons = [el("musicDownloadSelected"), el("musicDownloadAll")];
    buttons.forEach((button) => { button.disabled = true; });
    try {
      const data = await apiSend("/api/music/download", "POST", {
        username: release.username,
        files,
      });
      showToast(`${t("music.queued_hint", "Added to the queue")}: ${picked.length} × ${release.title}`);
      closeModal("musicOverlay");
      if (typeof window.refreshQueue === "function") window.refreshQueue();
    } catch (error) {
      showToast(error.message);
    } finally {
      buttons.forEach((button) => { button.disabled = false; });
    }
  }

  el("musicDownloadSelected").addEventListener("click", () => download(true));
  el("musicDownloadAll").addEventListener("click", () => download(false));

  window.addEventListener("resize", moveThumb);
  if (document.fonts) document.fonts.ready.then(moveThumb);
  window.addEventListener("pagehide", forgetSearch);

  /* ----- discover rows ----- */

  function discoverCard(item) {
    const card = document.createElement("div");
    card.className = `poster-card music-card${item.kind === "artist" ? " music-artist" : ""}`;
    card.tabIndex = 0;
    card.setAttribute("role", "button");
    const cover = document.createElement("div");
    cover.className = "music-cover";
    if (item.cover_url) {
      const image = document.createElement("img");
      image.src = item.cover_url;
      image.alt = "";
      image.loading = "lazy";
      image.addEventListener("error", () => paintCover(cover, null), { once: true });
      cover.append(image);
      cover.classList.add("has-image");
    } else {
      paintCover(cover, null);
    }
    const info = document.createElement("div");
    info.className = "info";
    const title = document.createElement("div");
    title.className = "title";
    title.title = item.title;
    title.textContent = item.title;
    const subtitle = document.createElement("div");
    subtitle.className = "subtitle";
    subtitle.textContent = item.kind === "artist"
      ? t("music.artist", "Artist")
      : [item.subtitle, item.year].filter(Boolean).join(" · ");
    info.append(title, subtitle);
    card.append(cover, info);
    const pick = () => {
      if (!available) {
        showToast(t("music.not_configured", "Soulseek is not set up yet"));
        return;
      }
      query.value = item.query;
      form.requestSubmit();
      window.scrollTo({ top: 0, behavior: "smooth" });
    };
    card.addEventListener("click", pick);
    card.addEventListener("keydown", (event) => {
      if (event.key === "Enter" || event.key === " ") {
        event.preventDefault();
        pick();
      }
    });
    return card;
  }

  async function loadRow(section) {
    const grid = section.querySelector(".browse-grid");
    message(grid, t("common.loading", "Loading…"));
    try {
      const data = await apiFetch(`/api/music/discover?row=${encodeURIComponent(section.dataset.row)}`, { timeoutMs: 20000 });
      const items = data.items || [];
      if (!items.length) {
        section.hidden = true;
        return;
      }
      grid.replaceChildren(...items.map(discoverCard));
    } catch (_error) {
      section.hidden = true;
    }
  }

  function showBrowse() {
    searchGeneration++;
    forgetSearch();
    releases = [];
    results.replaceChildren();
    results.hidden = true;
    statusRow.hidden = true;
    pulse.classList.remove("active");
    searchButton.disabled = !available;
    browse.hidden = false;
  }

  query.addEventListener("input", () => {
    if (!query.value.trim()) showBrowse();
  });

  async function init() {
    moveThumb();
    document.body.dataset.site = "soulseek";
    browse.querySelectorAll(".browse-section[data-row]").forEach(loadRow);
    available = await checkStatus();
    searchButton.disabled = !available;
  }

  init();
}());
