"""The persistent table scrollbar, exercised with real input in a real browser.

Chrome runs headless with ``--hide-scrollbars``, so no native scrollbar is ever drawn: what the
tests see is only the application's own track and thumb. Mouse, wheel, touch, and keyboard
input go through the Chrome DevTools Protocol's Input domain, which delivers trusted events
through the browser's normal input pipeline (pointer capture, focus, touch panning). The
protocol client below is a minimal standard-library WebSocket, so no test dependency is added.
"""

from __future__ import annotations

import base64
import json
import os
import re
import socket
import struct
import subprocess
import threading
import time
import urllib.parse
import urllib.request
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import Any

import pytest
import uvicorn

from test_web_layout import (  # noqa: F401  (evidence_root is a fixture)
    HOSTILE_RUN,
    PAGES,
    REPORT_PAGES,
    SHORT_RUN,
    WIDTHS,
    evidence_root,
    find_browser,
    free_port,
    make_app,
)

VIEWPORT_HEIGHT = 800
DRAG = 80


class DevTools:
    """Just enough of a WebSocket client to speak the Chrome DevTools Protocol."""

    def __init__(self, url: str) -> None:
        parsed = urllib.parse.urlparse(url)
        self.sock = socket.create_connection((parsed.hostname, parsed.port), timeout=60)
        key = base64.b64encode(os.urandom(16)).decode()
        self.sock.sendall(
            f"GET {parsed.path} HTTP/1.1\r\nHost: {parsed.hostname}:{parsed.port}\r\n"
            f"Upgrade: websocket\r\nConnection: Upgrade\r\nSec-WebSocket-Key: {key}\r\n"
            "Sec-WebSocket-Version: 13\r\n\r\n".encode()
        )
        data = b""
        while b"\r\n\r\n" not in data:
            data += self.sock.recv(4096)
        head, self.buffer = data.split(b"\r\n\r\n", 1)
        assert b" 101 " in head.split(b"\r\n", 1)[0], head
        self.next_id = 0

    def _read(self, size: int) -> bytes:
        while len(self.buffer) < size:
            chunk = self.sock.recv(65536)
            if not chunk:
                raise ConnectionError("DevTools connection closed")
            self.buffer += chunk
        data, self.buffer = self.buffer[:size], self.buffer[size:]
        return data

    def _write(self, opcode: int, payload: bytes) -> None:
        header = bytearray([0x80 | opcode])
        if len(payload) < 126:
            header.append(0x80 | len(payload))
        elif len(payload) < 65536:
            header += bytes([0x80 | 126]) + struct.pack(">H", len(payload))
        else:
            header += bytes([0x80 | 127]) + struct.pack(">Q", len(payload))
        mask = os.urandom(4)
        masked = bytes(byte ^ mask[i % 4] for i, byte in enumerate(payload))
        self.sock.sendall(bytes(header) + mask + masked)

    def _message(self) -> str:
        parts = []
        while True:
            first, second = self._read(2)
            size = second & 0x7F
            if size == 126:
                size = struct.unpack(">H", self._read(2))[0]
            elif size == 127:
                size = struct.unpack(">Q", self._read(8))[0]
            payload = self._read(size)
            opcode = first & 0x0F
            if opcode == 9:
                self._write(10, payload)
                continue
            if opcode == 8:
                raise ConnectionError("DevTools connection closed")
            parts.append(payload)
            if first & 0x80:
                return b"".join(parts).decode("utf-8")

    def send(self, method: str, **params: Any) -> dict[str, Any]:
        self.next_id += 1
        self._write(1, json.dumps({"id": self.next_id, "method": method, "params": params}).encode())
        while True:
            message = json.loads(self._message())
            if message.get("id") == self.next_id:
                if "error" in message:
                    raise RuntimeError(f"{method}: {message['error']}")
                return message["result"]

    def close(self) -> None:
        self.sock.close()


