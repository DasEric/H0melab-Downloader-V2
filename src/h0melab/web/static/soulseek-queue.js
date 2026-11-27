/* Soulseek downloads on the queue page.
 *
 * They run inside the Soulseek service, not in the downloader's own job
 * table, so they are polled from it and shown as their own block above the regular rows. The
 * page's status chips and search apply to them as well.
 */
(function () {
  const section = document.getElementById("soulseekQueue");
  const list = document.getElementById("soulseekQueueList");
  if (!section || !list) return;

  const filters = document.getElementById("queueFilters");
  const searchInput = document.getElementById("queueSearch");
  const clearBtn = document.getElementById("clearCompletedBtn");
  const POLL = 2500;

  const STATE_LABELS = {
    running: ["music.state_running", "Downloading"],
    queued: ["music.state_queued", "Queued"],
    completed: ["music.state_completed", "Completed"],
    failed: ["music.state_failed", "Failed"],
    cancelled: ["music.state_cancelled", "Cancelled"],
  };
  // Which queue filter chip a Soulseek state belongs to
  const CHIP = { running: "active", queued: "active", completed: "completed", failed: "failed", cancelled: "cancelled" };

  const expanded = new Set();
  let groups = [];
  let timer = null;
  let enabled = false;

  function stateLabel(state) {
    const entry = STATE_LABELS[state] || STATE_LABELS.queued;
    return t(entry[0], entry[1]);
  }

  function speed(bytesPerSecond) {
    return bytesPerSecond ? `${formatSize(bytesPerSecond)}/s` : "";
  }

  function chipStatus() {
    const active = filters && filters.querySelector(".chip.is-active");
    return active ? active.dataset.status || "" : "";
  }

  function visible() {
    const status = chipStatus();
    const needle = (searchInput ? searchInput.value : "").trim().toLowerCase();
    return groups.filter((group) => {
      if (status && CHIP[group.state] !== status) return false;
      if (needle && !`${group.title} ${group.username}`.toLowerCase().includes(needle)) return false;
      return true;
    });
  }

  function item(group) {
    const node = document.createElement("div");
    node.className = "queue-item music-transfer";
    const key = `${group.username}|${group.directory}`;
    const open = expanded.has(key);
    const active = group.state === "running" || group.state === "queued";
    const meta = [
      "Soulseek",
      group.username,
      `${group.completed}/${group.files.length} ${t("music.tracks_short", "tracks")}`,
      `${formatSize(group.transferred)} / ${formatSize(group.size)}`,
      group.state === "running" ? speed(group.speed) : "",
    ].filter(Boolean).join(" · ");
    const thumb = group.cover
      ? `<img class="music-thumb" src="/api/music/cover/${encodeURIComponent(group.cover)}" alt="" />`
      : '<span class="music-thumb"><i class="fa-solid fa-compact-disc" aria-hidden="true"></i></span>';
    node.innerHTML = `
      <div class="queue-item-head">
        ${thumb}
        <div class="music-transfer-text">
          <div class="queue-item-title">${esc(group.title)}</div>
          <div class="queue-item-meta">${esc(meta)}</div>
        </div>
        <div class="queue-item-actions">
          <span class="status-pill status-${group.state}">${esc(stateLabel(group.state))}</span>
          <button class="btn btn-ghost music-icon-btn" type="button" data-action="toggle"
            title="${esc(t("music.show_files", "Show files"))}"><i class="fa-solid fa-chevron-${open ? "up" : "down"}" aria-hidden="true"></i></button>
          <button class="btn btn-ghost music-icon-btn" type="button" data-action="${active ? "cancel" : "remove"}"
            title="${esc(active ? t("music.cancel", "Cancel") : t("music.remove", "Remove"))}"><i class="fa-solid fa-${active ? "xmark" : "trash"}" aria-hidden="true"></i></button>
        </div>
      </div>
      <div class="progress-track"><div class="progress-fill" style="width:${Math.min(100, group.percent)}%"></div></div>`;

    if (open) {
      const files = document.createElement("div");
      files.className = "music-transfer-files";
      files.innerHTML = group.files
        .map((file) => `
          <div class="music-transfer-file">
            <span class="ep-title">${esc(file.name)}</span>
            <span class="music-track-meta">${esc(
              file.state === "running"
                ? `${Math.round(file.percent)}% · ${speed(file.speed)}`
                : file.state === "queued" && file.queue_position
                  ? `${stateLabel("queued")} #${file.queue_position}`
                  : stateLabel(file.state)
            )}</span>
          </div>`)
        .join("");
      node.append(files);
    }

    node.querySelector('[data-action="toggle"]').addEventListener("click", () => {
      if (expanded.has(key)) expanded.delete(key);
      else expanded.add(key);
      render();
    });
    node.querySelector('[data-action="cancel"], [data-action="remove"]').addEventListener("click", async () => {
      const remove = !active;
      const ids = group.files
        .filter((file) => remove || file.state === "running" || file.state === "queued")
        .map((file) => file.id);
      try {
        await apiSend("/api/music/transfers/cancel", "POST", { username: group.username, ids, remove });
        load();
      } catch (error) {
        showToast(error.message);
      }
    });
    return node;
  }

  function render() {
    const shown = visible();
    section.hidden = shown.length === 0;
    // The regular list's "queue is empty" line would contradict these rows
    document.body.classList.toggle("has-soulseek-queue", shown.length > 0);
    list.replaceChildren(...shown.map(item));
  }

  async function load() {
    clearTimeout(timer);
    if (!enabled) return;
    try {
      const data = await apiFetch("/api/music/transfers", { timeoutMs: 15000 });
      groups = data.transfers || [];
      render();
    } catch (_error) {
      // Service away: keep the last known rows rather than flashing an error
    }
    if (!document.hidden) timer = setTimeout(load, POLL);
  }

  if (filters) filters.addEventListener("click", () => setTimeout(render, 0));
  if (searchInput) searchInput.addEventListener("input", render);
  if (clearBtn) {
    clearBtn.addEventListener("click", async () => {
      if (!enabled) return;
      try {
        await apiSend("/api/music/transfers/clear", "POST", {});
        load();
      } catch (_error) {
        /* the regular queue reports its own result */
      }
    });
  }
  document.addEventListener("visibilitychange", () => {
    if (!document.hidden) load();
  });

  // Only installs that use Soulseek; while the client starts up, ask again
  async function connect() {
    try {
      const data = await apiFetch("/api/music/status", { timeoutMs: 15000 });
      if (!data.configured) return;
      if (data.phase === "running") {
        enabled = true;
        load();
        return;
      }
    } catch (_error) {
      /* try again below */
    }
    setTimeout(connect, 5000);
  }

  connect();
}());
