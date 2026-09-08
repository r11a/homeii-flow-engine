const HOMEII_SYSTEM_SCREENSAVER_VERSION = "0.6.1";

(() => {
  if (window.__homeiiFlowSystemScreensaver) return;

  function readSessionValue(key) {
    try {
      return window.sessionStorage?.getItem(key) || "";
    } catch {
      return "";
    }
  }

  const state = {
    config: null,
    hass: null,
    overlay: null,
    visible: false,
    lastActivityAt: Date.now(),
    lastRefreshAt: 0,
    lastError: "",
    lastShowRequestId: readSessionValue("homeiiFlowLastScreensaverRequest"),
    visibleByCommand: false,
    status: "starting",
    refreshTimer: 0,
    idleTimer: 0,
    clockTimer: 0,
  };

  const DEFAULT_TONE_BACKGROUND = `
      radial-gradient(circle at 46% 18%, rgba(255,255,255,.16), rgba(255,255,255,0) 28%),
      linear-gradient(115deg, rgba(0,0,0,.72), rgba(8,11,20,.42) 45%, rgba(0,0,0,.82)),
      radial-gradient(circle at 50% 70%, rgba(255,255,255,.07), rgba(0,0,0,.84) 58%)`;
  const paletteCache = new Map();

  const rootStyle = `
    position: fixed;
    inset: 0;
    z-index: 2147483000;
    display: none;
    align-items: center;
    justify-content: center;
    overflow: hidden;
    background: #05070d;
    color: #fff;
    font-family: var(--primary-font-family, Inter, system-ui, sans-serif);
    direction: ltr;
    backdrop-filter: blur(22px);
  `;

  const html = `
    <div data-homeii-backdrop-art style="position:absolute;inset:-14%;opacity:0;background-position:center;background-size:cover;filter:blur(56px) saturate(1.28) brightness(.72);transform:scale(1.12);transition:opacity .55s ease, background-image .2s ease;pointer-events:none;"></div>
    <div data-homeii-backdrop-tone style="position:absolute;inset:0;background:${DEFAULT_TONE_BACKGROUND};transition:background .7s ease;pointer-events:none;"></div>
    <div class="homeii-system-screen" style="position:relative;z-index:2;text-align:center;width:min(1080px,88vw);padding:32px;">
      <img data-homeii-logo src="/homeii_flow/homeii-flow-logo.png?v=${HOMEII_SYSTEM_SCREENSAVER_VERSION}" alt="HOMEii Flow" style="width:clamp(160px,22vw,340px);height:auto;opacity:.76;margin:0 auto 28px;display:block;filter:drop-shadow(0 14px 32px rgba(0,0,0,.42));">
      <div data-homeii-clock style="font-weight:900;font-size:clamp(56px,12vw,168px);line-height:.9;text-shadow:0 18px 70px rgba(0,0,0,.42);"></div>
      <div data-homeii-date style="font-weight:800;font-size:clamp(14px,2vw,28px);opacity:.78;margin-top:18px;"></div>
      <div data-homeii-message style="font-weight:750;font-size:clamp(15px,1.7vw,24px);opacity:.82;margin-top:30px;"></div>
      <div data-homeii-playing style="display:none;align-items:center;justify-content:center;flex-wrap:wrap;gap:clamp(22px,5vw,58px);margin-top:42px;text-align:left;">
        <div data-homeii-art-shell style="width:clamp(180px,24vw,330px);aspect-ratio:1;border-radius:34px;overflow:hidden;background:rgba(255,255,255,.10);box-shadow:0 26px 90px rgba(0,0,0,.45);border:1px solid rgba(255,255,255,.18);">
          <img data-homeii-art alt="" style="width:100%;height:100%;object-fit:cover;display:none;">
          <div data-homeii-art-fallback style="width:100%;height:100%;display:flex;align-items:center;justify-content:center;font-weight:900;font-size:clamp(34px,6vw,72px);letter-spacing:.08em;opacity:.42;">ART</div>
        </div>
        <div style="min-width:0;max-width:min(520px,46vw);">
          <div data-homeii-kicker style="font-size:clamp(11px,1.1vw,15px);font-weight:900;letter-spacing:.24em;opacity:.58;margin-bottom:12px;">NOW PLAYING</div>
          <div data-homeii-now style="font-weight:900;font-size:clamp(28px,4.4vw,64px);line-height:1.02;text-shadow:0 18px 60px rgba(0,0,0,.42);overflow-wrap:anywhere;"></div>
          <div data-homeii-sub style="font-weight:750;font-size:clamp(15px,1.7vw,24px);opacity:.78;margin-top:12px;overflow-wrap:anywhere;"></div>
          <div data-homeii-player style="font-weight:750;font-size:clamp(12px,1.2vw,16px);opacity:.52;margin-top:18px;overflow-wrap:anywhere;"></div>
        </div>
      </div>
    </div>
  `;

  function looksLikeHass(value) {
    return Boolean(value?.callWS && value?.states && value?.services);
  }

  function findHassInTree(root, depth = 0, seen = new Set()) {
    if (!root || depth > 7 || seen.has(root)) return null;
    seen.add(root);
    if (looksLikeHass(root.hass)) return root.hass;
    const children = [];
    if (root.shadowRoot) children.push(root.shadowRoot);
    if (root.children) children.push(...root.children);
    if (root.querySelectorAll) {
      children.push(
        ...root.querySelectorAll(
          "home-assistant, home-assistant-main, partial-panel-resolver, ha-panel-lovelace, hui-root, ha-sidebar"
        )
      );
    }
    for (const child of children) {
      if (looksLikeHass(child?.hass)) return child.hass;
      const found = findHassInTree(child, depth + 1, seen);
      if (found) return found;
    }
    return null;
  }

  function findHass() {
    if (looksLikeHass(state.hass)) return state.hass;
    const candidates = [
      document.querySelector("home-assistant"),
      document.querySelector("home-assistant-main"),
      document.querySelector("ha-panel-lovelace"),
      document.querySelector("hui-root"),
    ];
    for (const element of candidates) {
      if (looksLikeHass(element?.hass)) return element.hass;
    }
    return findHassInTree(document);
  }

  function createOverlay() {
    if (state.overlay) return state.overlay;
    const overlay = document.createElement("div");
    overlay.id = "homeii-flow-system-screensaver";
    overlay.setAttribute("data-homeii-flow-version", HOMEII_SYSTEM_SCREENSAVER_VERSION);
    overlay.style.cssText = rootStyle;
    overlay.innerHTML = html;
    overlay.addEventListener("pointerdown", hide);
    overlay.addEventListener("wheel", hide, { passive: true });
    document.body.appendChild(overlay);
    state.overlay = overlay;
    return overlay;
  }

  function formatTime(date) {
    return date.toLocaleTimeString([], { hour: "2-digit", minute: "2-digit" });
  }

  function formatDate(date) {
    return date.toLocaleDateString([], { weekday: "long", month: "long", day: "numeric" });
  }

  function text(value) {
    return String(value || "").trim();
  }

  function looksLikeRawMediaText(value) {
    const clean = text(value);
    if (!clean) return false;
    return /^(https?:)?\/\//i.test(clean)
      || /\/(api|flow|media|local)\//i.test(clean)
      || /\.(aac|flac|m4a|mp3|ogg|opus|wav)([?#].*)?$/i.test(clean);
  }

  function cleanMediaText(value) {
    const clean = text(value);
    if (!clean) return "";
    const lower = clean.toLowerCase();
    if (["unknown", "none", "null", "undefined", "unavailable"].includes(lower)) return "";
    if (looksLikeRawMediaText(clean)) return "";
    return clean;
  }

  function firstCleanMediaText(...values) {
    for (const value of values) {
      const clean = cleanMediaText(value);
      if (clean) return clean;
    }
    return "";
  }

  function artworkUrl(path) {
    const value = text(path);
    if (!value) return "";
    if (/^(https?:)?\/\//i.test(value) || value.startsWith("data:") || value.startsWith("blob:")) return value;
    if (value.startsWith("/")) return value;
    return `/${value}`;
  }

  function cssImageUrl(value) {
    return `url("${String(value || "").replace(/["\\\n\r\f]/g, (char) => encodeURIComponent(char))}")`;
  }

  function clamp(value, minimum, maximum) {
    return Math.max(minimum, Math.min(maximum, value));
  }

  function colorCss(color, alpha = 1) {
    return `rgba(${Math.round(color.r)},${Math.round(color.g)},${Math.round(color.b)},${alpha})`;
  }

  function colorLuma(color) {
    return (0.2126 * color.r + 0.7152 * color.g + 0.0722 * color.b) / 255;
  }

  function colorSaturation(color) {
    const maximum = Math.max(color.r, color.g, color.b);
    const minimum = Math.min(color.r, color.g, color.b);
    return maximum ? (maximum - minimum) / maximum : 0;
  }

  function tuneColor(color, saturationBoost = 1.18, brightnessBoost = 1.04) {
    const average = (color.r + color.g + color.b) / 3;
    return {
      r: clamp(average + (color.r - average) * saturationBoost, 0, 255) * brightnessBoost,
      g: clamp(average + (color.g - average) * saturationBoost, 0, 255) * brightnessBoost,
      b: clamp(average + (color.b - average) * saturationBoost, 0, 255) * brightnessBoost,
    };
  }

  function fallbackPalette() {
    return {
      primary: { r: 78, g: 94, b: 126 },
      secondary: { r: 114, g: 76, b: 128 },
      glow: { r: 245, g: 166, b: 35 },
    };
  }

  function paletteToneBackground(palette) {
    const primary = tuneColor(palette.primary, 1.22, 1.08);
    const secondary = tuneColor(palette.secondary, 1.28, 1.04);
    const glow = tuneColor(palette.glow || palette.primary, 1.35, 1.1);
    return `
      radial-gradient(circle at 33% 38%, ${colorCss(primary, .42)}, rgba(0,0,0,0) 34%),
      radial-gradient(circle at 68% 26%, ${colorCss(secondary, .32)}, rgba(0,0,0,0) 38%),
      radial-gradient(circle at 58% 76%, ${colorCss(glow, .24)}, rgba(0,0,0,0) 34%),
      linear-gradient(118deg, rgba(0,0,0,.76), rgba(7,10,18,.38) 44%, rgba(0,0,0,.86)),
      radial-gradient(circle at 50% 58%, rgba(255,255,255,.09), rgba(0,0,0,.84) 62%)`;
  }

  function extractPaletteFromImage(imageElement) {
    if (!imageElement?.naturalWidth || !imageElement?.naturalHeight) return null;
    const size = 28;
    const canvas = document.createElement("canvas");
    canvas.width = size;
    canvas.height = size;
    const context = canvas.getContext("2d", { willReadFrequently: true });
    if (!context) return null;
    context.drawImage(imageElement, 0, 0, size, size);
    const data = context.getImageData(0, 0, size, size).data;
    const buckets = new Map();
    for (let index = 0; index < data.length; index += 16) {
      const alpha = data[index + 3];
      if (alpha < 160) continue;
      const color = { r: data[index], g: data[index + 1], b: data[index + 2] };
      const luma = colorLuma(color);
      if (luma < .06 || luma > .94) continue;
      const saturation = colorSaturation(color);
      const bucket = `${Math.round(color.r / 24)}-${Math.round(color.g / 24)}-${Math.round(color.b / 24)}`;
      const current = buckets.get(bucket) || { r: 0, g: 0, b: 0, score: 0, count: 0 };
      const weight = (saturation + .18) * (1.05 - Math.abs(luma - .5));
      current.r += color.r * weight;
      current.g += color.g * weight;
      current.b += color.b * weight;
      current.score += weight;
      current.count += 1;
      buckets.set(bucket, current);
    }
    const colors = [...buckets.values()]
      .filter((bucket) => bucket.score > 0)
      .map((bucket) => ({
        r: bucket.r / bucket.score,
        g: bucket.g / bucket.score,
        b: bucket.b / bucket.score,
        score: bucket.score * Math.log2(bucket.count + 1),
      }))
      .sort((a, b) => b.score - a.score);
    if (!colors.length) return fallbackPalette();
    const primary = colors[0];
    const secondary = colors.find((color) => Math.abs(colorLuma(color) - colorLuma(primary)) > .14 || colorSaturation(color) > colorSaturation(primary) + .08)
      || colors[1]
      || primary;
    const glow = colors.find((color) => colorSaturation(color) > .28 && colorLuma(color) > .22)
      || secondary
      || primary;
    return { primary, secondary, glow };
  }

  function applyArtworkPalette(overlay, imageElement, source = "") {
    const tone = overlay.querySelector("[data-homeii-backdrop-tone]");
    const artShell = overlay.querySelector("[data-homeii-art-shell]");
    if (!tone) return;
    const cleanSource = text(source);
    if (!cleanSource) {
      tone.dataset.homeiiPaletteSrc = "";
      tone.style.background = DEFAULT_TONE_BACKGROUND;
      if (artShell) {
        artShell.style.boxShadow = "0 26px 90px rgba(0,0,0,.45)";
        artShell.style.borderColor = "rgba(255,255,255,.18)";
      }
      return;
    }
    if (tone.dataset.homeiiPaletteSrc === cleanSource) return;
    let palette = paletteCache.get(cleanSource);
    if (!palette) {
      try {
        palette = extractPaletteFromImage(imageElement);
      } catch (error) {
        console.debug("[HOMEii Flow] Could not sample artwork palette", error);
        palette = fallbackPalette();
      }
      paletteCache.set(cleanSource, palette || fallbackPalette());
    }
    tone.dataset.homeiiPaletteSrc = cleanSource;
    const resolved = palette || fallbackPalette();
    tone.style.background = paletteToneBackground(resolved);
    if (artShell) {
      artShell.style.boxShadow = `0 28px 100px rgba(0,0,0,.48), 0 0 64px ${colorCss(tuneColor(resolved.primary, 1.22, 1.06), .24)}`;
      artShell.style.borderColor = colorCss(tuneColor(resolved.glow || resolved.secondary, 1.3, 1.08), .32);
    }
  }

  function setDynamicBackdrop(overlay, image = "", imageElement = null) {
    const backdrop = overlay.querySelector("[data-homeii-backdrop-art]");
    if (!backdrop) return;
    const source = text(image);
    if (!source) {
      backdrop.dataset.homeiiBackdropSrc = "";
      backdrop.style.opacity = "0";
      backdrop.style.backgroundImage = "";
      applyArtworkPalette(overlay, null, "");
      return;
    }
    if (backdrop.dataset.homeiiBackdropSrc !== source) {
      backdrop.dataset.homeiiBackdropSrc = source;
      backdrop.style.backgroundImage = cssImageUrl(source);
    }
    backdrop.style.opacity = ".72";
    if (imageElement) applyArtworkPalette(overlay, imageElement, source);
  }

  function hassAttributes(entityId) {
    const cleanEntityId = text(entityId);
    if (!cleanEntityId || !state.hass?.states) return {};
    return state.hass.states[cleanEntityId]?.attributes || {};
  }

  function artworkCandidates(activePlayer) {
    const entityId = text(activePlayer?.entity_id);
    const playerAttrs = hassAttributes(entityId);
    const queueId = text(playerAttrs.active_queue || playerAttrs.queue_id || activePlayer?.active_queue);
    const queueAttrs = hassAttributes(queueId);
    const rawCandidates = Array.isArray(activePlayer?.artwork_candidates) ? activePlayer.artwork_candidates : [];
    const profileId = encodeURIComponent(text(state.config?.config?.profile_id || "default") || "default");
    const proxyKey = encodeURIComponent(
      text(activePlayer?.media_content_id || activePlayer?.media_title || playerAttrs.entity_picture || queueAttrs.entity_picture || entityId)
    );
    const proxyUrl = entityId ? `/api/homeii_flow/artwork/${encodeURIComponent(entityId)}?profile_id=${profileId}&k=${proxyKey}` : "";
    const candidates = [
      playerAttrs.entity_picture,
      playerAttrs.media_image_url,
      queueAttrs.entity_picture,
      queueAttrs.media_image_url,
      proxyUrl,
      activePlayer?.entity_picture,
      activePlayer?.media_image_url,
      ...rawCandidates,
    ]
      .map(artworkUrl)
      .filter(Boolean);
    return [...new Set(candidates)];
  }
  function showRequestIsFresh(config) {
    const expiresAt = Date.parse(config?.show_request_expires_at || "");
    return Number.isFinite(expiresAt) && expiresAt > Date.now();
  }

  function rememberShowRequest(requestId) {
    state.lastShowRequestId = requestId;
    try {
      window.sessionStorage?.setItem("homeiiFlowLastScreensaverRequest", requestId);
    } catch (error) {
      console.debug("[HOMEii Flow] Could not persist screensaver request id", error);
    }
  }

  function handleShowRequest(screen) {
    const config = screen?.config || {};
    const requestId = String(config.show_request_id || "");
    if (!requestId || requestId === state.lastShowRequestId || !showRequestIsFresh(config)) return false;
    rememberShowRequest(requestId);
    show(true);
    return true;
  }

  function render() {
    const overlay = createOverlay();
    const now = new Date();
    const screen = state.config || {};
    const config = screen.config || {};
    const recommendation = screen.recommendation || {};
    const activePlayer = recommendation.active_player || {};
    const effectiveMode = screen.effective_mode || recommendation.mode || "clock";
    const entityId = text(activePlayer.entity_id);
    const playerAttrs = hassAttributes(entityId);
    const queueId = text(playerAttrs.active_queue || playerAttrs.queue_id || activePlayer.active_queue);
    const queueAttrs = hassAttributes(queueId);
    const title = firstCleanMediaText(playerAttrs.media_title, queueAttrs.media_title, activePlayer.media_title);
    const artist = firstCleanMediaText(
      playerAttrs.media_artist,
      queueAttrs.media_artist,
      activePlayer.media_artist,
      playerAttrs.media_album_artist,
      queueAttrs.media_album_artist,
      activePlayer.media_album_artist
    );
    const album = firstCleanMediaText(playerAttrs.media_album_name, queueAttrs.media_album_name, activePlayer.media_album_name);
    const player = cleanMediaText(activePlayer.friendly_name || playerAttrs.friendly_name || entityId);
    const images = artworkCandidates(activePlayer);
    const showPlaying = effectiveMode === "lyrics" && Boolean(title || artist || album || player);
    const subtitle = [artist, album].filter((value) => value && value !== title).join(" - ");
    const clock = overlay.querySelector("[data-homeii-clock]");
    const playing = overlay.querySelector("[data-homeii-playing]");
    const art = overlay.querySelector("[data-homeii-art]");
    const artFallback = overlay.querySelector("[data-homeii-art-fallback]");
    clock.textContent = formatTime(now);
    overlay.querySelector("[data-homeii-date]").textContent = formatDate(now);
    overlay.querySelector("[data-homeii-message]").textContent = config.message || "";
    clock.style.fontSize = showPlaying ? "clamp(42px,6vw,92px)" : "clamp(56px,12vw,168px)";
    playing.style.display = showPlaying ? "flex" : "none";
    overlay.querySelector("[data-homeii-now]").textContent = showPlaying ? title || "Now playing" : "";
    overlay.querySelector("[data-homeii-sub]").textContent = showPlaying ? subtitle : "";
    overlay.querySelector("[data-homeii-player]").textContent = showPlaying ? player : "";
    if (showPlaying && images.length && config.show_artwork !== false) {
      art.onload = () => {
        if (art.naturalWidth > 0) {
          art.style.display = "block";
          artFallback.style.display = "none";
          setDynamicBackdrop(overlay, art.currentSrc || art.src, art);
        }
      };
      art.onerror = () => {
        const nextIndex = Number(art.dataset.homeiiArtworkIndex || 0) + 1;
        if (images[nextIndex]) {
          art.dataset.homeiiArtworkIndex = String(nextIndex);
          art.style.display = "none";
          artFallback.style.display = "flex";
          art.setAttribute("src", images[nextIndex]);
          return;
        }
        art.removeAttribute("src");
        art.style.display = "none";
        artFallback.style.display = "flex";
        setDynamicBackdrop(overlay, "");
      };
      if (!images.includes(art.getAttribute("src") || "")) {
        art.dataset.homeiiArtworkIndex = "0";
        art.style.display = "none";
        artFallback.style.display = "flex";
        art.setAttribute("src", images[0]);
      } else if (art.complete && art.naturalWidth > 0) {
        art.style.display = "block";
        artFallback.style.display = "none";
        setDynamicBackdrop(overlay, art.currentSrc || art.src, art);
      } else {
        art.style.display = "none";
        artFallback.style.display = "flex";
      }
    } else {
      art.removeAttribute("src");
      art.dataset.homeiiArtworkIndex = "0";
      art.style.display = "none";
      artFallback.style.display = showPlaying ? "flex" : "none";
      setDynamicBackdrop(overlay, "");
    }
  }

  function show(force = false) {
    const screen = state.config || {};
    if (!screen.enabled && !force) return;
    createOverlay();
    render();
    state.visible = true;
    state.visibleByCommand = Boolean(force);
    state.overlay.style.display = "flex";
    clearInterval(state.clockTimer);
    state.clockTimer = window.setInterval(render, 1000);
  }

  function hide() {
    state.lastActivityAt = Date.now();
    state.visible = false;
    state.visibleByCommand = false;
    clearInterval(state.clockTimer);
    state.clockTimer = 0;
    if (state.overlay) state.overlay.style.display = "none";
  }

  function activity() {
    hide();
    scheduleIdleCheck();
  }

  function scheduleIdleCheck() {
    clearTimeout(state.idleTimer);
    const timeoutSeconds = Math.max(15, Number(state.config?.timeout_seconds || state.config?.config?.timeout_seconds || 90) || 90);
    const waitMs = Math.max(1000, timeoutSeconds * 1000 - (Date.now() - state.lastActivityAt));
    state.idleTimer = window.setTimeout(() => {
      if (Date.now() - state.lastActivityAt >= timeoutSeconds * 1000 - 250) show();
      scheduleIdleCheck();
    }, waitMs);
  }

  async function refreshConfig() {
    const hass = findHass();
    if (!hass?.callWS) {
      state.status = "waiting_for_hass";
      state.lastError = "Home Assistant frontend object was not found yet.";
      scheduleIdleCheck();
      return;
    }
    state.hass = hass;
    try {
      state.config = await hass.callWS({ type: "homeii_flow/screensaver/get", source: "system_screensaver_agent" });
      state.lastRefreshAt = Date.now();
      state.lastError = "";
      state.status = state.config?.enabled ? "enabled" : "disabled";
      const commandOpened = handleShowRequest(state.config);
      if (!state.config?.enabled && !state.visibleByCommand && !commandOpened) hide();
      scheduleIdleCheck();
    } catch (error) {
      state.status = "error";
      state.lastError = error?.message || String(error || "Unknown error");
      console.debug("[HOMEii Flow] System screensaver unavailable", error);
      scheduleIdleCheck();
    }
  }

  function start() {
    ["pointerdown", "pointermove", "keydown", "touchstart", "wheel", "scroll"].forEach((eventName) => {
      window.addEventListener(eventName, activity, { passive: true, capture: true });
    });
    ["location-changed", "popstate", "hashchange", "visibilitychange"].forEach((eventName) => {
      window.addEventListener(eventName, () => {
        hide();
        window.setTimeout(refreshConfig, 250);
      }, { passive: true });
    });
    refreshConfig();
    state.refreshTimer = window.setInterval(refreshConfig, 2500);
    scheduleIdleCheck();
  }

  window.__homeiiFlowSystemScreensaver = {
    version: HOMEII_SYSTEM_SCREENSAVER_VERSION,
    refresh: refreshConfig,
    show,
    hide,
    state,
  };

  if (document.readyState === "loading") {
    document.addEventListener("DOMContentLoaded", start, { once: true });
  } else {
    start();
  }
})();
