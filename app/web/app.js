/* Clipforge front-end.
 *
 * Deliberately dependency-free: no build step, no node_modules, nothing to
 * install. The whole UI is driven by GET /api/config, so colours, fonts,
 * presets and backend availability all come from the server rather than being
 * duplicated here.
 */

(() => {
  "use strict";

  const $ = (id) => document.getElementById(id);
  const api = (path) => path; // same-origin; swap for API_BASE_URL when split out

  const state = {
    config: null,
    source: null,
    media: null,
    highlight: new Set(),
    words: [],
    color: "#8B5CF6",
    colorName: "Electric Violet",
    outputPreset: "1080p30",
    keyframes: [],
    job: null,
    poll: null,
    loadedFonts: new Set(),
    upload: null,
  };

  const STAGES = [
    ["analyzing", "Analyzing video"],
    ["extracting", "Extracting clip"],
    ["detecting_silence", "Detecting speech"],
    ["removing_silence", "Removing silence"],
    ["transcribing", "Generating captions"],
    ["building_captions", "Grouping caption phrases"],
    ["rendering_hook", "Rendering hook"],
    ["compositing", "Compositing square viewport"],
    ["encoding", "Rendering final video"],
    ["finalizing", "Finalizing"],
  ];

  /* ------------------------------------------------------------------ *
   * helpers
   * ------------------------------------------------------------------ */

  function toast(message, isError) {
    const el = $("toast");
    el.textContent = message;
    el.classList.toggle("error", !!isError);
    el.classList.remove("hidden");
    clearTimeout(el._timer);
    el._timer = setTimeout(() => el.classList.add("hidden"), isError ? 9000 : 3500);
  }

  function bytes(n) {
    if (!n && n !== 0) return "–";
    const units = ["B", "KB", "MB", "GB"];
    let i = 0;
    let v = n;
    while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1; }
    return `${v.toFixed(v < 10 && i > 0 ? 1 : 0)} ${units[i]}`;
  }

  function clock(seconds) {
    if (!seconds && seconds !== 0) return "–";
    const total = Math.round(seconds);
    const h = Math.floor(total / 3600);
    const m = Math.floor((total % 3600) / 60);
    const s = total % 60;
    const mm = String(m).padStart(h ? 2 : 1, "0");
    return `${h ? h + ":" : ""}${mm}:${String(s).padStart(2, "0")}`;
  }

  const sleep = (ms) => new Promise((resolve) => setTimeout(resolve, ms));

  async function request(path, options) {
    const response = await fetch(api(path), options);
    const text = await response.text();
    let data = {};
    if (text) {
      try { data = JSON.parse(text); } catch (_) { data = {}; }
    }
    if (!response.ok) {
      throw new Error((data && data.message) || `Request failed (HTTP ${response.status})`);
    }
    return data;
  }

  const postJSON = (path, body) =>
    request(path, {
      method: "POST",
      headers: { "Content-Type": "application/json" },
      body: JSON.stringify(body),
    });

  /* ------------------------------------------------------------------ *
   * boot
   * ------------------------------------------------------------------ */

  async function boot() {
    wireEvents();
    try {
      const health = await request("/api/health");
      const pill = $("health");
      const ok = health.ffmpeg && health.ffmpeg.available;
      pill.textContent = ok ? "FFmpeg ready" : "FFmpeg missing";
      pill.className = `pill ${ok ? "pill-ok" : "pill-bad"}`;
    } catch (err) {
      $("health").textContent = "offline";
      $("health").className = "pill pill-bad";
    }

    try {
      state.config = await request("/api/config");
    } catch (err) {
      toast(`Could not load settings: ${err.message}`, true);
      return;
    }

    renderSwatches();
    renderBackends();
    renderOutputPresets();
    renderFonts();
    renderPresets();
    applyPreset(state.config.presets.find((p) => p.id === "my-default") || state.config.presets[0]);
    renderPreview();
  }

  /* ------------------------------------------------------------------ *
   * config-driven UI
   * ------------------------------------------------------------------ */

  function renderSwatches() {
    const host = $("color-swatches");
    host.innerHTML = "";
    state.config.colors.forEach((color) => {
      const button = document.createElement("button");
      button.type = "button";
      button.className = "swatch";
      button.dataset.hex = color.hex;
      button.innerHTML =
        `<span class="swatch-dot" style="background:${color.hex}"></span>` +
        `<span class="swatch-text"><span class="swatch-name">${color.name}</span>` +
        `<span class="swatch-hex">${color.hex}</span></span>`;
      button.addEventListener("click", () => setColor(color.hex, color.name));
      host.appendChild(button);
    });
  }

  function setColor(hex, name) {
    const clean = String(hex || "").trim().toUpperCase();
    if (!/^#[0-9A-F]{6}$/.test(clean)) {
      $("color-error").textContent = "Enter a HEX colour such as #8B5CF6.";
      $("color-error").classList.remove("hidden");
      return;
    }
    $("color-error").classList.add("hidden");
    state.color = clean;
    const known = state.config.colors.find((c) => c.hex.toUpperCase() === clean);
    state.colorName = name || (known ? known.name : "Custom");
    $("color-hex").value = clean;
    $("color-picker").value = clean;
    $("color-name").textContent = state.colorName;
    document.documentElement.style.setProperty("--accent", clean);
    document.querySelectorAll(".swatch").forEach((el) => {
      el.classList.toggle("on", el.dataset.hex.toUpperCase() === clean);
    });
    renderPreview();
  }

  function renderBackends() {
    const select = $("cap-backend");
    select.innerHTML = "";
    const labels = {
      faster_whisper: "faster-whisper (local speech-to-text)",
      manual: "Paste transcript manually",
      none: "No captions",
    };
    state.config.transcriptionBackends.forEach((backend) => {
      const option = document.createElement("option");
      option.value = backend.id;
      option.textContent =
        (labels[backend.id] || backend.id) + (backend.available ? "" : " — unavailable");
      select.appendChild(option);
    });
    const firstAvailable = state.config.transcriptionBackends.find((b) => b.available);
    select.value = firstAvailable ? firstAvailable.id : "none";
    onBackendChange();
  }

  function onBackendChange() {
    const id = $("cap-backend").value;
    const backend = state.config.transcriptionBackends.find((b) => b.id === id);
    $("cap-backend-hint").textContent =
      backend && !backend.available ? backend.reason : "";
    $("manual-text-field").classList.toggle("hidden", id !== "manual");
  }

  function renderOutputPresets() {
    const host = $("output-presets");
    host.innerHTML = "";
    const labels = { "1080p30": "1080p · 30 fps", "1080p60": "1080p · 60 fps", "720p30": "720p · 30 fps" };
    state.config.outputPresets.forEach((id) => {
      const button = document.createElement("button");
      button.type = "button";
      button.textContent = labels[id] || id;
      button.dataset.preset = id;
      button.addEventListener("click", () => {
        state.outputPreset = id;
        host.querySelectorAll("button").forEach((b) =>
          b.classList.toggle("on", b.dataset.preset === id));
      });
      host.appendChild(button);
    });
    const active = host.querySelector(`[data-preset="${state.outputPreset}"]`) || host.firstChild;
    if (active) active.classList.add("on");
  }

  async function renderFonts() {
    const fonts = state.config.fonts || [];
    ["hook-font", "cap-font"].forEach((selectId) => {
      const select = $(selectId);
      const previous = select.value;
      select.innerHTML = "";
      const auto = document.createElement("option");
      auto.value = "";
      auto.textContent = "Automatic fallback";
      select.appendChild(auto);
      fonts.forEach((font) => {
        const option = document.createElement("option");
        option.value = font.id;
        option.textContent =
          font.source === "missing" ? `${font.family} (not uploaded)` : font.family;
        option.disabled = font.source === "missing";
        select.appendChild(option);
      });
      if (previous) select.value = previous;
    });

    const list = $("font-list");
    list.innerHTML = "";
    fonts.forEach((font) => {
      const li = document.createElement("li");
      const missing = font.source === "missing";
      li.className = missing ? "font-missing" : "";
      li.innerHTML =
        `<span>${font.family}</span>` +
        (missing
          ? '<span class="tag warn">not installed</span>'
          : `<span class="tag">${font.extension || ""}</span>`);
      if (!missing) {
        const remove = document.createElement("button");
        remove.type = "button";
        remove.className = "ghost-btn";
        remove.textContent = "Remove";
        remove.addEventListener("click", () => deleteFont(font.id));
        li.appendChild(remove);
      }
      list.appendChild(li);
    });

    // Load real font files into the browser so the preview matches the render.
    fonts.filter((f) => f.source !== "missing").forEach(loadFontFace);
  }

  async function loadFontFace(font) {
    if (state.loadedFonts.has(font.id)) return;
    state.loadedFonts.add(font.id);
    try {
      const face = new FontFace(`cf-${font.id}`, `url(/api/fonts/${font.id}/file)`);
      await face.load();
      document.fonts.add(face);
      renderPreview();
    } catch (_) {
      state.loadedFonts.delete(font.id);
    }
  }

  function renderPresets() {
    const select = $("preset-select");
    select.innerHTML = "";
    (state.config.presets || []).forEach((preset) => {
      const option = document.createElement("option");
      option.value = preset.id;
      option.textContent = preset.name;
      select.appendChild(option);
    });
  }

  function applyPreset(preset) {
    if (!preset) return;
    const hook = preset.hook || {};
    const captions = preset.captions || {};
    const viewport = preset.viewport || {};
    const output = preset.output || {};
    const silence = preset.silence || {};
    const effects = preset.effects || {};

    if (hook.fontId) $("hook-font").value = hook.fontId;
    if (hook.fontSize) setRange("hook-size", hook.fontSize);
    if (hook.align) $("hook-align").value = hook.align;
    if (hook.gapAboveViewport != null) setRange("hook-gap", hook.gapAboveViewport);
    if (hook.uppercase != null) $("hook-upper").checked = !!hook.uppercase;

    if (captions.fontId) $("cap-font").value = captions.fontId;
    if (captions.fontSize) setRange("cap-size", captions.fontSize);
    if (captions.maxWordsPerPhrase) setRange("cap-maxwords", captions.maxWordsPerPhrase);
    if (captions.outlineWidth != null) setRange("cap-outline", captions.outlineWidth);
    if (captions.shadowOffset != null) setRange("cap-shadow", captions.shadowOffset);
    if (captions.marginV != null) setRange("cap-marginv", captions.marginV);
    if (captions.vertical) $("cap-vertical").value = captions.vertical;
    if (captions.color) $("cap-color").value = captions.color;
    if (captions.uppercase != null) $("cap-upper").checked = !!captions.uppercase;

    if (viewport.size) setRange("vp-size", viewport.size);
    if (viewport.y != null) setRange("vp-y", viewport.y);

    if (silence.enabled != null) $("sil-enabled").checked = !!silence.enabled;
    if (silence.thresholdDb != null) setRange("sil-threshold", silence.thresholdDb);
    if (silence.minSilenceDuration != null) setRange("sil-min", silence.minSilenceDuration);

    if (effects.mode) $("fx-mode").value = effects.mode;
    if (effects.autoZoomAmount != null) setRange("fx-amount", Math.round(effects.autoZoomAmount * 100));

    if (output.presetId) {
      state.outputPreset = output.presetId;
      renderOutputPresets();
    }

    const highlight = hook.highlightColor || state.config.defaults.highlightColor;
    setColor(highlight);
    syncLabels();
    renderPreview();
  }

  function setRange(id, value) {
    const el = $(id);
    if (el) el.value = value;
  }

  /* ------------------------------------------------------------------ *
   * hook words
   * ------------------------------------------------------------------ */

  function renderWordChips() {
    const raw = $("hook-text").value || "";
    state.words = raw.split(/\s+/).filter(Boolean);
    const host = $("hook-words");
    host.innerHTML = "";
    if (!state.words.length) {
      host.innerHTML = '<span class="empty-note">Type a hook above.</span>';
      return;
    }
    state.words.forEach((word, index) => {
      const chip = document.createElement("span");
      chip.className = "chip" + (state.highlight.has(index) ? " on" : "");
      chip.textContent = word;
      if (state.highlight.has(index)) chip.style.background = state.color;
      chip.addEventListener("click", () => {
        if (state.highlight.has(index)) state.highlight.delete(index);
        else state.highlight.add(index);
        renderWordChips();
        renderPreview();
      });
      host.appendChild(chip);
    });
    // Drop indices that no longer exist after an edit.
    [...state.highlight].forEach((i) => { if (i >= state.words.length) state.highlight.delete(i); });
  }

  /* ------------------------------------------------------------------ *
   * preview
   * ------------------------------------------------------------------ */

  function renderPreview() {
    const canvas = $("canvas");
    const width = canvas.clientWidth || 300;
    const scale = width / 1080; // preview pixels per output pixel

    const size = Number($("vp-size").value);
    const y = Number($("vp-y").value);
    const x = (1080 - size) / 2;

    const square = $("pv-square");
    square.style.left = `${x * scale}px`;
    square.style.top = `${y * scale}px`;
    square.style.width = `${size * scale}px`;
    square.style.height = `${size * scale}px`;

    // hook: sits above the square, never inside it
    const hookEl = $("pv-hook");
    const hookSize = Number($("hook-size").value);
    const gap = Number($("hook-gap").value);
    const enabled = $("hook-enabled").checked;
    hookEl.style.display = enabled ? "block" : "none";
    hookEl.style.fontSize = `${hookSize * scale}px`;
    hookEl.style.textAlign = $("hook-align").value;
    hookEl.style.fontFamily = fontStack($("hook-font").value);

    let text = $("hook-text").value || "";
    if ($("hook-upper").checked) text = text.toUpperCase();
    const words = text.split(/\s+/).filter(Boolean);
    hookEl.innerHTML = words
      .map((word, index) =>
        state.highlight.has(index)
          ? `<span style="color:${state.color}">${escapeHtml(word)}</span>`
          : escapeHtml(word))
      .join(" ");

    // Bottom-align the hook block just above the square.
    hookEl.style.bottom = "";
    hookEl.style.top = "0px";
    const hookHeight = hookEl.offsetHeight;
    const top = Math.max(0, y * scale - gap * scale - hookHeight);
    hookEl.style.top = `${top}px`;

    // caption inside the square
    const caption = $("pv-caption");
    caption.style.display = $("cap-enabled").checked ? "block" : "none";
    caption.style.fontFamily = fontStack($("cap-font").value);
    caption.style.fontSize = `${Number($("cap-size").value) * scale}px`;
    caption.style.color = $("cap-color").value;
    const outline = Number($("cap-outline").value) * 2.2 * scale;
    const shadow = Number($("cap-shadow").value) * scale;
    caption.style.webkitTextStroke = outline > 0.2 ? `${outline}px #000` : "";
    caption.style.textShadow = shadow > 0 ? `${shadow}px ${shadow}px 0 #000` : "none";

    const sample = $("cap-upper").checked ? "CAPTION TEXT" : "Caption text";
    caption.textContent = sample;

    const marginV = Number($("cap-marginv").value) * scale;
    const vertical = $("cap-vertical").value;
    caption.style.top = caption.style.bottom = "";
    if (vertical === "top") caption.style.top = `${marginV}px`;
    else if (vertical === "middle") caption.style.top = `${(size * scale) / 2 - 14}px`;
    else caption.style.bottom = `${marginV}px`;
  }

  function fontStack(fontId) {
    return fontId ? `"cf-${fontId}", system-ui, sans-serif` : "system-ui, sans-serif";
  }

  function escapeHtml(value) {
    return value.replace(/[&<>"']/g, (c) =>
      ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  }

  /* ------------------------------------------------------------------ *
   * upload
   *
   * The file is sent in pieces rather than as one large POST. A single
   * multi-hundred-megabyte request does not survive the GitHub Codespaces
   * port-forwarding proxy, and it does not survive a phone switching between
   * wifi and mobile data either. Small pieces also mean a failure costs one
   * chunk instead of the entire upload.
   * ------------------------------------------------------------------ */

  const CHUNK_ATTEMPTS = 4;
  const CHUNK_TIMEOUT_MS = 120000;

  async function uploadVideo(file) {
    if (!file) return;

    const limit = state.config.limits.maxUploadBytes;
    if (file.size > limit) {
      toast(`That file is ${bytes(file.size)}; the limit is ${bytes(limit)}.`, true);
      return;
    }

    $("dropzone-hint").textContent = `${file.name} · ${bytes(file.size)}`;
    $("upload-progress-wrap").classList.remove("hidden");
    $("upload-bar").style.width = "0%";
    $("upload-label").textContent = "Preparing…";
    $("generate").disabled = true;

    let uploadId = null;
    try {
      const session = await postJSON("/api/uploads", {
        filename: file.name,
        size: file.size,
      });
      uploadId = session.uploadId;
      state.upload = uploadId;
      const chunkSize = session.chunkSize || 8 * 1024 * 1024;

      let offset = 0;
      while (offset < file.size) {
        const end = Math.min(offset + chunkSize, file.size);
        await sendChunk(uploadId, file.slice(offset, end), offset);
        offset = end;

        const percent = (offset / file.size) * 100;
        $("upload-bar").style.width = `${percent.toFixed(1)}%`;
        $("upload-label").textContent =
          `Uploading · ${percent.toFixed(0)}% (${bytes(offset)} of ${bytes(file.size)})`;
      }

      $("upload-label").textContent = "Analyzing video…";
      const data = await postJSON(`/api/uploads/${uploadId}/finish`, {});
      state.upload = null;

      $("upload-label").textContent = "Ready";
      state.source = data.source;
      state.media = data.media;
      showMeta(data.source, data.media);
      $("generate").disabled = false;
      $("generate-msg").textContent = "Ready to render.";
      $("clip-end").value = clock(Math.min(data.media.duration, 30));
    } catch (err) {
      $("upload-progress-wrap").classList.add("hidden");
      $("upload-label").textContent = "";
      state.upload = null;
      if (uploadId) {
        // Do not leave a half-written .part file on the server.
        request(`/api/uploads/${uploadId}`, { method: "DELETE" }).catch(() => {});
      }
      toast(err.message || "That upload failed.", true);
    }
  }

  /* Send one chunk, retrying transient failures at the same byte offset. */
  function sendChunk(uploadId, blob, offset) {
    return new Promise((resolve, reject) => {
      let attempt = 0;

      const attemptSend = () => {
        attempt += 1;

        const giveUpOrRetry = (message) => {
          if (attempt < CHUNK_ATTEMPTS) {
            $("upload-label").textContent =
              `Connection problem — retrying (${attempt}/${CHUNK_ATTEMPTS - 1})…`;
            sleep(700 * attempt).then(attemptSend);
          } else {
            reject(new Error(message));
          }
        };

        const xhr = new XMLHttpRequest();
        xhr.open("POST", api(`/api/uploads/${uploadId}/chunk`));
        xhr.setRequestHeader("Content-Type", "application/octet-stream");
        xhr.setRequestHeader("X-Upload-Offset", String(offset));
        xhr.timeout = CHUNK_TIMEOUT_MS;

        xhr.addEventListener("load", () => {
          if (xhr.status >= 200 && xhr.status < 300) {
            resolve();
            return;
          }
          // Surface the status code: when a proxy rejects the request the body
          // is an HTML error page, not our JSON envelope.
          let message = `Upload failed (HTTP ${xhr.status})`;
          try {
            const parsed = JSON.parse(xhr.responseText || "{}");
            if (parsed && parsed.message) message = parsed.message;
          } catch (_) { /* not our JSON envelope */ }

          if (xhr.status >= 500 || xhr.status === 408 || xhr.status === 429) {
            giveUpOrRetry(message);
          } else {
            reject(new Error(message));
          }
        });

        xhr.addEventListener("error", () =>
          giveUpOrRetry("The connection dropped during upload. Please try again."));
        xhr.addEventListener("timeout", () =>
          giveUpOrRetry("The upload timed out. Please try again on a stronger connection."));

        xhr.send(blob);
      };

      attemptSend();
    });
  }

  function showMeta(source, media) {
    $("meta-panel").classList.remove("hidden");
    $("meta-name").textContent = source.originalName;
    $("meta-size").textContent = bytes(source.sizeBytes);
    $("meta-duration").textContent = `${clock(media.duration)} (${media.duration.toFixed(2)}s)`;
    $("meta-resolution").textContent = `${media.width}×${media.height}`;
    $("meta-fps").textContent = `${media.fps} fps`;
    $("meta-audio").textContent = media.hasAudio ? `yes (${media.audioCodec})` : "no audio track";
  }

  async function uploadFont(file) {
    if (!file) return;
    const form = new FormData();
    form.append("file", file, file.name);
    try {
      const data = await request("/api/fonts", { method: "POST", body: form });
      state.config.fonts = data.fonts;
      await renderFonts();
      toast(`${data.font.family} installed.`);
      renderPreview();
    } catch (err) {
      toast(err.message, true);
    }
  }

  async function deleteFont(fontId) {
    try {
      const data = await request(`/api/fonts/${fontId}`, { method: "DELETE" });
      state.config.fonts = data.fonts;
      await renderFonts();
    } catch (err) {
      toast(err.message, true);
    }
  }

  /* ------------------------------------------------------------------ *
   * manual keyframes
   * ------------------------------------------------------------------ */

  function renderKeyframes() {
    const host = $("keyframes");
    host.innerHTML = "";
    state.keyframes.forEach((frame, index) => {
      const row = document.createElement("div");
      row.className = "keyframe";

      const start = document.createElement("input");
      start.type = "number";
      start.step = "0.5";
      start.value = frame.start;
      start.addEventListener("input", () => { frame.start = Number(start.value); });

      const end = document.createElement("input");
      end.type = "number";
      end.step = "0.5";
      end.value = frame.end;
      end.addEventListener("input", () => { frame.end = Number(end.value); });

      const type = document.createElement("select");
      state.config.effectTypes.forEach((effectType) => {
        const option = document.createElement("option");
        option.value = effectType;
        option.textContent = effectType.replace(/_/g, " ");
        type.appendChild(option);
      });
      type.value = frame.type;
      type.addEventListener("change", () => { frame.type = type.value; });

      const remove = document.createElement("button");
      remove.type = "button";
      remove.className = "ghost-btn";
      remove.textContent = "✕";
      remove.addEventListener("click", () => {
        state.keyframes.splice(index, 1);
        renderKeyframes();
      });

      row.append(start, end, type, remove);
      host.appendChild(row);
    });
  }

  /* ------------------------------------------------------------------ *
   * render request
   * ------------------------------------------------------------------ */

  function buildRequest() {
    const highlightIndices = [...state.highlight].sort((a, b) => a - b);
    const payload = {
      sourceId: state.source.id,
      clip: { start: $("clip-start").value.trim(), end: $("clip-end").value.trim() },
      viewport: { size: Number($("vp-size").value), y: Number($("vp-y").value) },
      hook: {
        enabled: $("hook-enabled").checked,
        text: $("hook-text").value,
        fontId: $("hook-font").value,
        fontSize: Number($("hook-size").value),
        highlightColor: state.color,
        highlightIndices,
        align: $("hook-align").value,
        gapAboveViewport: Number($("hook-gap").value),
        uppercase: $("hook-upper").checked,
      },
      captions: {
        enabled: $("cap-enabled").checked,
        fontId: $("cap-font").value,
        fontSize: Number($("cap-size").value),
        color: $("cap-color").value,
        outlineWidth: Number($("cap-outline").value),
        shadowOffset: Number($("cap-shadow").value),
        vertical: $("cap-vertical").value,
        marginV: Number($("cap-marginv").value),
        maxWordsPerPhrase: Number($("cap-maxwords").value),
        uppercase: $("cap-upper").checked,
        highlightColor: state.color,
      },
      silence: {
        enabled: $("sil-enabled").checked,
        thresholdDb: Number($("sil-threshold").value),
        minSilenceDuration: Number($("sil-min").value),
        padBefore: Number($("sil-before").value),
        padAfter: Number($("sil-after").value),
      },
      effects: {
        mode: $("fx-mode").value,
        autoZoomAmount: Number($("fx-amount").value) / 100,
        autoCycleSeconds: Number($("fx-cycle").value),
        effects: state.keyframes,
      },
      output: { presetId: state.outputPreset },
      transcription: {
        backend: $("cap-backend").value,
        manualText: $("cap-manual").value,
      },
    };
    return payload;
  }

  async function generate() {
    if (!state.source) return;
    $("clip-error").classList.add("hidden");
    $("generate").disabled = true;
    $("result-card").classList.add("hidden");

    let data;
    try {
      data = await postJSON("/api/jobs", buildRequest());
    } catch (err) {
      $("generate").disabled = false;
      $("clip-error").textContent = err.message;
      $("clip-error").classList.remove("hidden");
      toast(err.message, true);
      return;
    }

    state.job = data.job;
    $("status-card").classList.remove("hidden");
    $("status-card").scrollIntoView({ behavior: "smooth", block: "center" });
    buildStageList();
    pollJob();
  }

  function buildStageList() {
    const list = $("stage-list");
    list.innerHTML = "";
    STAGES.forEach(([id, label]) => {
      const li = document.createElement("li");
      li.dataset.stage = id;
      li.textContent = label;
      list.appendChild(li);
    });
  }

  function pollJob() {
    clearInterval(state.poll);
    state.poll = setInterval(async () => {
      let data;
      try {
        data = await request(`/api/jobs/${state.job.id}`);
      } catch (err) {
        clearInterval(state.poll);
        toast(err.message, true);
        $("generate").disabled = false;
        return;
      }
      const job = data.job;
      state.job = job;
      applyStatus(job);

      if (job.status.stage === "done") {
        clearInterval(state.poll);
        showResult(job);
      } else if (job.status.stage === "failed" || job.status.stage === "cancelled") {
        clearInterval(state.poll);
        $("generate").disabled = false;
        $("generate-msg").textContent = job.status.error || "Render cancelled.";
        if (job.status.error) toast(job.status.error, true);
      }
    }, 1200);
  }

  function applyStatus(job) {
    const status = job.status;
    const percent = Math.round((status.overallProgress || 0) * 100);
    $("job-bar").style.width = `${percent}%`;
    $("job-percent").textContent = status.determinate ? `${percent}%` : "working…";
    $("job-stage").textContent = status.error || status.message;
    $("job-bar").parentElement.classList.toggle("indeterminate", !status.determinate);

    const order = STAGES.map(([id]) => id);
    const current = order.indexOf(status.stage);
    document.querySelectorAll("#stage-list li").forEach((li, index) => {
      li.classList.toggle("active", index === current);
      li.classList.toggle("done", current === -1 ? status.stage === "done" : index < current);
    });
    if (status.stage === "done") {
      document.querySelectorAll("#stage-list li").forEach((li) => li.classList.add("done"));
    }
  }

  function showResult(job) {
    $("generate").disabled = false;
    $("generate-msg").textContent = "Render complete.";
    $("result-card").classList.remove("hidden");

    const video = $("result-video");
    video.src = `${job.previewUrl}?t=${Date.now()}`;
    $("download-link").href = job.downloadUrl;
    $("download-link").setAttribute("download", `${job.id}.mp4`);

    const meta = job.outputMeta || {};
    $("result-meta").innerHTML = [
      ["Resolution", `${meta.width}×${meta.height}`],
      ["FPS", meta.fps],
      ["Duration", `${(meta.duration || 0).toFixed(2)}s`],
      ["Video codec", meta.videoCodec],
      ["Audio codec", meta.audioCodec || "none"],
      ["File size", bytes(job.outputBytes)],
      ["Caption phrases", job.captionCount],
      ["Silence removed", `${(job.removedSilenceSeconds || 0).toFixed(2)}s`],
    ].map(([k, v]) => `<div class="meta-row"><span>${k}</span><b>${v}</b></div>`).join("");

    const warnings = $("result-warnings");
    if (job.warnings && job.warnings.length) {
      warnings.innerHTML = job.warnings.map((w) => `<p>${escapeHtml(w)}</p>`).join("");
      warnings.classList.remove("hidden");
    } else {
      warnings.classList.add("hidden");
    }

    $("result-card").scrollIntoView({ behavior: "smooth", block: "start" });
  }

  /* ------------------------------------------------------------------ *
   * presets
   * ------------------------------------------------------------------ */

  async function savePreset() {
    const name = $("preset-name").value.trim();
    if (!name) { toast("Give the preset a name first.", true); return; }
    const base = state.source ? buildRequest() : null;
    const payload = {
      name,
      hook: base ? base.hook : { highlightColor: state.color },
      captions: base ? base.captions : {},
      viewport: { size: Number($("vp-size").value), y: Number($("vp-y").value) },
      output: { presetId: state.outputPreset },
      silence: {
        enabled: $("sil-enabled").checked,
        thresholdDb: Number($("sil-threshold").value),
        minSilenceDuration: Number($("sil-min").value),
      },
      effects: {
        mode: $("fx-mode").value,
        autoZoomAmount: Number($("fx-amount").value) / 100,
      },
    };
    try {
      await postJSON("/api/presets", payload);
      const data = await request("/api/presets");
      state.config.presets = data.presets;
      renderPresets();
      $("preset-name").value = "";
      toast(`Preset “${name}” saved.`);
    } catch (err) {
      toast(err.message, true);
    }
  }

  /* ------------------------------------------------------------------ *
   * wiring
   * ------------------------------------------------------------------ */

  function syncLabels() {
    $("hook-size-val").textContent = $("hook-size").value;
    $("hook-gap-val").textContent = $("hook-gap").value;
    $("cap-size-val").textContent = $("cap-size").value;
    $("cap-maxwords-val").textContent = $("cap-maxwords").value;
    $("cap-outline-val").textContent = Number($("cap-outline").value).toFixed(1);
    $("cap-shadow-val").textContent = $("cap-shadow").value;
    $("cap-marginv-val").textContent = $("cap-marginv").value;
    $("vp-size-val").textContent = $("vp-size").value;
    $("vp-y-val").textContent = $("vp-y").value;
    $("fx-amount-val").textContent = `${$("fx-amount").value}%`;
    $("fx-cycle-val").textContent = $("fx-cycle").value;
    $("sil-threshold-val").textContent = $("sil-threshold").value;
    $("sil-min-val").textContent = `${Number($("sil-min").value).toFixed(2)}s`;
    $("sil-before-val").textContent = `${Number($("sil-before").value).toFixed(2)}s`;
    $("sil-after-val").textContent = `${Number($("sil-after").value).toFixed(2)}s`;
  }

  function wireEvents() {
    const dropzone = $("dropzone");
    const fileInput = $("file-input");
    dropzone.addEventListener("click", () => fileInput.click());
    fileInput.addEventListener("change", () => uploadVideo(fileInput.files[0]));

    ["dragenter", "dragover"].forEach((event) =>
      dropzone.addEventListener(event, (e) => {
        e.preventDefault();
        dropzone.classList.add("dragover");
      }));
    ["dragleave", "drop"].forEach((event) =>
      dropzone.addEventListener(event, (e) => {
        e.preventDefault();
        dropzone.classList.remove("dragover");
      }));
    dropzone.addEventListener("drop", (e) => {
      if (e.dataTransfer.files.length) uploadVideo(e.dataTransfer.files[0]);
    });

    $("font-input").addEventListener("change", (e) => uploadFont(e.target.files[0]));

    $("hook-text").addEventListener("input", () => { renderWordChips(); renderPreview(); });

    $("color-hex").addEventListener("change", (e) => setColor(e.target.value));
    $("color-picker").addEventListener("input", (e) => setColor(e.target.value));

    $("cap-backend").addEventListener("change", onBackendChange);

    $("fx-mode").addEventListener("change", () => {
      const mode = $("fx-mode").value;
      $("fx-auto").classList.toggle("hidden", mode !== "auto");
      $("fx-manual").classList.toggle("hidden", mode !== "manual");
    });

    $("fx-add").addEventListener("click", () => {
      const last = state.keyframes[state.keyframes.length - 1];
      const start = last ? last.end : 0;
      state.keyframes.push({ start, end: start + 4, type: "zoom_in", scale: 1.0, scaleTo: 1.12 });
      renderKeyframes();
    });

    document.querySelectorAll('input[type="range"], input[type="color"], select, input[type="checkbox"]')
      .forEach((el) => {
        el.addEventListener("input", () => { syncLabels(); renderPreview(); });
        el.addEventListener("change", () => { syncLabels(); renderPreview(); });
      });

    $("generate").addEventListener("click", generate);

    $("job-cancel").addEventListener("click", async () => {
      if (!state.job) return;
      try {
        await postJSON(`/api/jobs/${state.job.id}/cancel`, {});
        toast("Cancelling…");
      } catch (err) {
        toast(err.message, true);
      }
    });

    $("preset-save").addEventListener("click", savePreset);
    $("preset-load").addEventListener("click", () => {
      const preset = state.config.presets.find((p) => p.id === $("preset-select").value);
      applyPreset(preset);
      toast(preset ? `Loaded “${preset.name}”.` : "Preset not found.", !preset);
    });

    $("preview-toggle").addEventListener("click", () => {
      const column = $("preview-col");
      const shown = !column.classList.toggle("hidden-mobile");
      $("preview-toggle").setAttribute("aria-expanded", String(shown));
      if (shown) { renderPreview(); column.scrollIntoView({ behavior: "smooth" }); }
    });

    window.addEventListener("resize", renderPreview);
  }

  document.addEventListener("DOMContentLoaded", boot);
})();
