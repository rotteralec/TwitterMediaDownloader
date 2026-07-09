(() => {
  const form = document.getElementById("form");
  const input = document.getElementById("url-input");
  const pasteBtn = document.getElementById("paste-btn");
  const clearBtn = document.getElementById("clear-btn");
  const submitBtn = document.getElementById("submit-btn");
  const quotaEl = document.getElementById("quota");
  const errorEl = document.getElementById("error");
  const resultsEl = document.getElementById("results");
  const tpl = document.getElementById("result-tpl");
  const ghLink = document.getElementById("gh-link");

  let status = { owner: false, daily_limit: null, remaining: null, github_url: "#" };

  // --- Status -------------------------------------------------------------
  async function loadStatus() {
    try {
      const r = await fetch("/api/status", { cache: "no-store" });
      if (r.ok) status = await r.json();
    } catch (_) { /* offline — ignore */ }
    ghLink.href = status.github_url || "#";
    renderQuota();
  }

  function renderQuota() {
    if (status.owner) {
      quotaEl.hidden = false;
      quotaEl.innerHTML = `<strong>Owner mode</strong> — unlimited downloads.`;
      return;
    }
    if (status.remaining == null || status.daily_limit == null) {
      quotaEl.hidden = true;
      return;
    }
    quotaEl.hidden = false;
    if (status.remaining <= 0) {
      quotaEl.textContent = `Daily demo limit reached (${status.daily_limit}/day). `;
      const a = document.createElement("a");
      a.href = status.github_url || "#";
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "Host your own";
      quotaEl.append(a, " for unlimited use.");
    } else {
      quotaEl.innerHTML =
        `<strong>${status.remaining}</strong> of ${status.daily_limit} free downloads left today.`;
    }
  }

  function canShareFiles() {
    if (!navigator.canShare) return false;
    try {
      return navigator.canShare({ files: [new File(["x"], "probe.gif", { type: "image/"})]})
    } catch { return false; }
  }
  const CAN_SHARE = canShareFiles();

async function shareMedia(downloadUrl, filename, mimeType, btn) {
  if (btn && btn.classList.contains("busy")) return;
  const lbl = btn && btn.querySelector(".lbl");
  const prev = lbl && lbl.textContent;
  if (btn) { btn.classList.add("busy"); if (lbl) lbl.textContent = "Preparing…"; }
  try {
    const res = await fetch(downloadUrl);
    if (!res.ok) throw new Error(`fetch ${res.status}`);
    const blob = await res.blob();
    const file = new File([blob], filename, { type: mimeType || blob.type });
    if (navigator.canShare && !navigator.canShare({ files: [file] })) {
      throw new Error("unshareable");
    }
    await navigator.share({ files: [file] });
  } catch (err) {
    if (err && err.name === "AbortError") return;   // user dismissed the share sheet
    setError("Couldn't share that — you can still download it.");
  } finally {
    if (btn) { btn.classList.remove("busy"); if (lbl) lbl.textContent = prev; }
  }
}

  // --- Paste --------------------------------------------------------------
  pasteBtn.addEventListener("click", async () => {
    try {
      const text = await navigator.clipboard.readText();
      if (text) {
        input.value = text.trim();
        toggleClear();
        input.focus();
      }
    } catch (_) {
      // Clipboard API blocked (e.g. iOS without user gesture). Fall back to focusing.
      input.focus();
      input.select();
    }
  });

  // --- Clear (×) ----------------------------------------------------------
  // Show the × only when there's text; clicking it empties the field.
  function toggleClear() {
    clearBtn.hidden = !input.value;
  }
  input.addEventListener("input", toggleClear);
  clearBtn.addEventListener("click", () => {
    input.value = "";
    toggleClear();
    setError(null);
    input.focus();
  });

  // --- Submit -------------------------------------------------------------
  form.addEventListener("submit", async (e) => {
    e.preventDefault();
    const url = input.value.trim();
    if (!url) return;

    setError(null);
    resultsEl.hidden = true;
    resultsEl.innerHTML = "";
    setLoading(true);

    try {
      const r = await fetch("/api/extract", {
        method: "POST",
        headers: { "Content-Type": "application/json", Accept: "application/json" },
        body: JSON.stringify({ url }),
      });
      const data = await r.json().catch(() => ({}));

      if (!r.ok) {
        if (r.status === 429) {
          // Server says we're out for the day.
          status.remaining = 0;
          renderQuota();
        }
        setError(data.message || `Request failed (${r.status}).`, data.github_url);
        return;
      }

      // Update quota from response if present.
      if (typeof data.remaining === "number" || data.owner === true) {
        status.owner = !!data.owner;
        status.remaining = data.owner ? null : data.remaining;
        renderQuota();
      }

      renderResults(data);
    } catch (err) {
      setError("Network error. Check your connection and try again.");
    } finally {
      setLoading(false);
    }
  });

  function setLoading(on) {
    submitBtn.disabled = on;
    submitBtn.classList.toggle("is-loading", on);
    submitBtn.querySelector(".btn-label").textContent = on ? "Working" : "Download";
  }

  function setError(msg, githubUrl) {
    if (!msg) {
      errorEl.hidden = true;
      errorEl.textContent = "";
      return;
    }
    errorEl.hidden = false;
    errorEl.innerHTML = "";
    errorEl.append(document.createTextNode(msg));
    if (githubUrl) {
      errorEl.append(" ");
      const a = document.createElement("a");
      a.href = githubUrl;
      a.target = "_blank";
      a.rel = "noopener";
      a.textContent = "Host your own →";
      errorEl.append(a);
    }
  }

  // --- Render results -----------------------------------------------------
  function renderResults(data) {
    const media = data.media || [];
    if (media.length === 0) {
      setError("No video or GIF found in that tweet.");
      return;
    }
    resultsEl.hidden = false;
    media.forEach((item, idx) => {
      const node = tpl.content.cloneNode(true);
      const root = node.querySelector(".result");
      const img = node.querySelector("img");
      const title = node.querySelector(".title");
      const uploader = node.querySelector(".uploader");
      const qs = node.querySelector(".qualities");

      if (item.thumbnail) {
        img.src = item.thumbnail;
      } else {
        node.querySelector(".thumb").style.display = "none";
        root.style.gridTemplateColumns = "1fr";
      }

      const baseTitle = media.length > 1
        ? `${data.title || "Twitter"} (${idx + 1}/${media.length})`
        : (item.title || data.title || "Twitter");

      title.textContent = baseTitle;
      if (item.kind === "gif") {
        const badge = document.createElement("span");
        badge.className = "badge";
        badge.textContent = "GIF";
        title.prepend(badge);
      }
      uploader.textContent = data.uploader ? `@${data.uploader.replace(/^@/, "")}` : "";

      if (item.kind === "gif") {
        renderGif(qs, item, baseTitle);
      } else {
        renderVideo(qs, item, baseTitle);
      }

      resultsEl.appendChild(node);
    });
  }

  function renderVideo(container, item, baseTitle) {
  (item.formats || []).forEach((f) => {
    const a = document.createElement("a");
    a.className = "q-btn";
    a.href = buildDownloadUrl(f.url, `${baseTitle}_${f.quality}`, "mp4");
    a.rel = "noopener";
    a.innerHTML = `${escapeHtml(f.quality)}` +
      (f.filesize_human ? ` <span class="sz">${escapeHtml(f.filesize_human)}</span>` : "");
    container.appendChild(a);
  });

  if (CAN_SHARE && item.formats && item.formats.length) {
    const best = item.formats[0];   // formats are sorted highest-quality first
    const url = buildDownloadUrl(best.url, `${baseTitle}_${best.quality}`, "mp4");
    const share = document.createElement("button");
    share.type = "button";
    share.className = "q-btn share-btn";
    share.innerHTML = `<span class="lbl">Share</span>`;
    share.addEventListener("click", () => {
      shareMedia(url, sanitize(baseTitle) + ".mp4", "video/mp4", share);
    });
    container.appendChild(share);
  }
}

  function renderGif(container, item, baseTitle) {
  const src = item.source || {};
  const dlUrl = buildDownloadUrl(src.url, baseTitle, "gif");

  const a = document.createElement("a");
  a.className = "q-btn gif-btn";
  a.href = dlUrl;
  a.rel = "noopener";
  a.innerHTML = `<span class="lbl">Download GIF</span>`;
  container.appendChild(a);

  if (CAN_SHARE) {
    const share = document.createElement("button");
    share.type = "button";
    share.className = "q-btn gif-btn share-btn";
    share.innerHTML = `<span class="lbl">Share GIF</span>`;
    share.addEventListener("click", () => {
      shareMedia(dlUrl, sanitize(baseTitle) + ".gif", "image/gif", share);
    });
    container.appendChild(share);
  }

  const note = document.createElement("p");
  note.className = "gif-note";
  note.textContent = "Converted to a real .gif on the fly — may take a few seconds.";
  container.appendChild(note);

  a.addEventListener("click", () => {
    if (a.classList.contains("busy")) return;
    a.classList.add("busy");
    const lbl = a.querySelector(".lbl");
    const prev = lbl.textContent;
    lbl.textContent = "Converting…";
    setTimeout(() => {
      a.classList.remove("busy");
      lbl.textContent = prev;
    }, 10000);
  });
}

  function buildDownloadUrl(cdnUrl, filenameBase, fmt) {
    const params = new URLSearchParams({
      url: cdnUrl,
      filename: sanitize(filenameBase || "twitter"),
      fmt: fmt || "mp4",
    });
    return `/api/download?${params.toString()}`;
  }

  function sanitize(s) {
    return s.replace(/[^A-Za-z0-9._-]+/g, "_").replace(/^_+|_+$/g, "").slice(0, 60);
  }
  function escapeHtml(s) {
    return String(s).replace(/[&<>"']/g, (c) => ({
      "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
    }[c]));
  }

  toggleClear();
  loadStatus();
})();
