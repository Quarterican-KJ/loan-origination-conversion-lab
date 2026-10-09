"use strict";

const THEME_KEY = "loan-lab-theme";
const root = document.documentElement;

function currentTheme() {
  return root.getAttribute("data-theme") === "light" ? "light" : "dark";
}

function syncThemeButtons() {
  const next = currentTheme() === "dark" ? "light" : "dark";
  document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
    button.setAttribute("aria-label", `Switch to ${next} mode`);
  });
}

function setTheme(theme) {
  root.setAttribute("data-theme", theme);
  try {
    window.localStorage.setItem(THEME_KEY, theme);
  } catch (error) {
    // Preference simply won't persist.
  }
  syncThemeButtons();
}

function setSidebar(open) {
  document.body.classList.toggle("sidebar-open", open);
  document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
    button.setAttribute("aria-expanded", String(open));
  });
  document.querySelectorAll("[data-sidebar-close]").forEach((backdrop) => {
    backdrop.hidden = !open;
  });
}

document.querySelectorAll("[data-theme-toggle]").forEach((button) => {
  button.addEventListener("click", () => setTheme(currentTheme() === "dark" ? "light" : "dark"));
});
syncThemeButtons();

document.querySelectorAll("[data-sidebar-toggle]").forEach((button) => {
  button.addEventListener("click", () => setSidebar(!document.body.classList.contains("sidebar-open")));
});
document.querySelectorAll("[data-sidebar-close]").forEach((backdrop) => {
  backdrop.addEventListener("click", () => setSidebar(false));
});
document.addEventListener("keydown", (event) => {
  if (event.key === "Escape") setSidebar(false);
});

// Wide tables scroll inside .table-scroll. Only while a table overflows does its region get a
// visible hint and a tab stop, so keyboard users can focus it and scroll with the arrow keys.
let scrollHints = 0;

function syncScrollRegion(region) {
  const overflowing = region.scrollWidth > region.clientWidth + 1;
  let hint = region.previousElementSibling;
  if (!hint || !hint.hasAttribute("data-scroll-hint")) {
    if (!overflowing) return;
    hint = document.createElement("p");
    hint.className = "scroll-hint";
    hint.id = `scroll-hint-${++scrollHints}`;
    hint.setAttribute("data-scroll-hint", "");
    hint.textContent = "Scroll sideways to see all columns: swipe, or focus the table and use the arrow keys.";
    region.before(hint);
  }
  hint.hidden = !overflowing;
  region.toggleAttribute("data-overflowing", overflowing);
  if (overflowing) {
    region.tabIndex = 0;
    if (!region.hasAttribute("role")) region.setAttribute("role", "region");
    if (!region.hasAttribute("aria-label")) region.setAttribute("aria-label", "Scrollable table");
    region.setAttribute("aria-describedby", hint.id);
  } else {
    region.removeAttribute("tabindex");
    region.removeAttribute("aria-describedby");
  }
}

const scrollRegions = document.querySelectorAll(".table-scroll");
scrollRegions.forEach(syncScrollRegion);
if ("ResizeObserver" in window) {
  const observer = new ResizeObserver((entries) => entries.forEach((entry) => syncScrollRegion(entry.target)));
  scrollRegions.forEach((region) => observer.observe(region));
}

// Filter selects apply immediately; the Apply button remains for keyboard and no-JS use.
document.querySelectorAll("form[data-auto-submit] select").forEach((select) => {
  select.addEventListener("change", () => select.form.requestSubmit());
});