# Page-side probes. They only read state or set up a position; every interaction under test is
# real input sent through the Input domain.
PROBE = r"""
window.__sb = (() => {
  const doc = document;
  const root = doc.documentElement;
  const regions = () => [...doc.querySelectorAll(".table-scroll")];
  const frameOf = (r) => r.closest(".table-frame");
  const barOf = (r) => frameOf(r).querySelector(":scope > [data-table-scrollbar]");
  const box = (el) => { const b = el.getBoundingClientRect(); return {left: b.left, right: b.right, top: b.top, bottom: b.bottom, width: b.width, height: b.height}; };
  const intersects = (a, b) => a.left < b.right - 0.5 && b.left < a.right - 0.5 && a.top < b.bottom - 0.5 && b.top < a.bottom - 0.5;
  const frames = () => new Promise((resolve) => requestAnimationFrame(() => requestAnimationFrame(resolve)));
  const topbarBottom = () => { const t = doc.querySelector(".topbar"); return t ? t.getBoundingClientRect().bottom : 0; };

  function inventory() {
    return regions().map((r) => ({
      label: r.getAttribute("aria-label"),
      overflowing: r.scrollWidth > r.clientWidth + 1,
      bars: frameOf(r).querySelectorAll("[data-table-scrollbar]").length,
      frames: r.parentElement.matches(".table-frame") ? 1 : 0,
      shown: !barOf(r).hidden,
      max: r.scrollWidth - r.clientWidth,
      height: Math.round(r.offsetHeight),
      nativeScrollbar: getComputedStyle(r).scrollbarWidth,
    }));
  }

  function place(i, where) {
    const r = regions()[i];
    const vh = root.clientHeight;
    const top = r.getBoundingClientRect().top + scrollY;
    const height = r.offsetHeight;
    const y = {top: top - topbarBottom() - 8, middle: top + height / 2 - vh / 2, bottom: top + height - vh + 80}[where];
    scrollTo(0, Math.max(0, Math.min(y, root.scrollHeight - vh)));
    return frames();
  }

  function state(i) {
    const r = regions()[i];
    const bar = barOf(r);
    const track = bar.querySelector(".table-scrollbar-track");
    const thumb = bar.querySelector(".table-scrollbar-thumb");
    const vh = root.clientHeight;
    const b = box(bar), t = box(thumb), tr = box(track), rr = box(r), fr = box(frameOf(r));
    const max = r.scrollWidth - r.clientWidth;
    const travel = track.clientWidth - thumb.offsetWidth;
    const thumbOffset = t.left - (tr.left + track.clientLeft);
    const hit = doc.elementFromPoint(t.left + t.width / 2, t.top + t.height / 2);
    const thumbStyle = getComputedStyle(thumb);
    const others = regions().filter((o) => o !== r).map(barOf).filter((o) => !o.hidden).map(box);
    const visibleTop = Math.max(rr.top, topbarBottom());
    const visibleBottom = Math.min(rr.bottom, b.top, vh);
    return {
      shown: !bar.hidden,
      scrollLeft: r.scrollLeft,
      max,
      bar: b, thumb: t, track: tr, region: rr, frame: fr, vh, topbar: topbarBottom(),
      inViewport: b.height > 0 && b.top >= topbarBottom() - 0.5 && b.bottom <= vh + 0.5,
      pinned: Math.abs(b.bottom - vh) <= 1,
      thumbOnTop: hit === thumb,
      thumbDisplay: thumbStyle.display,
      thumbVisibility: thumbStyle.visibility,
      opacity: [thumbStyle.opacity, getComputedStyle(track).opacity, getComputedStyle(bar).opacity],
      thumbColor: thumbStyle.backgroundColor,
      trackColor: getComputedStyle(track).backgroundColor,
      aligned: Math.abs(b.left - rr.left) <= 1 && Math.abs(b.right - rr.right) <= 1,
      withinFrame: b.top >= fr.top - 0.5 && b.bottom <= fr.bottom + 0.5,
      overlapsOtherBar: others.some((o) => intersects(o, b)),
      thumbInTrack: t.left >= tr.left - 0.5 && t.right <= tr.right + 0.5,
      expectedThumbWidth: Math.max(24, Math.round(track.clientWidth * r.clientWidth / r.scrollWidth)),
      syncError: max > 0 ? Math.abs(thumbOffset - travel * r.scrollLeft / max) : 0,
      travel,
      valueNow: thumb.getAttribute("aria-valuenow"),
      pageOverflow: root.scrollWidth - root.clientWidth,
      // A point on the table's visible rows, above the bar, for wheel and touch input.
      tablePoint: {x: (rr.left + rr.right) / 2, y: (visibleTop + visibleBottom) / 2},
      tableVisible: visibleBottom - visibleTop,
    };
  }

  function controls(i) {
    const r = regions()[i];
    const bar = barOf(r);
    const [left, right] = bar.querySelectorAll("button");
    const thumb = bar.querySelector("[role=scrollbar]");
    return {
      regionId: r.id,
      left: {label: left.getAttribute("aria-label"), controls: left.getAttribute("aria-controls"), disabled: left.getAttribute("aria-disabled")},
      right: {label: right.getAttribute("aria-label"), controls: right.getAttribute("aria-controls"), disabled: right.getAttribute("aria-disabled")},
      thumb: {label: thumb.getAttribute("aria-label"), controls: thumb.getAttribute("aria-controls"),
              orientation: thumb.getAttribute("aria-orientation"), tabIndex: thumb.tabIndex,
              min: thumb.getAttribute("aria-valuemin"), max: thumb.getAttribute("aria-valuemax"), now: thumb.getAttribute("aria-valuenow")},
    };
  }

  function focusBefore(i) {
    const r = regions()[i];
    const inside = [...r.querySelectorAll("a[href], button, summary, input, select, [tabindex]:not([tabindex='-1'])")]
      .filter((el) => el.checkVisibility());
    (inside[inside.length - 1] || r).focus();
  }

  function active() {
    const el = doc.activeElement;
    const style = getComputedStyle(el);
    return {
      label: el.getAttribute("aria-label"),
      role: el.getAttribute("role") || el.tagName.toLowerCase(),
      focusVisible: el.matches(":focus-visible"),
      outline: `${style.outlineStyle} ${style.outlineWidth}`,
      inViewport: (() => { const b = el.getBoundingClientRect(); return b.top >= 0 && b.bottom <= root.clientHeight; })(),
    };
  }

  function lastColumnInView(i) {
    const r = regions()[i];
    const right = r.getBoundingClientRect().right;
    return [...r.querySelector("table").rows].every((row) => row.cells[row.cells.length - 1].getBoundingClientRect().right <= right + 1);
  }

  function stickyOk(i) {
    const r = regions()[i];
    const table = r.querySelector("table[data-report-table][data-sticky]");
    if (!table) return null;
    const left = r.getBoundingClientRect().left;
    return [...table.rows].every((row) => {
      const source = row.querySelector(".col-source").getBoundingClientRect();
      const key = row.querySelector(".col-key").getBoundingClientRect();
      return Math.abs(source.left - left) <= 1 && key.left >= source.right - 1;
    });
  }

  // A focusable control in a row hidden behind the pinned bar, focused programmatically.
  function revealBehindBar(i) {
    const r = regions()[i];
    const bar = barOf(r);
    const barTop = bar.getBoundingClientRect().top;
    const hidden = [...r.querySelectorAll("a[href], summary")].find((el) => {
      const b = el.getBoundingClientRect();
      return b.bottom > barTop + 1 && b.top < root.clientHeight;
    });
    if (!hidden) return null;
    hidden.focus();
    const revealed = hidden.getBoundingClientRect().bottom <= bar.getBoundingClientRect().top + 1;
    hidden.blur();
    return revealed;
  }

  return {regions, inventory, place, state, controls, focusBefore, active, lastColumnInView, stickyOk,
          revealBehindBar, frames,
          setScroll: (i, x) => { regions()[i].scrollLeft = x; return frames(); },
          setAll: (x) => { regions().forEach((r) => { r.scrollLeft = x; }); return frames(); },
          scrollLefts: () => regions().map((r) => r.scrollLeft),
          openDisclosures: (i, open) => { regions()[i].querySelectorAll("details").forEach((d) => { d.open = open; }); return frames().then(frames); },
          disclosureCount: (i) => regions()[i].querySelectorAll("details").length,
          setTheme: (theme) => { root.dataset.theme = theme; return frames(); },
          focusThumb: (i) => { barOf(regions()[i]).querySelector("[role=scrollbar]").focus(); }};
})();
"""


