// Loaded synchronously in <head> so a saved light theme applies before first paint.
(function () {
  try {
    var saved = window.localStorage.getItem("loan-lab-theme");
    if (saved === "light" || saved === "dark") {
      document.documentElement.setAttribute("data-theme", saved);
    }
  } catch (error) {
    // Storage unavailable (privacy mode); keep the dark default.
  }
})();
