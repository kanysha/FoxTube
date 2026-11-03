(() => {
  "use strict";

  const toggle = document.querySelector("[data-menu-toggle]");
  const backdrop = document.querySelector("[data-menu-backdrop]");
  const sidebar = document.querySelector("#site-sidebar");
  const search = document.querySelector("#site-search");
  const wideScreen = window.matchMedia("(min-width: 900px)");

  if (!toggle || !backdrop || !sidebar) return;

  let menuOpen = wideScreen.matches;

  const renderMenu = () => {
    document.body.dataset.sidebarOpen = String(menuOpen);
    toggle.setAttribute("aria-expanded", String(menuOpen));
    toggle.setAttribute("aria-label", menuOpen ? "Свернуть меню" : "Открыть меню");
    backdrop.hidden = wideScreen.matches || !menuOpen;
    sidebar.inert = !wideScreen.matches && !menuOpen;
  };

  const closeMobileMenu = () => {
    if (wideScreen.matches || !menuOpen) return;
    menuOpen = false;
    renderMenu();
    toggle.focus();
  };

  toggle.addEventListener("click", () => {
    menuOpen = !menuOpen;
    renderMenu();
  });
  backdrop.addEventListener("click", closeMobileMenu);
  wideScreen.addEventListener("change", () => {
    menuOpen = wideScreen.matches;
    renderMenu();
  });
  document.addEventListener("keydown", (event) => {
    if (event.key === "Escape") closeMobileMenu();
    if (event.key !== "/" || !search || event.ctrlKey || event.altKey || event.metaKey) return;
    const target = event.target;
    if (target instanceof HTMLElement && (target.matches("input, textarea, select") || target.isContentEditable)) return;
    event.preventDefault();
    search.focus();
  });

  renderMenu();
})();