class Page:
    def __init__(self, tools: DevTools) -> None:
        self.tools = tools

    def js(self, expression: str) -> Any:
        result = self.tools.send("Runtime.evaluate", expression=expression, returnByValue=True,
                                 awaitPromise=True)
        if "exceptionDetails" in result:
            raise RuntimeError(f"{expression}: {result['exceptionDetails']}")
        return result["result"].get("value")

    def viewport(self, width: int, height: int = VIEWPORT_HEIGHT) -> None:
        self.tools.send("Emulation.setDeviceMetricsOverride", width=width, height=height,
                        deviceScaleFactor=1, mobile=False)

    def open(self, url: str) -> None:
        self.tools.send("Page.navigate", url=url)
        deadline = time.monotonic() + 20
        while time.monotonic() < deadline:
            time.sleep(0.05)
            try:
                if self.js("document.readyState === 'complete' && !!document.querySelector('main')"):
                    break
            except RuntimeError:
                continue
        self.js(PROBE)
        self.js("__sb.frames().then(__sb.frames)")

    def state(self, index: int) -> dict[str, Any]:
        return self.js(f"__sb.state({index})")

    def mouse(self, kind: str, x: float, y: float, **extra: Any) -> None:
        self.tools.send("Input.dispatchMouseEvent", type=kind, x=x, y=y, **extra)

    def drag(self, x: float, y: float, dx: float) -> None:
        self.mouse("mouseMoved", x, y)
        self.mouse("mousePressed", x, y, button="left", buttons=1, clickCount=1)
        for step in range(1, 5):
            self.mouse("mouseMoved", x + dx * step / 4, y, button="left", buttons=1)
        self.mouse("mouseReleased", x + dx, y, button="left", buttons=0, clickCount=1)
        self.js("__sb.frames()")

    def click(self, x: float, y: float) -> None:
        self.mouse("mouseMoved", x, y)
        self.mouse("mousePressed", x, y, button="left", buttons=1, clickCount=1)
        self.mouse("mouseReleased", x, y, button="left", buttons=0, clickCount=1)
        self.js("__sb.frames()")

    def key(self, key: str, code: str, keycode: int, text: str | None = None) -> None:
        down: dict[str, Any] = {"key": key, "code": code, "windowsVirtualKeyCode": keycode}
        if text:
            down["text"] = text
        self.tools.send("Input.dispatchKeyEvent", type="keyDown", **down)
        self.tools.send("Input.dispatchKeyEvent", type="keyUp", key=key, code=code,
                        windowsVirtualKeyCode=keycode)
        self.js("__sb.frames()")

    def touch_drag(self, x: float, y: float, dx: float) -> None:
        send = self.tools.send
        send("Input.dispatchTouchEvent", type="touchStart", touchPoints=[{"x": x, "y": y}])
        for step in range(1, 7):
            send("Input.dispatchTouchEvent", type="touchMove",
                 touchPoints=[{"x": x + dx * step / 6, "y": y}])
        send("Input.dispatchTouchEvent", type="touchEnd", touchPoints=[])

    def settle(self, index: int) -> float:
        """Wait for an animated scroll (smooth wheel, touch fling) to come to rest."""
        last = None
        for _ in range(40):
            now = self.js(f"__sb.frames().then(() => __sb.regions()[{index}].scrollLeft)")
            if now == last:
                return now
            last = now
        return last


