/* Book/audio catalog. Remote values only ever reach textContent/attributes. */
(function () {
  const el = (id) => document.getElementById(id);
  let sources = [];
  let paths = {};
  let kind = "ebook";
  let sourceKey = "";
  let current = null;

  function sourceButtons() {
    const available = sources.filter((source) => source.media_kind === kind);
    if (!available.some((source) => source.key === sourceKey)) {
      sourceKey = available.length ? available[0].key : "";
    }
    el("catalogSources").replaceChildren(...available.map((source) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = `catalog-source${source.key === sourceKey ? " active" : ""}`;
      button.dataset.source = source.key;
      button.textContent = source.label;
      return button;
    }));
    el("catalogLibraryPath").textContent = `${t("catalog.library", "Library")}: ${paths[kind] || "–"}`;
  }

  async function init() {
    try {
      const data = await apiFetch("/api/catalog/sources");
      sources = data.sources || [];
      paths = data.library_paths || {};
      sourceButtons();
      el("catalogEmpty").hidden = false;
    } catch (error) {
      showToast(error.message);
    }
  }

  el("catalogKinds").addEventListener("click", (event) => {
    const button = event.target.closest("[data-kind]");
    if (!button) return;
    kind = button.dataset.kind;
    el("catalogKinds").querySelectorAll("[data-kind]").forEach((candidate) => {
      candidate.classList.toggle("active", candidate === button);
    });
    sourceButtons();
    el("catalogResults").replaceChildren();
    el("catalogEmpty").hidden = false;
  });

  el("catalogSources").addEventListener("click", (event) => {
    const button = event.target.closest("[data-source]");
    if (!button) return;
    sourceKey = button.dataset.source;
    sourceButtons();
  });

  el("catalogSearchForm").addEventListener("submit", async (event) => {
    event.preventDefault();
    const query = el("catalogQuery").value.trim();
    if (query.length < 2 || !sourceKey) return;
    el("catalogResults").replaceChildren();
    el("catalogEmpty").hidden = true;
    try {
      const data = await apiFetch(`/api/catalog/search?source=${encodeURIComponent(sourceKey)}&q=${encodeURIComponent(query)}`);
      renderResults(data.items || []);
    } catch (error) {
      showToast(error.message);
      el("catalogEmpty").hidden = false;
    }
  });

  function renderResults(items) {
    el("catalogEmpty").hidden = items.length > 0;
    items.forEach((item) => {
      const card = document.createElement("button");
      card.type = "button";
      card.className = "catalog-card";
      const cover = document.createElement("div");
      cover.className = "catalog-card-cover";
      if (item.cover_url) {
        const image = document.createElement("img");
        image.src = item.cover_url;
        image.alt = "";
        image.loading = "lazy";
        image.referrerPolicy = "no-referrer";
        image.addEventListener("error", () => {
          image.remove();
          const icon = document.createElement("i");
          icon.className = "fa-solid fa-book-open";
          icon.setAttribute("aria-hidden", "true");
          cover.append(icon);
        }, { once: true });
        cover.append(image);
      } else {
        cover.innerHTML = '<i class="fa-solid fa-book-open" aria-hidden="true"></i>';
      }
      const title = document.createElement("strong");
      title.textContent = item.title;
      const meta = document.createElement("span");
      meta.textContent = [item.author, item.language].filter(Boolean).join(" · ");
      card.append(cover, title, meta);
      card.addEventListener("click", () => openDetails(item));
      el("catalogResults").append(card);
    });
  }

  async function openDetails(item) {
    try {
      current = await apiFetch(`/api/catalog/details?source=${encodeURIComponent(item.source)}&id=${encodeURIComponent(item.id)}`);
      el("catalogModalTitle").textContent = current.title || "";
      el("catalogMeta").textContent = [current.author, current.year, current.language].filter(Boolean).join(" · ");
      el("catalogDescription").textContent = current.description || t("catalog.no_description", "No description available.");
      el("catalogTarget").textContent = current.target_path || t("catalog.no_asset", "No downloadable file found");
      const cover = el("catalogCover");
      cover.hidden = !current.cover_url;
      cover.src = current.cover_url || "";
      cover.referrerPolicy = "no-referrer";
      cover.onerror = () => {
        cover.hidden = true;
        cover.removeAttribute("src");
      };
      renderAssets(current.assets || []);
      const modal = el("catalogModal");
      modal.hidden = false;
      modal.classList.add("open");
      document.body.dataset.modal = "open";
    } catch (error) {
      showToast(error.message);
    }
  }

  function renderAssets(assets) {
    const box = el("catalogAssets");
    box.replaceChildren();
    assets.forEach((asset) => {
      const row = document.createElement("div");
      row.className = "catalog-asset";
      const label = document.createElement("span");
      const assetInfo = document.createElement("span");
      assetInfo.className = "catalog-asset-info";
      label.textContent = asset.label || asset.extension.toUpperCase();
      const target = document.createElement("code");
      target.textContent = asset.target_path || "";
      assetInfo.append(label, target);
      const button = document.createElement("button");
      button.type = "button";
      button.className = "btn btn-primary";
      button.textContent = t("catalog.download", "Download");
      button.addEventListener("click", () => queueAsset(asset, button));
      row.append(assetInfo, button);
      box.append(row);
    });
    if (!assets.length) {
      box.textContent = t("catalog.no_asset", "No downloadable file found");
    }
  }

  async function queueAsset(asset, button) {
    button.disabled = true;
    try {
      const data = await apiSend("/api/catalog/download", "POST", {
        source: current.source,
        id: current.id,
        asset_id: asset.id
      });
      showToast(`${t("catalog.queued", "Queued")}: ${data.target_path || current.title}`);
      closeModal();
    } catch (error) {
      showToast(error.message);
      button.disabled = false;
    }
  }

  function closeModal() {
    const modal = el("catalogModal");
    modal.classList.remove("open");
    modal.hidden = true;
    document.body.dataset.modal = "closed";
  }
  el("catalogModalClose").addEventListener("click", closeModal);
  el("catalogModal").addEventListener("click", (event) => {
    if (event.target === el("catalogModal")) closeModal();
  });
  window.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeModal();
  });

  init();
}());
