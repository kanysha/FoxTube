"use strict";

(() => {
  const reducedMotion = window.matchMedia('(prefers-reduced-motion: reduce)');
  const finePointer = window.matchMedia('(hover: hover) and (pointer: fine)');
  let hovered = null;
  let focused = null;
  let active = null;
  let timer;
  const allowed = () => !window.FoxMiniPlayer?.occupied() && !document.hidden && !reducedMotion.matches && !navigator.connection?.saveData;
  const release = (keepVideo = false) => {
    window.clearTimeout(timer);
    if (!active) return;
    const { video, link, observer, frame, sound, progress, events } = active;
    active = null;
    observer.disconnect();
    events.abort();
    if (frame !== undefined) video.cancelVideoFrameCallback(frame);
    if (!keepVideo) {
      video.pause();
      video.removeAttribute('src');
      video.load();
      video.remove();
    }
    sound.remove();
    progress.remove();
    link.classList.remove('is-previewing');
    return { video, link };
  };
  const stop = () => release();
  const start = (link, pin = false) => {
    stop();
    if (!link || (!pin && !allowed())) return;
    // Do not fetch any MP4 while the user merely passes across the grid.
    const create = () => {
      if ((!pin && !allowed()) || !link.isConnected) return;
      const video = document.createElement('video');
      video.className = 'thumbnail-preview';
      video.muted = true;
      video.defaultMuted = true;
      video.playsInline = true;
      video.preload = 'none';
      video.setAttribute('aria-hidden', 'true');
      video.tabIndex = -1;
      const sound = document.createElement('span');
      sound.className = 'thumbnail-preview-sound';
      sound.setAttribute('aria-hidden', 'true');
      const icon = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
      icon.setAttribute('viewBox', '0 0 24 24');
      const path = document.createElementNS('http://www.w3.org/2000/svg', 'path');
      path.setAttribute('d', 'm3 3 18 18M9 5l3-2v6M12 13v8l-6-5H3V8h3M16 9l5 6M21 9l-5 6');
      icon.append(path);
      sound.append(icon);
      const progress = document.createElement('span');
      progress.className = 'thumbnail-preview-progress';
      progress.setAttribute('aria-hidden', 'true');
      const fill = document.createElement('span');
      progress.append(fill);
      const observer = new IntersectionObserver((entries) => {
        if (entries.some((entry) => !entry.isIntersecting)) stop();
      });
      const events = new AbortController();
      const options = { signal: events.signal };
      const state = { video, link, observer, sound, progress, events, frame: undefined };
      active = state;
      observer.observe(link);
      const isCurrent = () => active === state;
      const loop = () => {
        if (!isCurrent() || !Number.isFinite(video.duration) || video.duration <= 0) return;
        const end = video.duration * 0.2;
        if (video.currentTime >= end) video.currentTime = 0;
        fill.style.width = `${Math.min(100, video.currentTime / end * 100)}%`;
      };
      if ('requestVideoFrameCallback' in video) {
        const nextFrame = () => {
          if (!isCurrent()) return;
          loop();
          state.frame = video.requestVideoFrameCallback(nextFrame);
        };
        state.frame = video.requestVideoFrameCallback(nextFrame);
      }
      video.addEventListener('timeupdate', loop, options);
      video.addEventListener('playing', () => {
        if (isCurrent()) link.classList.add('is-previewing');
      }, options);
      video.addEventListener('error', () => { if (isCurrent()) stop(); }, options);
      video.addEventListener('ended', () => {
        if (!isCurrent()) return;
        video.currentTime = 0;
        video.play().catch(() => { if (isCurrent()) stop(); });
      }, options);
      // Reuse the same ACL-protected endpoint as the full player, without tokens.
      video.src = link.dataset.previewSrc;
      link.append(video, sound, progress);
      video.play().catch(() => { if (isCurrent()) stop(); });
      if (pin) window.FoxMiniPlayer.preview(release(true));
    };
    if (pin) create();
    else timer = window.setTimeout(create, 350);
  };
  const update = () => start(hovered || focused);
  document.addEventListener('pointerover', (event) => {
    if (!finePointer.matches || event.pointerType === 'touch') return;
    const link = event.target.closest('.video-thumbnail[data-preview-src]');
    if (!link || link.contains(event.relatedTarget)) return;
    hovered = link;
    update();
  });
  document.addEventListener('pointerout', (event) => {
    if (!hovered || !hovered.contains(event.target) || hovered.contains(event.relatedTarget)) return;
    if (event.relatedTarget?.closest?.('[data-preview-mini]')?.closest('.video-card') === hovered.closest('.video-card')) return;
    hovered = null;
    update();
  });
  document.addEventListener('focusin', (event) => {
    const link = event.target.closest('.video-thumbnail[data-preview-src]');
    if (!link?.matches(':focus-visible')) return;
    focused = link;
    update();
  });
  document.addEventListener('focusout', (event) => {
    if (!focused || !focused.contains(event.target) || focused.contains(event.relatedTarget)) return;
    if (event.relatedTarget?.closest?.('[data-preview-mini]')?.closest('.video-card') === focused.closest('.video-card')) return;
    focused = null;
    update();
  });
  const reset = () => { hovered = null; focused = null; stop(); };
  window.FoxPreviews = { reset };
  document.addEventListener('click', (event) => {
    const button = event.target.closest('[data-preview-mini]');
    if (!button || !window.FoxMiniPlayer) return;
    event.preventDefault();
    const link = button.closest('.video-card').querySelector('.video-thumbnail[data-preview-src]');
    if (active?.link === link) window.FoxMiniPlayer.preview(release(true));
    else start(link, true);
    hovered = null; focused = null;
  });
  document.addEventListener('foxtube:page-leave', reset);
  document.addEventListener('visibilitychange', reset);
  window.addEventListener('pagehide', reset);
  window.addEventListener('blur', reset);
  reducedMotion.addEventListener('change', reset);
  finePointer.addEventListener('change', reset);
  navigator.connection?.addEventListener('change', reset);
})();