TAB = ("Tab", "Tab", 9)
ARROW_RIGHT = ("ArrowRight", "ArrowRight", 39)
END = ("End", "End", 35)
HOME = ("Home", "Home", 36)


def interact(page: Page, index: int, report: bool) -> dict[str, Any]:
    """Real pointer, wheel, touch, and keyboard use of one table's scrollbar."""
    result: dict[str, Any] = {}
    page.js(f"__sb.place({index}, 'middle')")
    page.js("__sb.setAll(0)")
    before = page.state(index)
    result["controls"] = page.js(f"__sb.controls({index})")
    result["startDisabledLeft"] = result["controls"]["left"]["disabled"]

    # Drag the thumb with the mouse.
    thumb = before["thumb"]
    x, y = thumb["left"] + thumb["width"] / 2, thumb["top"] + thumb["height"] / 2
    dx = min(DRAG, before["travel"] / 2)
    page.drag(x, y, dx)
    after = page.state(index)
    result["drag"] = {
        "dx": dx,
        "scrollLeft": after["scrollLeft"],
        "expected": dx * before["max"] / before["travel"],
        "thumbMoved": after["thumb"]["left"] - thumb["left"],
        "syncError": after["syncError"],
    }
    # The pointer leaves and the page idles: the bar stays drawn.
    page.mouse("mouseMoved", 2, 2)
    time.sleep(1.0)
    result["idle"] = page.state(index)

    # Press the track on either side of the thumb.
    track, thumb = after["track"], after["thumb"]
    clicks = {}
    if track["right"] - thumb["right"] > 8:
        start = page.state(index)["scrollLeft"]
        page.click((thumb["right"] + track["right"]) / 2, (track["top"] + track["bottom"]) / 2)
        clicks["right"] = page.state(index)["scrollLeft"] - start
    now = page.state(index)
    if now["thumb"]["left"] - now["track"]["left"] > 8:
        start = now["scrollLeft"]
        page.click((now["track"]["left"] + now["thumb"]["left"]) / 2,
                   (now["track"]["top"] + now["track"]["bottom"]) / 2)
        clicks["left"] = page.state(index)["scrollLeft"] - start
    result["trackClicks"] = clicks
    result["trackRoom"] = before["track"]["width"] - before["thumb"]["width"]
    result["afterClicks"] = page.state(index)

    # Horizontal wheel or trackpad over the table rows moves the thumb.
    page.js("__sb.setAll(0)")
    point = page.state(index)["tablePoint"]
    page.mouse("mouseWheel", point["x"], point["y"], deltaX=120, deltaY=0)
    page.settle(index)
    wheel = page.state(index)
    result["wheel"] = {"scrollLeft": wheel["scrollLeft"], "syncError": wheel["syncError"],
                       "valueNow": wheel["valueNow"], "pageScrollY": page.js("scrollY")}

    # Keyboard: Tab out of the table reaches the bar's controls in order, with a focus ring.
    page.js("__sb.setAll(0)")
    page.js(f"__sb.focusBefore({index})")
    page.key(*TAB)
    first = page.js("__sb.active()")
    page.js("__sb.setAll(0)")
    page.key(*TAB)
    second = page.js("__sb.active()")
    page.key(*ARROW_RIGHT)
    arrow = page.state(index)["scrollLeft"]
    page.key(*END)
    end = page.state(index)
    end_column = page.js(f"__sb.lastColumnInView({index})")
    sticky = page.js(f"__sb.stickyOk({index})")
    end_controls = page.js(f"__sb.controls({index})")
    page.key(*HOME)
    home = page.state(index)["scrollLeft"]
    page.key(*TAB)
    third = page.js("__sb.active()")
    page.key("Enter", "Enter", 13, "\r")
    enter = page.state(index)["scrollLeft"]
    result["keyboard"] = {
        "order": [first, second, third],
        "arrow": arrow, "end": end["scrollLeft"], "max": end["max"], "endValueNow": end["valueNow"],
        "endSyncError": end["syncError"], "endRightDisabled": end_controls["right"]["disabled"],
        "lastColumnInView": end_column, "stickyOk": sticky, "home": home, "enter": enter,
    }
    page.js("document.activeElement.blur()")
    result["afterFocus"] = page.state(index)

    # Touch: dragging the thumb, and panning the table itself, both stay in sync.
    page.tools.send("Emulation.setTouchEmulationEnabled", enabled=True, maxTouchPoints=1)
    page.js("__sb.setAll(0)")
    now = page.state(index)
    thumb = now["thumb"]
    page.touch_drag(thumb["left"] + thumb["width"] / 2, thumb["top"] + thumb["height"] / 2,
                    min(DRAG, now["travel"] / 2))
    page.settle(index)
    touch_thumb = page.state(index)
    page.js("__sb.setAll(0)")
    point = page.state(index)["tablePoint"]
    page.touch_drag(point["x"] + 60, point["y"], -120)
    page.settle(index)
    touch_pan = page.state(index)
    page.tools.send("Emulation.setTouchEmulationEnabled", enabled=False)
    result["touch"] = {
        "thumbScrollLeft": touch_thumb["scrollLeft"], "thumbSyncError": touch_thumb["syncError"],
        "panScrollLeft": touch_pan["scrollLeft"], "panSyncError": touch_pan["syncError"],
    }

    # A row hidden behind the pinned bar is revealed when one of its controls takes focus.
    page.js("__sb.setAll(0)")
    page.js(f"__sb.place({index}, 'middle')")
    result["revealed"] = page.js(f"__sb.revealBehindBar({index})")

    # Opening every source-line disclosure widens the table; the thumb follows.
    if report:
        page.js(f"__sb.place({index}, 'middle')")
        page.js("__sb.setAll(0)")
        page.js(f"__sb.openDisclosures({index}, true)")
        opened = page.state(index)
        page.js(f"__sb.focusThumb({index})")
        page.key(*END)
        opened_end = page.state(index)
        result["disclosures"] = {
            "count": page.js(f"__sb.disclosureCount({index})"),
            "maxBefore": before["max"], "maxAfter": opened["max"],
            "thumbWidth": opened["thumb"]["width"], "expectedThumbWidth": opened["expectedThumbWidth"],
            "endReached": abs(opened_end["scrollLeft"] - opened_end["max"]) <= 1,
            "syncError": opened_end["syncError"],
            "lastColumnInView": page.js(f"__sb.lastColumnInView({index})"),
            "stickyOk": page.js(f"__sb.stickyOk({index})"),
            "pageOverflow": opened_end["pageOverflow"],
        }
        page.js("document.activeElement.blur()")
        page.js(f"__sb.openDisclosures({index}, false)")
    page.js("__sb.setAll(0)")
    return result


