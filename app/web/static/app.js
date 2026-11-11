(() => {
  "use strict";

  const initializers = [];
  const sidebarStorageKey = "foxtube.sidebar.expanded.v1";
  let desktopSidebarExpanded = true;
  try {
    const stored = window.localStorage.getItem(sidebarStorageKey);
    if (stored === "true" || stored === "false") desktopSidebarExpanded = stored === "true";
  } catch { /* The in-memory preference still survives internal navigation. */ }
  let currentPage;
  const makePage = () => {
    const controller = new AbortController();
    const timers = new Set();
    const cleanups = [];
    const dirtyForms = new WeakSet();
    const page = {
      get active() { return !controller.signal.aborted; },
      check() {
        if (!page.active) throw new DOMException("Page left", "AbortError");
      },
      listen(target, type, handler, options = {}) {
        if (!target) return;
        target.addEventListener(type, (event) => {
          if (page.active) return handler(event);
        }, { ...options, signal: controller.signal });
      },
      async fetch(url, options = {}) {
        page.check();
        const response = await fetch(url, { ...options, signal: controller.signal });
        page.check();
        return response;
      },
      timeout(callback, delay) {
        if (!page.active) return;
        const timer = window.setTimeout(() => {
          timers.delete(timer);
          if (page.active) callback();
        }, delay);
        timers.add(timer);
        return timer;
      },
      cleanup(callback) { cleanups.push(callback); },
      clean(form) { dirtyForms.delete(form); },
      canNavigate() {
        return page.active && !document.querySelector('form[aria-busy="true"], button[aria-busy="true"]')
          && ![...document.forms].some((form) => dirtyForms.has(form));
      },
      leave() {
        if (!page.active) return;
        // Abort XHR while its settlement listeners are still attached.
        for (const cleanup of cleanups) cleanup();
        controller.abort();
        for (const timer of timers) window.clearTimeout(timer);
        timers.clear();
      },
    };
    const markDirty = (event) => {
      const form = event.target.form;
      if (form && form.getAttribute("method")?.toLowerCase() !== "get") dirtyForms.add(form);
    };
    page.listen(document, "input", markDirty);
    page.listen(document, "change", markDirty);
    page.listen(document, "reset", (event) => page.clean(event.target));
    return page;
  };
  currentPage = makePage();
  // Messages contain no account data and only trigger a fresh server check.
  let sessionChannel;
  try {
    sessionChannel = new BroadcastChannel("foxtube-session");
    sessionChannel.addEventListener("message", () => {
      document.dispatchEvent(new Event("foxtube:session-change"));
    });
  } catch { /* Focus/navigation checks still work without BroadcastChannel. */ }
  window.FoxTubePage = {
    register(initialize) {
      initializers.push(initialize);
      if (currentPage.active) initialize(currentPage);
    },
    canNavigate: () => currentPage.canNavigate(),
    sessionChanged() {
      document.dispatchEvent(new Event("foxtube:session-change"));
      sessionChannel?.postMessage("changed");
    },
  };
  document.addEventListener("foxtube:page-leave", () => currentPage.leave());
  document.addEventListener("foxtube:page-ready", () => {
    currentPage.leave();
    currentPage = makePage();
    for (const initialize of initializers) initialize(currentPage);
  });

  window.FoxTubePage.register((page) => {
    const toggle = document.querySelector("[data-menu-toggle]");
    const backdrop = document.querySelector("[data-menu-backdrop]");
    const sidebar = document.querySelector("#site-sidebar");
    const search = document.querySelector("#site-search");
    const wideScreen = window.matchMedia("(min-width: 900px)");

    if (!toggle || !backdrop || !sidebar) return;

    let mobileMenuOpen = false;

    const renderMenu = () => {
      const menuOpen = wideScreen.matches ? desktopSidebarExpanded : mobileMenuOpen;
      document.body.dataset.sidebarOpen = String(menuOpen);
      toggle.setAttribute("aria-expanded", String(menuOpen));
      toggle.setAttribute("aria-label", menuOpen ? "Свернуть меню" : "Открыть меню");
      backdrop.hidden = wideScreen.matches || !menuOpen;
      sidebar.inert = !wideScreen.matches && !menuOpen;
    };

    const closeMobileMenu = () => {
      if (wideScreen.matches || !mobileMenuOpen) return;
      mobileMenuOpen = false;
      renderMenu();
      toggle.focus();
    };

    page.listen(toggle, "click", () => {
      if (wideScreen.matches) {
        desktopSidebarExpanded = !desktopSidebarExpanded;
        try {
          window.localStorage.setItem(sidebarStorageKey, String(desktopSidebarExpanded));
        } catch { /* Storage can be disabled without disabling the menu. */ }
      } else {
        mobileMenuOpen = !mobileMenuOpen;
      }
      renderMenu();
    });
    page.listen(backdrop, "click", closeMobileMenu);
    page.listen(wideScreen, "change", () => {
      mobileMenuOpen = false;
      renderMenu();
    });
    page.listen(document, "keydown", (event) => {
      if (event.key === "Escape") closeMobileMenu();
      if (event.key !== "/" || !search || event.ctrlKey || event.altKey || event.metaKey) return;
      const target = event.target;
      if (target instanceof HTMLElement && (target.matches("input, textarea, select") || target.isContentEditable)) return;
      event.preventDefault();
      search.focus();
    });

    page.cleanup(() => {
      mobileMenuOpen = false;
      renderMenu();
    });

    renderMenu();
  });
})();
