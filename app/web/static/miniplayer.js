"use strict";

(() => {
  const host = document.querySelector('[data-miniplayer]');
  const mount = host?.querySelector('[data-mini-mount]');
  if (!host || !mount || !window.FoxPlayer) return;
  const title = host.querySelector('[data-mini-title]');
  const notice = document.querySelector('[data-mini-notice]');
  const identity = (doc = document) => doc.querySelector('meta[name="foxtube-viewer"]')?.content;
  const uuid = '[0-9a-f-]{36}';
  const routes = new RegExp(`^/(?:$|search$|account$|upload$|studio$|subscriptions$|playlists$|moderation$|admin$|(?:watch|channels|playlists)/${uuid}$|@[^/]+$)`, 'i');
  const allowed = (url) => url.origin === location.origin && routes.test(url.pathname);
  let current = null;
  let navigation;
  let navigationId = 0;
  let validation;
  const originalScrollRestoration = history.scrollRestoration;

  const announce = (text = '') => {
    notice.textContent = text;
    notice.hidden = !text;
  };
  const occupied = () => Boolean(current && (current.started || current.mini));
  const cancelNavigation = () => {
    navigationId += 1; navigation?.abort();
    document.dispatchEvent(new Event('foxtube:navigation-end'));
  };
  const syncLayout = () => {
    if ('scrollRestoration' in history) history.scrollRestoration = occupied() ? 'manual' : originalScrollRestoration;
    document.body.classList.toggle('has-mini-player', Boolean(current?.mini));
    if (current?.mini) document.body.style.setProperty('--mini-player-height', `${host.offsetHeight + 32}px`);
    else document.body.style.removeProperty('--mini-player-height');
  };
  const resume = async (state) => {
    try { await state.video.play(); }
    catch (error) {
      if (current !== state || error?.name === 'AbortError') return;
      if (error?.name === 'NotAllowedError' && !state.video.muted) {
        state.video.muted = true;
        try { await state.video.play(); }
        catch { announce('Нажмите воспроизведение в миниплеере, чтобы продолжить.'); }
      } else announce('Нажмите воспроизведение в миниплеере, чтобы продолжить.');
    }
  };
  const move = (state, parent, before = null) => {
    const playing = !state.video.paused && !state.video.ended;
    // Atomic moves retain focus/playback; older engines keep the same media
    // element and decoder too, but may require resuming its playback.
    if (parent.moveBefore && state.root.isConnected && parent.isConnected) parent.moveBefore(state.root, before);
    else parent.insertBefore(state.root, before);
    if (playing && state.video.paused) resume(state);
    state.root.dispatchEvent(new Event('foxtube:player-mode'));
  };
  const close = ({ cancel = true, focus = false } = {}) => {
    if (cancel) cancelNavigation();
    validation?.abort();
    const state = current;
    current = null;
    if (state) {
      state.events.abort();
      if (document.pictureInPictureElement === state.video) document.exitPictureInPicture().catch(() => {});
      if (document.fullscreenElement === state.root) document.exitFullscreen().catch(() => {});
      window.FoxPlayer.dispose(state.root);
      state.video.pause();
      state.video.removeAttribute('src');
      state.video.removeAttribute('poster');
      state.video.querySelectorAll('source, track').forEach((element) => element.remove());
      state.video.load();
      state.root.remove();
      const link = document.querySelector('[data-player-return]');
      if (link) { link.hidden = false; link.textContent = 'Открыть видео заново'; }
    }
    host.hidden = true;
    title.textContent = '';
    title.href = '/';
    syncLayout();
    if (focus) document.querySelector('[data-player-return], #main-content')?.focus({ preventScroll: true });
    if (state) document.dispatchEvent(new Event('foxtube:player-change'));
  };
  const setMini = () => {
    const state = current;
    if (!state) return;
    window.FoxPreviews?.reset();
    state.mini = true;
    state.root.classList.add('is-mini-player');
    host.hidden = false;
    title.textContent = state.title;
    title.href = state.url;
    const link = document.querySelector('[data-player-return]');
    if (link) { link.hidden = false; link.textContent = 'Вернуть основной плеер'; }
    if (state.root.parentElement !== mount) move(state, mount);
    if (document.fullscreenElement === state.root) document.exitFullscreen().catch(() => {});
    syncLayout();
  };
  const attach = (slot) => {
    const state = current;
    if (!state || !slot) return;
    state.mini = false;
    state.root.classList.remove('is-mini-player');
    const link = slot.querySelector('[data-player-return]');
    if (link) link.hidden = true;
    move(state, slot, link);
    slot.closest('.watch-page')?.classList.toggle('is-theater', state.root.classList.contains('is-theater'));
    host.hidden = true;
    syncLayout();
  };
  const adopt = (root, { started = false } = {}) => {
    if (!root || current?.root === root) return;
    close({ cancel: false });
    const video = root.querySelector('video');
    const id = root.dataset.playerVideo;
    if (!video || !new RegExp(`^${uuid}$`, 'i').test(id || '')) return;
    current = { root, video, id, started, mini: false, viewer: identity(),
      title: video.getAttribute('aria-label') || 'Видео', url: `/watch/${id}`, events: new AbortController() };
    const state = current;
    video.addEventListener('play', () => { state.started = true; window.FoxPreviews?.reset(); }, { signal: state.events.signal });
    document.dispatchEvent(new Event('foxtube:player-change'));
  };
  const validMedia = (data, state) => {
    if (data?.id !== state.id || typeof data.playback_url !== 'string') return false;
    const url = new URL(data.playback_url, location.href);
    return url.origin === location.origin && url.pathname === `/media/videos/${state.id}/playback`;
  };
  const validate = async (state, { session = false, signal } = {}) => {
    if (!state) return true;
    try {
      if (session) {
        const response = await fetch('/api/v1/auth/session', { credentials: 'same-origin', mode: 'same-origin', cache: 'no-store', signal });
        const data = response.ok ? await response.json() : null;
        if (current !== state) return false;
        if (!data || (data.user?.id || 'anonymous') !== state.viewer) {
          close({ cancel: false }); announce('Аккаунт изменился. Откройте видео заново.'); return false;
        }
      }
      const response = await fetch(`/api/v1/videos/${state.id}`, { credentials: 'same-origin', mode: 'same-origin', cache: 'no-store', signal });
      if (current !== state) return false;
      if ([401, 403, 404].includes(response.status)) {
        close({ cancel: false }); announce('Видео больше недоступно.'); return false;
      }
      if (!response.ok) throw new Error('access');
      const data = await response.json();
      if (current !== state) return false;
      if (!validMedia(data, state)) {
        close({ cancel: false }); announce('Видео больше недоступно.'); return false;
      }
      state.title = data.title;
      title.textContent = state.title;
      return true;
    } catch (error) {
      if (current === state && error?.name !== 'AbortError') {
        state.video.pause(); announce('Не удалось проверить доступ к видео. Проверьте соединение и повторите переход.');
      }
      return false;
    }
  };
  const revalidate = () => {
    if (!occupied() || document.hidden) return;
    validation?.abort(); validation = new AbortController();
    validate(current, { session: true, signal: validation.signal });
  };
  const native = (url) => { close(); location.assign(url.href); };
  const rememberScroll = (position) => {
    const state = history.state && typeof history.state === 'object' ? history.state : {};
    history.replaceState({ ...state, foxScroll: position }, '', location.href);
  };
  const navigate = async (target, { pop = false, scroll = [0, 0], automatic = false, guard = () => true, autoplay } = {}) => {
    const url = new URL(target, location.href);
    if (!guard()) return false;
    if (!allowed(url) || !window.FoxTubePage?.canNavigate()) { if (!automatic) native(url); return false; }
    cancelNavigation();
    const attempt = navigationId;
    navigation = new AbortController();
    const { signal } = navigation;
    const validAttempt = () => attempt === navigationId && guard();
    const state = current;
    const departingScroll = [scrollX, scrollY];
    announce();
    document.dispatchEvent(new CustomEvent('foxtube:navigation-start', { detail: { automatic } }));
    try {
      const response = await fetch(url.href, { headers: { Accept: 'text/html' }, credentials: 'same-origin', mode: 'same-origin', cache: 'no-store', signal });
      if (!validAttempt()) return false;
      const final = new URL(response.url);
      if (!response.ok || !allowed(final) || !response.headers.get('content-type')?.startsWith('text/html')) { if (!automatic) native(url); return false; }
      const html = await response.text();
      if (!validAttempt()) return false;
      const doc = new DOMParser().parseFromString(html, 'text/html');
      const required = ['#main-content', '.site-header', '#site-sidebar', '[data-menu-backdrop]'];
      if (doc.querySelector('meta[name="foxtube-shell"]')?.content !== 'miniplayer-v1' || required.some((selector) => !doc.querySelector(selector)) || !identity(doc)) { if (!automatic) native(url); return false; }
      if (!window.FoxTubePage.canNavigate()) { if (!automatic) native(url); return false; }
      if (automatic && (identity(doc) !== identity() || !doc.querySelector(`[data-player-video="${autoplay?.id}"]`))) return false;
      if (state && current === state) {
        if (identity(doc) !== state.viewer) { close({ cancel: false }); announce('Аккаунт изменился. Откройте видео заново.'); }
        else await validate(state, { signal });
      }
      if (!validAttempt()) return false;
      if (!window.FoxTubePage.canNavigate()) { if (!automatic) native(url); return false; }
      const nextPlayer = doc.querySelector('#main-content [data-fox-player]');
      const same = current && nextPlayer?.dataset.playerVideo === current.id;
      if (current) {
        if (nextPlayer && !same) close({ cancel: false });
        else setMini();
      }
      // Scripts remain in the original document. New server DOM is data only;
      // each page module has explicit teardown/reinitialization hooks.
      document.dispatchEvent(new Event('foxtube:page-leave'));
      for (const selector of required) {
        const next = doc.querySelector(selector);
        next.querySelectorAll('script').forEach((script) => script.remove());
        if (same && selector === '#main-content') next.querySelector('[data-fox-player]')?.remove();
        document.querySelector(selector).replaceWith(document.importNode(next, true));
      }
      document.title = doc.title;
      document.querySelector('meta[name="csrf-token"]')?.remove();
      const csrf = doc.querySelector('meta[name="csrf-token"]');
      if (csrf) document.head.append(document.importNode(csrf, true));
      document.querySelector('meta[name="foxtube-viewer"]').content = identity(doc);
      if (!pop) {
        rememberScroll(departingScroll);
        history.pushState({ foxScroll: [0, 0] }, '', final.href);
      }
      if (same) attach(document.querySelector(`[data-player-slot="${current.id}"]`));
      document.dispatchEvent(new Event('foxtube:page-ready'));
      if (!current) adopt(document.querySelector('#main-content [data-fox-player]'));
      if (automatic && current?.id === autoplay?.id) {
        current.video.volume = autoplay.volume;
        current.video.muted = autoplay.muted;
        current.video.playbackRate = autoplay.rate;
        current.root.dispatchEvent(new Event('foxtube:play-request'));
      }
      window.scrollTo(...scroll);
      document.querySelector('#main-content')?.focus({ preventScroll: true });
      return true;
    } catch (error) {
      if (attempt === navigationId && error?.name !== 'AbortError') {
        if (pop) native(url);
        else announce('Не удалось открыть страницу. Проверьте соединение и повторите переход.');
      }
    } finally {
      if (attempt === navigationId) document.dispatchEvent(new Event('foxtube:navigation-end'));
    }
    return false;
  };
  const restore = () => {
    if (!current) return;
    const slot = document.querySelector(`[data-player-slot="${current.id}"]`);
    if (slot && location.pathname === current.url) { attach(slot); current.root.focus({ preventScroll: true }); slot.scrollIntoView({ block: 'center' }); }
    else navigate(current.url);
  };
  host.querySelector('[data-mini-close]').addEventListener('click', () => { close({ focus: true }); announce(); });
  host.querySelector('[data-mini-expand]').addEventListener('click', restore);
  title.addEventListener('click', (event) => {
    if (event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    event.preventDefault(); restore();
  });
  document.addEventListener('click', (event) => {
    if (event.defaultPrevented || event.button || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    if (event.target.closest('[data-fox-mini]')) { event.preventDefault(); setMini(); return; }
    const link = event.target.closest('a[href]');
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self') || !occupied()) return;
    const url = new URL(link.href);
    if (url.pathname === location.pathname && url.search === location.search) {
      if (link.matches('[data-player-return]')) { event.preventDefault(); restore(); }
      return;
    }
    if (allowed(url) && window.FoxTubePage?.canNavigate()) { event.preventDefault(); navigate(url); }
  });
  document.addEventListener('submit', (event) => {
    const form = event.target;
    if (event.defaultPrevented || !occupied() || !(form instanceof HTMLFormElement) || form.method !== 'get' || (form.target && form.target !== '_self')) return;
    const url = new URL(form.action);
    if (!allowed(url) || !window.FoxTubePage?.canNavigate()) return;
    url.search = new URLSearchParams(new FormData(form, event.submitter)).toString();
    event.preventDefault(); navigate(url);
  });
  window.addEventListener('popstate', (event) => {
    if (!occupied()) { location.reload(); return; }
    navigate(location.href, { pop: true, scroll: event.state?.foxScroll || [0, 0] });
  });
  window.addEventListener('pagehide', (event) => {
    cancelNavigation(); validation?.abort();
    if (!event.persisted) close({ cancel: false });
    else current?.video.pause();
  });
  window.addEventListener('pageshow', (event) => { if (event.persisted) revalidate(); });
  window.addEventListener('focus', revalidate);
  document.addEventListener('visibilitychange', revalidate);
  document.addEventListener('foxtube:session-change', revalidate);
  window.setInterval(() => { if (current?.mini) revalidate(); }, 30000);
  new ResizeObserver(syncLayout).observe(host);
  const preview = ({ video, link }) => {
    cancelNavigation();
    close({ cancel: false });
    const root = document.querySelector('[data-mini-template]').content.firstElementChild.cloneNode(true);
    const id = link.getAttribute('href').split('/').pop();
    root.dataset.playerVideo = id;
    root.dataset.preserveState = 'true';
    root.querySelector('[data-fox-settings-panel]').id = `fox-player-settings-${id}`;
    root.querySelector('[data-fox-settings]').setAttribute('aria-controls', `fox-player-settings-${id}`);
    const label = link.closest('.video-card').querySelector('h2').textContent.trim();
    root.setAttribute('aria-label', `Видеоплеер: ${label}`);
    // Connect the controls before moving the already-playing preview into them.
    root.querySelector('video').remove();
    mount.append(root);
    if (root.moveBefore && video.isConnected) root.moveBefore(video, root.firstChild);
    else root.prepend(video);
    video.className = 'video-player';
    video.removeAttribute('aria-hidden');
    video.removeAttribute('tabindex');
    video.setAttribute('aria-label', label);
    video.preload = 'metadata';
    video.loop = false;
    window.FoxPlayer.initialize(mount);
    adopt(root, { started: true });
    setMini();
    if (video.paused) resume(current);
  };
  const playNext = async (data, root, guard) => {
    const state = current;
    if (!state || state.root !== root || !guard() || !new RegExp(`^${uuid}$`, 'i').test(data?.id || '') || !validMedia(data, { id: data.id })) return false;
    const preferences = { id: data.id, volume: state.video.volume, muted: state.video.muted, rate: state.video.playbackRate };
    if (!state.mini) return navigate(`/watch/${data.id}`, { automatic: true, guard, autoplay: preferences });
    // A mini queue advances without replacing the page the viewer is browsing.
    const next = document.querySelector('[data-mini-template]').content.firstElementChild.cloneNode(true);
    next.dataset.playerVideo = data.id;
    next.dataset.preserveState = 'true';
    next.setAttribute('aria-label', `Видеоплеер: ${data.title}`);
    next.querySelector('[data-fox-settings-panel]').id = `fox-player-settings-${data.id}`;
    next.querySelector('[data-fox-settings]').setAttribute('aria-controls', `fox-player-settings-${data.id}`);
    const video = next.querySelector('video');
    video.setAttribute('aria-label', data.title);
    video.querySelector('source').src = data.playback_url;
    if (data.poster_url === `/media/videos/${data.id}/poster`) video.poster = data.poster_url;
    video.volume = preferences.volume;
    video.muted = preferences.muted;
    video.playbackRate = preferences.rate;
    close({ cancel: false });
    mount.append(next);
    window.FoxPlayer.initialize(mount);
    adopt(next, { started: true });
    setMini();
    next.dispatchEvent(new Event('foxtube:play-request'));
    return true;
  };
  window.FoxMiniPlayer = { occupied, preview, close, playNext,
    getPlayer: () => current ? { root: current.root, video: current.video, id: current.id, mini: current.mini } : null };
  window.FoxTubePage.register(() => {
    document.querySelectorAll('[data-preview-mini]').forEach((button) => { button.hidden = false; });
  });
  adopt(document.querySelector('#main-content [data-fox-player]'));
})();
