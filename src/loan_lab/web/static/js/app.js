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
    hint.textContent = "Scroll sideways to see all columns: swipe, use the scrollbar under the table, or focus the table and use the arrow keys.";
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

// Each region is wrapped in a .table-frame that ends with the region's own scrollbar: step
// buttons plus a drawn track and thumb, kept in sync with the region's scrollLeft both ways. It
// never relies on the native scrollbar, which may be hidden or auto-hide. The bar is sticky in
// its frame, so the table crossing the bottom of the viewport has its bar pinned there, and every
// other table's bar sits under its own last row: bars never stack or cover another table.
const scrollRegions = document.querySelectorAll(".table-scroll");
const scrollbars = new Map();
const KEY_STEP = 40;
let regionIds = 0;

function pageStep(region) {
  return Math.max(region.clientWidth * 0.8, KEY_STEP);
}

function stepButton(region, label, direction) {
  const button = document.createElement("button");
  button.type = "button";
  button.className = "table-scrollbar-step";
  button.setAttribute("aria-controls", region.id);
  button.setAttribute("aria-label", `Scroll ${label} ${direction < 0 ? "left" : "right"}`);
  button.textContent = direction < 0 ? "‹" : "›";
  button.addEventListener("click", () => { region.scrollLeft += direction * pageStep(region); });
  return button;
}

function createScrollbar(region) {
  if (!region.id) region.id = `table-scroll-${++regionIds}`;
  const label = region.getAttribute("aria-label") || "table";
  const frame = document.createElement("div");
  frame.className = "table-frame";
  region.before(frame);
  frame.append(region);

  const bar = document.createElement("div");
  bar.className = "table-scrollbar";
  bar.hidden = true;
  bar.setAttribute("data-table-scrollbar", "");
  const track = document.createElement("div");
  track.className = "table-scrollbar-track";
  const thumb = document.createElement("div");
  thumb.className = "table-scrollbar-thumb";
  thumb.tabIndex = 0;
  thumb.setAttribute("role", "scrollbar");
  thumb.setAttribute("aria-controls", region.id);
  thumb.setAttribute("aria-orientation", "horizontal");
  thumb.setAttribute("aria-valuemin", "0");
  thumb.setAttribute("aria-valuemax", "100");
  thumb.setAttribute("aria-label", `${label} columns`);
  track.append(thumb);
  const left = stepButton(region, label, -1);
  const right = stepButton(region, label, 1);
  bar.append(left, track, right);
  frame.append(bar);
  scrollbars.set(region, { frame, bar, track, thumb, left, right });

  // Dragging moves the table by the thumb's distance scaled from track travel to scroll range.
  thumb.addEventListener("pointerdown", (event) => {
    if (event.button !== 0) return;
    event.preventDefault();
    thumb.setPointerCapture(event.pointerId);
    thumb.setAttribute("data-dragging", "");
    const startX = event.clientX;
    const startScroll = region.scrollLeft;
    const move = (moved) => {
      const travel = track.clientWidth - thumb.offsetWidth;
      if (travel > 0) {
        region.scrollLeft = startScroll + (moved.clientX - startX) * (region.scrollWidth - region.clientWidth) / travel;
      }
    };
    const end = () => {
      thumb.removeAttribute("data-dragging");
      thumb.removeEventListener("pointermove", move);
      thumb.removeEventListener("lostpointercapture", end);
    };
    thumb.addEventListener("pointermove", move);
    thumb.addEventListener("lostpointercapture", end);
  });
  // A press on the track pages toward it, like a native scrollbar.
  track.addEventListener("pointerdown", (event) => {
    if (event.button !== 0 || event.target !== track) return;
    event.preventDefault();
    const direction = event.clientX < thumb.getBoundingClientRect().left ? -1 : 1;
    region.scrollLeft += direction * pageStep(region);
  });
  thumb.addEventListener("keydown", (event) => {
    const max = region.scrollWidth - region.clientWidth;
    const now = region.scrollLeft;
    const next = {
      ArrowLeft: now - KEY_STEP,
      ArrowRight: now + KEY_STEP,
      PageUp: now - pageStep(region),
      PageDown: now + pageStep(region),
      Home: 0,
      End: max,
    }[event.key];
    if (next === undefined) return;
    event.preventDefault();
    region.scrollLeft = next;
  });
  // Horizontal wheel and trackpad gestures over the bar scroll its table; vertical ones still
  // scroll the page.
  bar.addEventListener("wheel", (event) => {
    if (Math.abs(event.deltaX) <= Math.abs(event.deltaY)) return;
    const before = region.scrollLeft;
    region.scrollLeft += event.deltaX;
    if (region.scrollLeft !== before) event.preventDefault();
  }, { passive: false });
  region.addEventListener("scroll", () => updateScrollbar(region), { passive: true });
}

