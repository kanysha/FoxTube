"use strict";

window.FoxTubePage.register((page) => {
  const indicator = document.querySelector('[data-navigation-loading]');
  if (!indicator) return;
  let timer;
  const reset = () => { window.clearTimeout(timer); indicator.hidden = true; };
  const start = () => {
    reset();
    timer = page.timeout(() => { indicator.hidden = false; }, 200);
  };
  page.cleanup(reset);
  page.listen(document, 'foxtube:navigation-start', start);
  page.listen(document, 'foxtube:navigation-end', reset);
  page.listen(document, 'click', (event) => {
    if (event.defaultPrevented || event.button !== 0 || event.ctrlKey || event.metaKey || event.shiftKey || event.altKey) return;
    const link = event.target.closest('a[href]');
    if (!link || link.hasAttribute('download') || (link.target && link.target !== '_self')) return;
    const url = new URL(link.href, location.href);
    if (url.origin !== location.origin || !['http:', 'https:'].includes(url.protocol)) return;
    if (url.pathname === location.pathname && url.search === location.search) return;
    start();
  });
  page.listen(document, 'submit', (event) => {
    if (!event.defaultPrevented && event.target.matches('form[method="get"]')) start();
  });
  page.listen(window, 'pageshow', reset);
  page.listen(window, 'pagehide', reset);
});
