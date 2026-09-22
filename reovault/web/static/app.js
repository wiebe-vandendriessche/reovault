// ReoVault dashboard client script. Four responsibilities: the color theme
// toggle, the service worker, the offline/online banner, and video
// pause-others + codec fallback. Everything else is native HTML, <details>,
// or HTMX attributes; a fifth responsibility here needs a reason.
(function () {
  "use strict";

  // 0. Color theme: the OS default, unless a visitor has explicitly
  // overridden it (see the toggle button in base.html; login.html and
  // offline.html load this same script purely so a stored override still
  // applies there too, before any nav exists to change it). Storage, not a
  // cookie: this is a per-browser display preference, not server state,
  // and the CSP here (style-src/script-src 'self', no unsafe-inline) rules
  // out the usual inline-script trick for applying it before first paint
  // anyway -- this external, deferred script is the earliest point CSP
  // allows any theme logic to run at all. Applying `data-theme` is what
  // app.css's own `:root[data-theme="light"]` override (and its absence,
  // meaning "follow the OS") already key every themed rule off.
  var THEME_KEY = "rv-theme";

  function storedTheme() {
    try {
      return localStorage.getItem(THEME_KEY);
    } catch (e) {
      return null; // private browsing or blocked storage: always "auto"
    }
  }

  function effectiveTheme() {
    var stored = storedTheme();
    if (stored === "light" || stored === "dark") return stored;
    var prefersLight = window.matchMedia && window.matchMedia("(prefers-color-scheme: light)").matches;
    return prefersLight ? "light" : "dark";
  }

  function applyStoredThemeOverride() {
    var stored = storedTheme();
    if (stored === "light" || stored === "dark") {
      document.documentElement.setAttribute("data-theme", stored);
    } else {
      document.documentElement.removeAttribute("data-theme");
    }
  }

  function syncThemeToggle() {
    var toggle = document.getElementById("theme-toggle");
    if (!toggle) return; // login.html / offline.html have no navbar to put it in
    toggle.setAttribute(
      "aria-label",
      effectiveTheme() === "dark" ? "Switch to light theme" : "Switch to dark theme",
    );
  }

  applyStoredThemeOverride();
  syncThemeToggle();

  var themeToggle = document.getElementById("theme-toggle");
  if (themeToggle) {
    themeToggle.addEventListener("click", function () {
      var next = effectiveTheme() === "dark" ? "light" : "dark";
      try {
        localStorage.setItem(THEME_KEY, next);
      } catch (e) {
        /* the choice just won't survive a reload; it still applies now */
      }
      applyStoredThemeOverride();
      syncThemeToggle();
    });
  }

  // No stored override yet: keep the toggle's own label in sync if the OS
  // theme changes underneath it while the page is open. app.css's own
  // `@media (prefers-color-scheme)` rules already repaint everything else
  // automatically; only this button's aria-label needs JS to follow along.
  if (window.matchMedia) {
    window.matchMedia("(prefers-color-scheme: light)").addEventListener("change", function () {
      if (!storedTheme()) syncThemeToggle();
    });
  }

  // 1. Service worker registration.
  if ("serviceWorker" in navigator) {
    window.addEventListener("load", function () {
      navigator.serviceWorker.register("/sw.js").catch(function () {
        /* offline install just doesn't happen; the app still works online */
      });
    });
  }

  // 2. Online/offline banner.
  function syncOnlineState() {
    document.documentElement.classList.toggle("is-offline", !navigator.onLine);
  }

  window.addEventListener("online", syncOnlineState);
  window.addEventListener("offline", syncOnlineState);
  document.body.addEventListener("htmx:sendError", function () {
    document.documentElement.classList.add("is-offline");
  });
  syncOnlineState();

  // 3. Video: only one player active at a time, plus codec-failure fallback.
  document.body.addEventListener("htmx:afterSwap", function (evt) {
    document.querySelectorAll("video").forEach(function (video) {
      if (!evt.detail.target.contains(video)) {
        video.pause();
      }
    });
  });

  document.body.addEventListener(
    "error",
    function (evt) {
      var video = evt.target;
      if (!(video instanceof HTMLVideoElement) || !video.error) return;
      var code = video.error.code;
      var shell = video.closest(".player-shell");
      if (!shell) return;
      var reason =
        code === 4
          ? "This browser cannot play this clip. The camera records H.265, which Chrome and Firefox on Linux and Windows often cannot decode."
          : code === 3
            ? "Playback failed partway through. The file may be truncated on the camera, or this browser cannot decode the rest."
            : null;
      if (!reason) return;
      try {
        sessionStorage.setItem("rv-codec-fallback", "1");
      } catch (e) {
        /* private browsing or blocked storage: just skip the memory */
      }
      var downloadHref = video.getAttribute("data-download-href") || "";
      shell.innerHTML =
        '<div class="codec-fallback">' +
        "<p>" +
        reason +
        "</p>" +
        (downloadHref
          ? '<a class="btn primary" href="' + downloadHref + '">Download</a>'
          : "") +
        "</div>";
    },
    true, // capture: media errors on <video> do not bubble
  );
})();
