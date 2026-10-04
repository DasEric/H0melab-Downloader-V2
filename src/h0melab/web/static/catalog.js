/* Book/audio catalog using the same navigation, cards and modal language as home.js. */
(function () {
  const el = (id) => document.getElementById(id);
  const sourceTrack = el("catalogSourceTrack");
  const sourceThumb = el("catalogSourceThumb");
  const query = el("catalogQuery");
  const searchButton = el("catalogSearchBtn");
  const randomButton = el("catalogRandomBtn");
  const spinner = el("catalogSpinner");
  const results = el("catalogResults");
  const browse = el("catalogBrowse");
  const browseGrid = el("catalogBrowseGrid");
  const browseHeading = el("catalogBrowseHeading");
  const modal = el("catalogModal");
  const modalLoading = el("catalogLoading");
  const modalContent = el("catalogContent");
  const formatSelect = el("catalogFormat");
  const downloadButton = el("catalogDownloadBtn");

  let sources = [];
  let sourceKey = "";
  let current = null;

  const kindHeading = {
    ebook: ["catalog.browse_ebooks", "Discover eBooks"],
    audiobook: ["catalog.browse_audiobooks", "Discover audio books"],
    podcast: ["catalog.browse_podcasts", "Discover podcasts"]
  };

  const languageNames = {
    de: ["catalog.language_de", "German"],
    deu: ["catalog.language_de", "German"],
    ger: ["catalog.language_de", "German"],
    en: ["catalog.language_en", "English"],
    eng: ["catalog.language_en", "English"],
    "en-gb": ["catalog.language_en", "English"],
    "en-us": ["catalog.language_en", "English"]
  };

  function activeSource() {
    return sources.find((source) => source.key === sourceKey) || null;
  }

  function languageLabel(value) {
    const key = String(value || "").trim().toLowerCase();
    const entry = languageNames[key];
    return entry ? t(entry[0], entry[1]) : (value || t("catalog.language_unknown", "Unknown"));
  }

  function browseLabel() {
    const source = activeSource();
    const entry = kindHeading[source ? source.media_kind : "ebook"] || kindHeading.ebook;
    return t(entry[0], entry[1]);
  }

  function setBusy(busy) {
    spinner.classList.toggle("active", busy);
    searchButton.disabled = busy;
    randomButton.disabled = busy;
  }

  function moveThumb() {
    const active = sourceTrack.querySelector(".segmented-btn.active");
    if (!active) {
      sourceThumb.style.width = "0";
      return;
    }
    const trackRect = sourceTrack.getBoundingClientRect();
    const activeRect = active.getBoundingClientRect();
    sourceThumb.style.width = `${activeRect.width}px`;
    sourceThumb.style.transform = `translateX(${activeRect.left - trackRect.left + sourceTrack.scrollLeft}px)`;
  }

  function renderSourceButtons() {
    sourceTrack.querySelectorAll(".segmented-btn").forEach((button) => button.remove());
    sources.forEach((source) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "segmented-btn";
      button.dataset.source = source.key;
      button.setAttribute("role", "tab");
      button.textContent = source.label;
      button.addEventListener("click", () => switchSource(source.key));
      sourceTrack.append(button);
    });
  }

  async function switchSource(nextSource) {
    sourceKey = nextSource;
    document.body.dataset.site = sourceKey;
    sourceTrack.querySelectorAll(".segmented-btn").forEach((button) => {
      const active = button.dataset.source === sourceKey;
      button.classList.toggle("active", active);
      button.setAttribute("aria-selected", String(active));
    });
    moveThumb();
    query.value = "";
    results.replaceChildren();
    results.hidden = true;
    browse.hidden = false;
    browseHeading.textContent = browseLabel();
    await loadBrowse();
  }

  async function init() {
    try {
      const data = await apiFetch("/api/catalog/sources");
      sources = data.sources || [];
      renderSourceButtons();
      const preferred = sources.find((source) => source.key === "standard_ebooks") || sources[0];
      if (preferred) await switchSource(preferred.key);
    } catch (_error) {
      renderMessage(browseGrid, t("catalog.error_unavailable", "Catalog currently unavailable."));
    }
  }

  async function loadBrowse() {
    const requestedSource = sourceKey;
    setBusy(true);
    renderMessage(browseGrid, t("common.loading", "Loading…"));
    try {
      const data = await apiFetch(`/api/catalog/browse?source=${encodeURIComponent(requestedSource)}`);
      if (sourceKey !== requestedSource) return;
      renderCards(browseGrid, data.items || []);
    } catch (_error) {
      if (sourceKey !== requestedSource) return;
      renderMessage(browseGrid, t("catalog.error_unavailable", "Catalog currently unavailable."));
    } finally {
      if (sourceKey === requestedSource) setBusy(false);
    }
  }

  function renderMessage(container, message) {
    container.replaceChildren();
    const empty = document.createElement("div");
    empty.className = "empty-state";
    empty.textContent = message;
    container.append(empty);
  }

  function coverFallback(container) {
    container.replaceChildren();
    const fallback = document.createElement("div");
    fallback.className = "catalog-poster-fallback";
    const icon = document.createElement("i");
    icon.className = "fa-solid fa-book-open";
    icon.setAttribute("aria-hidden", "true");
    fallback.append(icon);
    container.append(fallback);
  }

  async function loadMissingCover(item, poster) {
    try {
      const details = await apiFetch(`/api/catalog/details?source=${encodeURIComponent(item.source)}&id=${encodeURIComponent(item.id)}`);
      if (!poster.isConnected || !details.cover_url) return;
      const image = document.createElement("img");
      image.src = details.cover_url;
      image.alt = "";
      image.loading = "lazy";
      image.referrerPolicy = "no-referrer";
      image.addEventListener("error", () => coverFallback(poster), { once: true });
      poster.replaceChildren(image);
    } catch (_error) {
      /* The placeholder is the complete fallback for missing detail images. */
    }
  }

  function renderCards(container, items) {
    container.replaceChildren();
    if (!items.length) {
      renderMessage(container, t("catalog.no_results", "No results found."));
      return;
    }
    items.forEach((item) => {
      const card = document.createElement("div");
      card.className = "poster-card";
      card.tabIndex = 0;
      card.setAttribute("role", "button");

      const poster = document.createElement("div");
      poster.className = "catalog-poster";
      if (item.cover_url) {
        const image = document.createElement("img");
        image.src = item.cover_url;
        image.alt = "";
        image.loading = "lazy";
        image.referrerPolicy = "no-referrer";
        image.addEventListener("error", () => coverFallback(poster), { once: true });
        poster.append(image);
      } else {
        coverFallback(poster);
        loadMissingCover(item, poster);
      }

      const info = document.createElement("div");
      info.className = "info";
      const title = document.createElement("div");
      title.className = "title";
      title.title = item.title || "";
      title.textContent = item.title || t("catalog.unknown_title", "Unknown title");
      const subtitle = document.createElement("div");
      subtitle.className = "subtitle";
      subtitle.textContent = [item.author, item.language ? languageLabel(item.language) : ""]
        .filter(Boolean)
        .join(" · ");
      info.append(title, subtitle);
      card.append(poster, info);

      const open = () => openDetails(item);
      card.addEventListener("click", open);
      card.addEventListener("keydown", (event) => {
        if (event.key === "Enter" || event.key === " ") {
          event.preventDefault();
          open();
        }
      });
      container.append(card);
    });
  }

  el("catalogSearchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const keyword = query.value.trim();
    if (!keyword) {
      results.hidden = true;
      browse.hidden = false;
      return;
    }
    if (keyword.length < 2) {
      showToast(t("catalog.search_too_short", "Please enter at least two characters."));
      return;
    }
    setBusy(true);
    browse.hidden = true;
    results.hidden = false;
    renderMessage(results, t("index.searching", "Searching…"));
    try {
      const data = await apiFetch(`/api/catalog/search?source=${encodeURIComponent(sourceKey)}&q=${encodeURIComponent(keyword)}`);
      renderCards(results, data.items || []);
    } catch (_error) {
      renderMessage(results, t("catalog.error_unavailable", "Catalog currently unavailable."));
    } finally {
      setBusy(false);
    }
  });

  randomButton.addEventListener("click", async () => {
    setBusy(true);
    try {
      const data = await apiFetch(`/api/catalog/random?source=${encodeURIComponent(sourceKey)}`);
      if (data.item) await openDetails(data.item);
    } catch (_error) {
      showToast(t("catalog.error_unavailable", "Catalog currently unavailable."));
    } finally {
      setBusy(false);
    }
  });

  async function openDetails(item) {
    modal.hidden = false;
    modal.classList.add("open");
    document.body.dataset.modal = "open";
    modalLoading.hidden = false;
    modalContent.hidden = true;
    try {
      current = await apiFetch(`/api/catalog/details?source=${encodeURIComponent(item.source)}&id=${encodeURIComponent(item.id)}`);
      el("catalogModalTitle").textContent = current.title || "";
      el("catalogMeta").textContent = [current.author, current.year].filter(Boolean).join(" · ");
      el("catalogDescription").textContent = current.description || t("catalog.no_description", "No description available.");
      el("catalogLanguage").value = languageLabel(current.language);
      renderFormats(current.assets || []);
      renderModalCover(current.cover_url || "");
      modalLoading.hidden = true;
      modalContent.hidden = false;
    } catch (_error) {
      closeModal();
      showToast(t("catalog.error_details", "Details could not be loaded."));
    }
  }

  function renderModalCover(url) {
    const image = el("catalogCover");
    const fallback = el("catalogCoverFallback");
    image.hidden = !url;
    fallback.hidden = Boolean(url);
    image.removeAttribute("src");
    if (!url) return;
    image.referrerPolicy = "no-referrer";
    image.onerror = () => {
      image.hidden = true;
      fallback.hidden = false;
      image.removeAttribute("src");
    };
    image.src = url;
  }

  function renderFormats(assets) {
    formatSelect.replaceChildren();
    assets.forEach((asset) => {
      const option = document.createElement("option");
      option.value = asset.id;
      option.textContent = assetLabel(asset);
      option.dataset.target = asset.target_path || "";
      formatSelect.append(option);
    });
    formatSelect.disabled = !assets.length;
    downloadButton.disabled = !assets.length;
    updateTarget();
  }

  function assetLabel(asset) {
    const sourceLabel = String(asset.label || "").toLowerCase();
    if (sourceLabel.includes("compatible epub")) {
      return t("catalog.format_compatible_epub", "EPUB (compatible)");
    }
    if (sourceLabel.includes("advanced epub")) {
      return t("catalog.format_advanced_epub", "EPUB (advanced)");
    }
    if (sourceLabel.includes("kepub")) return "KEPUB";
    if (asset.title) return asset.title;
    return asset.extension ? asset.extension.toUpperCase() : asset.label;
  }

  function selectedAsset() {
    if (!current) return null;
    return (current.assets || []).find((asset) => asset.id === formatSelect.value) || null;
  }

  function updateTarget() {
    const asset = selectedAsset();
    el("catalogTarget").textContent = asset
      ? asset.target_path
      : t("catalog.no_asset", "No downloadable file found");
  }

  formatSelect.addEventListener("change", updateTarget);

  downloadButton.addEventListener("click", async () => {
    const asset = selectedAsset();
    if (!asset || !current) return;
    downloadButton.disabled = true;
    try {
      const data = await apiSend("/api/catalog/download", "POST", {
        source: current.source,
        id: current.id,
        asset_id: asset.id
      });
      showToast(`${t("catalog.queued", "Queued")}: ${data.target_path || current.title}`);
      closeModal();
    } catch (_error) {
      showToast(t("catalog.error_download", "Download could not be queued."));
      downloadButton.disabled = false;
    }
  });

  function closeModal() {
    modal.classList.remove("open");
    modal.hidden = true;
    modalLoading.hidden = false;
    modalContent.hidden = true;
    document.body.dataset.modal = "closed";
  }

  el("catalogModalClose").addEventListener("click", closeModal);
  modal.addEventListener("click", (event) => {
    if (event.target === modal) closeModal();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape" && !modal.hidden) closeModal();
  });
  window.addEventListener("resize", moveThumb);
  if (document.fonts) document.fonts.ready.then(moveThumb);

  init();
}());
