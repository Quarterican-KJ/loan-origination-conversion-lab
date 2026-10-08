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

// Filter selects apply immediately; the Apply button remains for keyboard and no-JS use.
document.querySelectorAll("form[data-auto-submit] select").forEach((select) => {
  select.addEventListener("change", () => select.form.requestSubmit());
});
