"use strict";

(() => {
  const indicator = document.querySelector('[data-navigation-loading]');
  if (!indicator) return;
  let timer;
  const reset = () => { window.clearTimeout(timer); indicator.hidden = true; };
  const start = () => {
    reset();
    timer = window.setTimeout(() => { indicator.hidden = false; }, 200);
  };
  document.addEventListener('click', (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a[href]');
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) return;
    if (url.pathname === location.pathname && url.search === location.search) return;
    start();
  });
  document.addEventListener('submit', (event) => {
    if (!event.defaultPrevented && event.target.matches('form[method="get"]')) start();
  });
  window.addEventListener('pageshow', reset);
  window.addEventListener('pagehide', reset);
})();