def independence(page: Page, overflowing: list[int]) -> dict[str, Any]:
    """Dragging one table's thumb moves that table only."""
    results = []
    for index in overflowing:
        page.js(f"__sb.place({index}, 'middle')")
        page.js("__sb.setAll(0)")
        now = page.state(index)
        thumb = now["thumb"]
        page.drag(thumb["left"] + thumb["width"] / 2, thumb["top"] + thumb["height"] / 2,
                  min(DRAG / 2, now["travel"] / 2))
        lefts = page.js("__sb.scrollLefts()")
        states = [page.state(i) for i in overflowing]
        results.append({
            "moved": lefts[index] > 0,
            "othersStill": all(lefts[i] == 0 for i in overflowing if i != index),
            "otherThumbsAtStart": all(s["valueNow"] == "0" for i, s in zip(overflowing, states) if i != index),
            "overlap": any(s["overlapsOtherBar"] for s in states if s["shown"]),
        })
    page.js("__sb.setAll(0)")
    return {"tables": len(overflowing), "drags": results}


def survey(page: Page, base: str, path: str, width: int) -> dict[str, Any]:
    page.viewport(width)
    page.open(base + path)
    inventory = page.js("__sb.inventory()")
    overflowing = [i for i, region in enumerate(inventory) if region["overflowing"]]
    positions = {}
    themes = {}
    for index in overflowing:
        for where in ("top", "middle", "bottom"):
            page.js(f"__sb.place({index}, '{where}')")
            positions[f"{index}:{where}"] = page.state(index)
    if overflowing:
        index = overflowing[0]
        page.js(f"__sb.place({index}, 'middle')")
        for theme in ("light", "dark"):
            page.js(f"__sb.setTheme('{theme}')")
            themes[theme] = page.state(index)
    result: dict[str, Any] = {
        "inventory": inventory,
        "positions": positions,
        "themes": themes,
        "interaction": interact(page, overflowing[0], path in REPORT_PAGES) if overflowing else None,
        "independence": independence(page, overflowing) if len(overflowing) >= 2 else None,
        "pageOverflow": page.js("document.documentElement.scrollWidth - document.documentElement.clientWidth"),
    }
    if width == 1280:
        resized = []
        for size in (390, 1920, 1280):
            page.viewport(size)
            page.js("__sb.frames().then(__sb.frames)")
            resized.append({"width": size, "inventory": page.js("__sb.inventory()"),
                            "pageOverflow": page.js("document.documentElement.scrollWidth - document.documentElement.clientWidth")})
        result["resize"] = resized
    return result


