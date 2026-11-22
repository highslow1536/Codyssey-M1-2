(() => {
  const storageKey = "marketnote-theme";
  const root = document.documentElement;
  let dark = false;
  try {
    dark = localStorage.getItem(storageKey) === "dark";
  } catch {
    // A restricted browser can still switch the theme for the current page.
  }

  function applyTheme() {
    root.dataset.theme = dark ? "dark" : "light";
    document.querySelector('meta[name="theme-color"]').content = dark
      ? "#141b18"
      : "#183e34";
  }

  applyTheme();
  document.addEventListener("DOMContentLoaded", () => {
    const toggle = document.getElementById("theme-toggle");
    toggle.setAttribute("aria-pressed", String(dark));
    toggle.addEventListener("click", () => {
      dark = !dark;
      applyTheme();
      toggle.setAttribute("aria-pressed", String(dark));
      try {
        localStorage.setItem(storageKey, root.dataset.theme);
      } catch {
        // The theme is already applied even if persisting it is unavailable.
      }
    });
  });
})();
