"use strict";

(() => {
  if (!window.FoxMiniPlayer?.playNext) return;
  const preferenceKey = "foxtube.autoplay.v1";
  let enabled = true;
  try { enabled = localStorage.getItem(preferenceKey) !== "false"; } catch { /* Memory fallback. */ }
  let active;
  const viewer = () => document.querySelector('meta[name="foxtube-viewer"]')?.content;
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/i;
  const playable = (data, id) => data?.id === id && uuid.test(id) &&
    data.playback_url === `/media/videos/${id}/playback`;
  const controls = () => {
    document.querySelectorAll("[data-autoplay-control]").forEach((element) => { element.hidden = false; });
    document.querySelectorAll("[data-autoplay-toggle]").forEach((input) => { input.checked = enabled; });
  };
  const cancel = (state, mark = true) => {
    if (!state) return;
    state.cancelled = mark;
    state.attempt += 1;
    state.check?.abort();
    clearInterval(state.timer);
    state.timer = undefined;
    state.pending = false;
    state.overlay.hidden = true;
    state.root.classList.remove("is-awaiting-next");
  };
  const canAdvance = (state) => active === state && state.root.isConnected &&
    state.video.ended && !state.video.loop && enabled && !state.cancelled &&
    !document.hidden && viewer() === state.viewer && window.FoxTubePage?.canNavigate();
  const showFailure = (state, text) => {
    if (active !== state || state.cancelled) return;
    clearInterval(state.timer);
    state.timer = undefined;
    state.pending = false;
    state.status.textContent = text;
    state.now.disabled = true;
  };
  const advance = async (state) => {
    if (!canAdvance(state) || state.pending || !state.next) return;
    clearInterval(state.timer);
    state.timer = undefined;
    state.pending = true;
    state.now.disabled = true;
    state.status.textContent = "Проверяем доступ…";
    state.count.textContent = "0";
    state.check?.abort();
    state.check = new AbortController();
    const attempt = ++state.attempt;
    const guard = () => attempt === state.attempt && canAdvance(state);
    const options = { credentials: "same-origin", mode: "same-origin", cache: "no-store", signal: state.check.signal };
    try {
      const sessionResponse = await fetch("/api/v1/auth/session", options);
      if (!guard()) return;
      const session = sessionResponse.ok ? await sessionResponse.json() : null;
      if (!guard()) return;
      if (!session || (session.user?.id || "anonymous") !== state.viewer) {
        cancel(state);
        window.FoxMiniPlayer.close();
        return;
      }
      const sourceResponse = await fetch(`/api/v1/videos/${state.id}`, options);
      if (!guard()) return;
      const source = sourceResponse.ok ? await sourceResponse.json() : null;
      if (!guard()) return;
      if (!playable(source, state.id)) {
        showFailure(state, "Это видео больше недоступно. Автопереход остановлен.");
        return;
      }
      const response = await fetch(`/api/v1/videos/${state.next.id}`, options);
      if (!guard()) return;
      const next = response.ok ? await response.json() : null;
      if (!guard()) return;
      if (!playable(next, state.next.id)) {
        showFailure(state, "Следующее видео недоступно. Выберите другой ролик.");
        return;
      }
      const changed = await window.FoxMiniPlayer.playNext(next, state.root, guard);
      if (!changed && guard()) showFailure(state, "Не удалось открыть следующее видео. Повторите переход вручную.");
    } catch (error) {
      if (guard() && error?.name !== "AbortError") showFailure(state, "Нет соединения. Автопереход остановлен.");
    }
  };
  const begin = (state) => {
    if (!canAdvance(state) || !state.next || state.pending || state.timer) return;
    state.overlay.hidden = false;
    state.root.classList.add("is-awaiting-next");
    state.title.textContent = state.next.title;
    state.status.textContent = "";
    state.now.disabled = false;
    state.count.textContent = "5";
    const deadline = performance.now() + 5000;
    state.timer = setInterval(() => {
      if (!canAdvance(state)) { cancel(state); return; }
      const seconds = Math.max(0, Math.ceil((deadline - performance.now()) / 1000));
      if (state.count.textContent !== String(seconds)) state.count.textContent = String(seconds);
      if (!seconds) advance(state);
    }, 100);
  };
  const syncPlayer = () => {
    controls();
    const player = window.FoxMiniPlayer.getPlayer();
    if (active?.root === player?.root) return;
    if (active) { cancel(active); active.queue.abort(); active.events.abort(); }
    active = undefined;
    if (!player || !uuid.test(player.id)) return;
    const root = player.root;
    const get = (name) => root.querySelector(`[data-up-next${name ? `-${name}` : ""}]`);
    const overlay = get("");
    if (!overlay) return;
    const state = { ...player, viewer: viewer(), overlay, title: get("title"), count: get("countdown"),
      status: get("status"), now: get("now"), attempt: 0, cancelled: false, pending: false,
      queue: new AbortController(), events: new AbortController() };
    active = state;
    const listen = (element, event, callback) => element.addEventListener(event, callback, { signal: state.events.signal });
    listen(state.video, "ended", () => begin(state));
    listen(state.video, "play", () => cancel(state, false));
    listen(state.video, "seeking", () => cancel(state, false));
    listen(state.video, "error", () => cancel(state));
    listen(get("cancel"), "click", () => {
      cancel(state);
      root.focus({ preventScroll: true });
    });
    listen(state.now, "click", () => advance(state));
    listen(overlay, "keydown", (event) => {
      if (event.key === "Escape") { event.preventDefault(); event.stopPropagation(); cancel(state); root.focus({ preventScroll: true }); }
    });
    fetch(`/api/v1/videos/${state.id}/recommendations?limit=12`, {
      credentials: "same-origin", mode: "same-origin", cache: "no-store", signal: state.queue.signal,
    }).then(async (response) => {
      const result = response.ok ? await response.json() : null;
      if (active !== state) return;
      state.next = result?.items?.find((item) => item.id !== state.id && playable(item, item.id));
      begin(state);
    }).catch(() => { /* A failed recommendation request never starts a transition. */ });
  };
  const setEnabled = (value, save = true) => {
    enabled = value;
    if (save) { try { localStorage.setItem(preferenceKey, String(enabled)); } catch { /* Memory fallback. */ } }
    controls();
    if (active) {
      cancel(active, !enabled);
      if (enabled) begin(active);
    }
  };
  document.addEventListener("change", (event) => {
    if (event.target.matches("[data-autoplay-toggle]")) setEnabled(event.target.checked);
    if (event.target.matches("[data-fox-loop]") && active?.video.loop) cancel(active);
  });
  document.addEventListener("foxtube:navigation-start", (event) => {
    if (!event.detail?.automatic && active) cancel(active, active.video.ended);
  });
  document.addEventListener("foxtube:session-change", () => cancel(active));
  document.addEventListener("visibilitychange", () => { if (document.hidden) cancel(active); });
  window.addEventListener("pagehide", () => cancel(active));
  window.addEventListener("storage", (event) => {
    if (event.key === preferenceKey || event.key === null) {
      try { setEnabled(localStorage.getItem(preferenceKey) !== "false", false); } catch { /* Keep memory preference. */ }
    }
  });
  document.addEventListener("foxtube:player-change", syncPlayer);
  document.addEventListener("foxtube:page-ready", syncPlayer);
  syncPlayer();
})();