@contextmanager
def browser_session(evidence_root: Path, profile: Path, flags: list[str]) -> Iterator[tuple[Page, str]]:
    """The app on a local server and a headless browser driven over the DevTools Protocol."""
    browser = find_browser()
    if browser is None:
        pytest.skip("no Chrome or Edge installation found for browser tests")
    port = free_port()
    server = uvicorn.Server(uvicorn.Config(make_app(evidence_root), host="127.0.0.1", port=port,
                                           log_level="warning"))
    thread = threading.Thread(target=server.run, daemon=True)
    thread.start()
    process = subprocess.Popen(
        [str(browser), "--headless=new", "--disable-gpu", "--no-first-run",
         "--no-default-browser-check", "--disable-extensions", *flags,
         f"--user-data-dir={profile}", "--remote-debugging-port=0", "--window-size=1920,900",
         "about:blank"],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    tools = None
    try:
        deadline = time.monotonic() + 30
        while not server.started or not (profile / "DevToolsActivePort").is_file():
            if time.monotonic() > deadline or process.poll() is not None:
                pytest.fail("browser or test server did not start")
            time.sleep(0.05)
        time.sleep(0.2)
        debug_port = (profile / "DevToolsActivePort").read_text().split()[0]
        with urllib.request.urlopen(f"http://127.0.0.1:{debug_port}/json/list") as response:
            targets = json.load(response)
        target = next(t for t in targets if t["type"] == "page")
        tools = DevTools(target["webSocketDebuggerUrl"])
        yield Page(tools), f"http://127.0.0.1:{port}"
    finally:
        if tools:
            tools.close()
        process.kill()
        process.wait(timeout=10)
        server.should_exit = True
        thread.join(timeout=10)


@pytest.fixture(scope="module")
def scrollbars(evidence_root: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    profile = tmp_path_factory.mktemp("scrollbar-profile")
    with browser_session(evidence_root, profile, ["--hide-scrollbars"]) as (page, base):
        return {f"{path} @ {width}": survey(page, base, path, width)
                for path in PAGES for width in WIDTHS}


# Windows 11 draws overlay scrollbars: they take no space and fade out once scrolling stops.
# A control built on a native scrollbar then shows an empty track, which is how UI-006 failed
# acceptance. Chrome's OverlayScrollbar feature reproduces that rendering.
IDLE_SECONDS = 3.2
IDLE_CASES = [
    (f"/conversions/{SHORT_RUN}/reports/exceptions", 1707, 869, "dark"),
    (f"/conversions/{SHORT_RUN}/reports/exceptions", 1707, 869, "light"),
    (f"/conversions/{SHORT_RUN}/reports/exceptions", 390, 800, "dark"),
    (f"/conversions/{HOSTILE_RUN}/reports/exceptions", 1280, 800, "dark"),
    ("/applications?page_size=100", 768, 800, "dark"),
]


def idle_after_vertical_scroll(page: Page, base: str, path: str, width: int, height: int,
                               theme: str) -> dict[str, Any]:
    page.viewport(width, height)
    page.open(base + path)
    page.js(f"__sb.setTheme('{theme}')")
    overlay = page.js("""(() => { const d = document.createElement('div');
      d.style.cssText = 'overflow:scroll;width:100px;height:100px;position:absolute';
      document.body.append(d); const width = d.offsetWidth - d.clientWidth; d.remove(); return width; })()""")
    page.js("scrollTo(0, 0)")
    region = page.js("(() => { const b = __sb.regions()[0].getBoundingClientRect(); return {x: b.left + b.width / 2, top: b.top, height: b.height}; })()")
    y = min(region["top"] + 120, height / 2)
    page.mouse("mouseMoved", region["x"], y)
    # Wheel down to roughly the middle of the table.
    for _ in range(max(1, round((region["top"] + region["height"] / 2 - height / 2) / 120))):
        page.mouse("mouseWheel", region["x"], y, deltaX=0, deltaY=120)
    last = None
    for _ in range(40):
        now = page.js("__sb.frames().then(() => scrollY)")
        if now == last and now > 0:
            break
        last = now
    samples = []
    started = time.monotonic()
    while time.monotonic() - started < IDLE_SECONDS:
        time.sleep(0.25)
        samples.append(page.state(0))
    # The thumb must put pixels on screen: hiding it alone has to change the captured image.
    thumb = samples[-1]["thumb"]
    scroll = page.js("({x: scrollX, y: scrollY})")
    clip = {"x": thumb["left"] + scroll["x"], "y": thumb["top"] + scroll["y"],
            "width": thumb["width"], "height": thumb["height"], "scale": 1}
    shown = page.tools.send("Page.captureScreenshot", format="png", clip=clip)["data"]
    page.js("document.querySelector('.table-scrollbar-thumb').style.visibility = 'hidden'")
    page.js("__sb.frames()")
    hidden = page.tools.send("Page.captureScreenshot", format="png", clip=clip)["data"]
    page.js("document.querySelector('.table-scrollbar-thumb').style.visibility = ''")
    return {"overlayScrollbarWidth": overlay, "scrollY": last, "samples": samples,
            "thumbPainted": shown != hidden}


@pytest.fixture(scope="module")
def overlay_idle(evidence_root: Path, tmp_path_factory: pytest.TempPathFactory) -> dict[str, Any]:
    profile = tmp_path_factory.mktemp("overlay-profile")
    with browser_session(evidence_root, profile, ["--enable-features=OverlayScrollbar"]) as (page, base):
        return {case: idle_after_vertical_scroll(page, base, *case) for case in IDLE_CASES}


CASES = [(page, width) for page in PAGES for width in WIDTHS]


def luminance(color: str) -> float:
    channels = [int(c) / 255 for c in re.findall(r"\d+", color)[:3]]
    linear = [c / 12.92 if c <= 0.03928 else ((c + 0.055) / 1.055) ** 2.4 for c in channels]
    return 0.2126 * linear[0] + 0.7152 * linear[1] + 0.0722 * linear[2]


def contrast(a: str, b: str) -> float:
    high, low = sorted((luminance(a), luminance(b)), reverse=True)
    return (high + 0.05) / (low + 0.05)


def assert_drawn(state: dict[str, Any]) -> None:
    """The track and thumb are rendered, on screen, on top, and opaque."""
    assert state["shown"] and state["inViewport"], state
    assert state["thumbOnTop"], state
    assert state["thumbDisplay"] != "none" and state["thumbVisibility"] == "visible", state
    assert state["opacity"] == ["1", "1", "1"], state
    assert state["thumb"]["width"] >= 24 and state["thumb"]["height"] >= 8, state
    assert state["track"]["height"] >= 12 and state["thumbInTrack"], state
    assert state["aligned"] and state["withinFrame"] and not state["overlapsOtherBar"], state
    assert state["syncError"] <= 1, state


def get(scrollbars: dict[str, Any], page: str, width: int) -> dict[str, Any]:
    return scrollbars[f"{page} @ {width}"]


@pytest.mark.parametrize(("page", "width"), CASES)
def test_a_bar_exists_exactly_for_overflowing_tables(
    scrollbars: dict[str, Any], page: str, width: int
) -> None:
    result = get(scrollbars, page, width)
    for region in result["inventory"]:
        assert region["frames"] == 1 and region["bars"] == 1, region
        assert region["shown"] == region["overflowing"], region
        if region["overflowing"]:
            assert region["nativeScrollbar"] == "none", region
    assert result["pageOverflow"] <= 0


@pytest.mark.parametrize(("page", "width"), CASES)
def test_track_and_thumb_are_drawn_at_top_middle_and_bottom_of_each_table(
    scrollbars: dict[str, Any], page: str, width: int
) -> None:
    for key, state in get(scrollbars, page, width)["positions"].items():
        assert_drawn(state)
        assert state["bar"]["top"] >= state["topbar"] - 0.5, key
        assert state["pageOverflow"] <= 0, key
        # A table running past the viewport has its bar pinned to the viewport bottom.
        if state["region"]["bottom"] > state["vh"]:
            assert state["pinned"], (key, state)


@pytest.mark.parametrize(("page", "width"), CASES)
def test_bar_stays_drawn_when_idle_after_pointer_and_focus_changes(
    scrollbars: dict[str, Any], page: str, width: int
) -> None:
    interaction = get(scrollbars, page, width)["interaction"]
    if interaction is None:
        return
    assert_drawn(interaction["idle"])
    assert_drawn(interaction["afterClicks"])
    assert_drawn(interaction["afterFocus"])


@pytest.mark.parametrize(("page", "width"), CASES)
def test_bar_is_visible_in_both_themes(scrollbars: dict[str, Any], page: str, width: int) -> None:
    themes = get(scrollbars, page, width)["themes"]
    for theme, state in themes.items():
        assert_drawn(state)
        assert contrast(state["thumbColor"], state["trackColor"]) >= 3, (theme, state)
    if themes:
        assert themes["light"]["thumbColor"] != themes["dark"]["thumbColor"]


@pytest.mark.parametrize(("page", "width"), CASES)
def test_dragging_the_thumb_scrolls_its_table(scrollbars: dict[str, Any], page: str, width: int) -> None:
    interaction = get(scrollbars, page, width)["interaction"]
    if interaction is None:
        return
    drag = interaction["drag"]
    assert abs(drag["scrollLeft"] - drag["expected"]) <= 3, drag
    assert abs(drag["thumbMoved"] - drag["dx"]) <= 2, drag
    assert drag["syncError"] <= 1, drag


@pytest.mark.parametrize(("page", "width"), CASES)
def test_pressing_the_track_pages_toward_the_press(
    scrollbars: dict[str, Any], page: str, width: int
) -> None:
    interaction = get(scrollbars, page, width)["interaction"]
    if interaction is None:
        return
    clicks = interaction["trackClicks"]
    # A table overflowing by a few pixels has a thumb filling its track: nothing to press.
    assert clicks or interaction["trackRoom"] <= 16, interaction["trackRoom"]
    if "right" in clicks:
        assert clicks["right"] > 0, clicks
    if "left" in clicks:
        assert clicks["left"] < 0, clicks


@pytest.mark.parametrize(("page", "width"), CASES)
def test_wheel_and_touch_on_the_table_move_the_thumb(
    scrollbars: dict[str, Any], page: str, width: int
) -> None:
    interaction = get(scrollbars, page, width)["interaction"]
    if interaction is None:
        return
    wheel, touch = interaction["wheel"], interaction["touch"]
    assert wheel["scrollLeft"] > 0 and wheel["syncError"] <= 1 and wheel["valueNow"] != "0", wheel
    assert touch["thumbScrollLeft"] > 0 and touch["thumbSyncError"] <= 1, touch
    assert touch["panScrollLeft"] > 0 and touch["panSyncError"] <= 1, touch


@pytest.mark.parametrize(("page", "width"), CASES)
def test_keyboard_reaches_and_operates_the_bar_with_labels_and_focus_rings(
    scrollbars: dict[str, Any], page: str, width: int
) -> None:
    interaction = get(scrollbars, page, width)["interaction"]
    if interaction is None:
        return
    controls = interaction["controls"]
    region_id = controls["regionId"]
    assert controls["left"]["controls"] == controls["right"]["controls"] == region_id
    assert controls["thumb"]["controls"] == region_id
    assert controls["left"]["label"].startswith("Scroll ") and controls["left"]["label"].endswith(" left")
    assert controls["right"]["label"].endswith(" right")
    assert controls["thumb"]["label"].endswith(" columns") and controls["thumb"]["orientation"] == "horizontal"
    assert controls["thumb"]["tabIndex"] == 0
    assert interaction["startDisabledLeft"] == "true"

    keyboard = interaction["keyboard"]
    first, second, third = keyboard["order"]
    assert first["role"] == "button" and first["label"] == controls["left"]["label"], first
    assert second["role"] == "scrollbar" and second["label"] == controls["thumb"]["label"], second
    assert third["role"] == "button" and third["label"] == controls["right"]["label"], third
    for focused in keyboard["order"]:
        assert focused["focusVisible"] and focused["outline"] == "solid 2px", focused
        assert focused["inViewport"], focused
    assert abs(keyboard["arrow"] - min(40, keyboard["max"])) <= 1, keyboard
    assert abs(keyboard["end"] - keyboard["max"]) <= 1 and keyboard["endValueNow"] == "100", keyboard
    assert keyboard["endSyncError"] <= 1 and keyboard["endRightDisabled"] == "true", keyboard
    assert keyboard["lastColumnInView"], keyboard
    assert keyboard["stickyOk"] in (None, True), keyboard
    assert keyboard["home"] == 0 and keyboard["enter"] > 0, keyboard


@pytest.mark.parametrize(("page", "width"), CASES)
def test_focus_reveals_rows_behind_a_pinned_bar(
    scrollbars: dict[str, Any], page: str, width: int
) -> None:
    interaction = get(scrollbars, page, width)["interaction"]
    if interaction is None:
        return
    assert interaction["revealed"] in (None, True), interaction["revealed"]


def test_focus_reveal_is_exercised(scrollbars: dict[str, Any]) -> None:
    assert any(result["interaction"] and result["interaction"]["revealed"]
               for result in scrollbars.values())


REPORT_CASES = [(page, width) for page in REPORT_PAGES for width in WIDTHS]


@pytest.mark.parametrize(("page", "width"), REPORT_CASES)
def test_opened_source_lines_widen_the_range_and_stay_reachable(
    scrollbars: dict[str, Any], page: str, width: int
) -> None:
    disclosures = get(scrollbars, page, width)["interaction"]["disclosures"]
    assert disclosures["count"] > 0
    assert disclosures["maxAfter"] >= disclosures["maxBefore"], disclosures
    assert abs(disclosures["thumbWidth"] - disclosures["expectedThumbWidth"]) <= 1, disclosures
    assert disclosures["endReached"] and disclosures["syncError"] <= 1, disclosures
    assert disclosures["lastColumnInView"] and disclosures["pageOverflow"] <= 0, disclosures
    assert disclosures["stickyOk"] in (None, True), disclosures


@pytest.mark.parametrize(("page", "width"), CASES)
def test_tables_on_one_page_scroll_independently(
    scrollbars: dict[str, Any], page: str, width: int
) -> None:
    result = get(scrollbars, page, width)["independence"]
    if result is None:
        return
    for drag in result["drags"]:
        assert drag["moved"] and drag["othersStill"] and drag["otherThumbsAtStart"], drag
        assert not drag["overlap"], drag


def test_independence_is_exercised(scrollbars: dict[str, Any]) -> None:
    assert any(result["independence"] for result in scrollbars.values())


@pytest.mark.parametrize("page", PAGES)
def test_resizing_shows_and_hides_bars_without_duplicates(
    scrollbars: dict[str, Any], page: str
) -> None:
    for step in get(scrollbars, page, 1280)["resize"]:
        for region in step["inventory"]:
            assert region["frames"] == 1 and region["bars"] == 1, (step["width"], region)
            assert region["shown"] == region["overflowing"], (step["width"], region)
        assert step["pageOverflow"] <= 0, step


def test_long_tables_are_covered(scrollbars: dict[str, Any]) -> None:
    """The survey reaches the cases the bar exists for: long tables at narrow and wide widths."""
    pinned = {key for key, result in scrollbars.items()
              if any(state["pinned"] and state["region"]["bottom"] > state["vh"]
                     for state in result["positions"].values())}
    assert "/applications?page_size=100 @ 390" in pinned
    assert any(key.endswith("@ 1920") for key in pinned)


@pytest.mark.parametrize("case", IDLE_CASES, ids=lambda case: f"{case[0]}-{case[1]}x{case[2]}-{case[3]}")
def test_bar_stays_drawn_through_idle_after_vertical_scroll_with_overlay_scrollbars(
    overlay_idle: dict[tuple, Any], case: tuple
) -> None:
    result = overlay_idle[case]
    assert result["overlayScrollbarWidth"] == 0, "overlay scrollbars are not active"
    assert result["scrollY"] > 0, "the page did not scroll vertically"
    assert len(result["samples"]) >= 12
    for state in result["samples"]:
        assert_drawn(state)
        # Mid-table, the table's own scrollbar is below the viewport; the bar is pinned instead.
        assert state["region"]["bottom"] > state["vh"] and state["pinned"], state
    assert result["thumbPainted"], "the thumb is in the DOM but paints nothing"