function updateScrollbar(region) {
  const state = scrollbars.get(region);
  const max = region.scrollWidth - region.clientWidth;
  const overflowing = max > 1;
  state.bar.hidden = !overflowing;
  state.frame.toggleAttribute("data-scrollbar", overflowing);
  if (!overflowing) return;
  const trackWidth = state.track.clientWidth;
  const width = Math.max(24, Math.round(trackWidth * region.clientWidth / region.scrollWidth));
  const position = Math.min(1, Math.max(0, region.scrollLeft / max));
  state.thumb.style.width = `${width}px`;
  state.thumb.style.transform = `translateX(${Math.round((trackWidth - width) * position)}px)`;
  state.thumb.setAttribute("aria-valuenow", String(Math.round(position * 100)));
  state.left.setAttribute("aria-disabled", String(region.scrollLeft <= 0));
  state.right.setAttribute("aria-disabled", String(region.scrollLeft >= max - 1));
}

scrollRegions.forEach((region) => {
  createScrollbar(region);
  syncScrollRegion(region);
  updateScrollbar(region);
});

// Browsers leave a partly visible focused control where it is, so bring the whole control into
// view, after any sticky leading columns (scroll-padding-left) and above a pinned scrollbar.
scrollRegions.forEach((region) => {
  region.addEventListener("focusin", (event) => {
    if (event.target === region) return;
    const box = region.getBoundingClientRect();
    const target = event.target.getBoundingClientRect();
    const start = box.left + (parseFloat(region.style.scrollPaddingLeft) || 0);
    if (target.right > box.right) {
      region.scrollLeft += Math.min(target.right - box.right, target.left - start);
    } else if (target.left < start) {
      region.scrollLeft -= start - target.left;
    }
    const { bar } = scrollbars.get(region);
    if (!bar.hidden) {
      const barTop = bar.getBoundingClientRect().top;
      if (target.bottom > barTop) window.scrollBy(0, target.bottom - barTop + 4);
    }
  });
});
// Regions, tables, and tracks change size on resize, responsive layout, and opened disclosures.
if ("ResizeObserver" in window) {
  const observer = new ResizeObserver((entries) => {
    new Set(entries.map((entry) => entry.target.closest(".table-frame").querySelector(":scope > .table-scroll")))
      .forEach((region) => {
        syncScrollRegion(region);
        updateScrollbar(region);
      });
  });
  scrollRegions.forEach((region) => {
    observer.observe(region);
    const table = region.querySelector("table");
    if (table) observer.observe(table);
    observer.observe(scrollbars.get(region).track);
  });
}

// Complete report values get a Copy button where the clipboard is available. It copies the
// element's text, which is the value exactly as rendered; nothing is sent anywhere.
document.querySelectorAll("[data-raw-full]").forEach((source) => {
  if (!navigator.clipboard) return;
  const button = document.createElement("button");
  button.type = "button";
  button.className = "button button-ghost button-copy";
  button.setAttribute("aria-live", "polite");
  const setText = (text) => {
    button.textContent = text;
    const context = document.createElement("span");
    context.className = "visually-hidden";
    context.textContent = ` ${source.dataset.copyLabel || "value"}`;
    button.append(context);
  };
  setText("Copy");
  button.addEventListener("click", async () => {
    try {
      await navigator.clipboard.writeText(source.textContent);
      setText("Copied");
    } catch (error) {
      setText("Copy failed");
    }
    window.setTimeout(() => setText("Copy"), 2000);
  });
  source.after(button);
});

// Report tables keep Source and Key in view while scrolling, but only on wide screens and only
// while the two columns take at most half the region. Key's offset is Source's measured width,
// so they never overlap, and scroll-padding keeps focused cells from scrolling under them.
const STICKY_MEDIA = window.matchMedia("(min-width: 1024px)");
const STICKY_MAX_SHARE = 0.5;

function syncStickyColumns(table) {
  const region = table.closest(".table-scroll");
  const source = table.querySelector("th.col-source");
  const key = table.querySelector("th.col-key");
  if (!region || !source || !key) return;
  const sourceWidth = source.getBoundingClientRect().width;
  const stickyWidth = sourceWidth + key.getBoundingClientRect().width;
  const sticky = STICKY_MEDIA.matches && stickyWidth <= region.clientWidth * STICKY_MAX_SHARE;
  table.toggleAttribute("data-sticky", sticky);
  table.style.setProperty("--sticky-key-left", `${sourceWidth}px`);
  region.style.scrollPaddingLeft = sticky ? `${stickyWidth}px` : "";
}

const reportTables = document.querySelectorAll("table[data-report-table]");
reportTables.forEach(syncStickyColumns);
STICKY_MEDIA.addEventListener("change", () => reportTables.forEach(syncStickyColumns));
if ("ResizeObserver" in window) {
  const observer = new ResizeObserver(() => reportTables.forEach(syncStickyColumns));
  reportTables.forEach((table) => {
    observer.observe(table);
    observer.observe(table.closest(".table-scroll") || table);
  });
}

// Filter selects apply immediately; the Apply button remains for keyboard and no-JS use.
document.querySelectorAll("form[data-auto-submit] select").forEach((select) => {
  select.addEventListener("change", () => select.form.requestSubmit());
});
