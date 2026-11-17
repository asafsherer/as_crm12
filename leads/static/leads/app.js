// Replaces inline handlers so the page can run under a strict Content-Security-Policy.
document.addEventListener("submit", function (e) {
  var msg = (e.submitter && e.submitter.getAttribute("data-confirm")) || (e.target.getAttribute && e.target.getAttribute("data-confirm"));
  if (msg && !window.confirm(msg)) e.preventDefault();
});
document.addEventListener("change", function (e) {
  var t = e.target;
  if (t.hasAttribute && t.hasAttribute("data-other-toggle")) {  // free-text name only for "other"
    var n = t.form.querySelector("[data-other-name]");
    if (n) { n.hidden = t.value !== "other"; if (n.hidden) n.value = ""; }
  }
  if (t.hasAttribute && t.hasAttribute("data-select-all")) {
    document.querySelectorAll("[data-doc-check]").forEach(function (c) { c.checked = t.checked; });
  }
  if (e.target.hasAttribute && e.target.hasAttribute("data-autosubmit") && e.target.form) e.target.form.submit();
});
document.addEventListener("click", function (e) {
  var a = e.target.closest && e.target.closest("[data-back]");
  if (a) { e.preventDefault(); history.back(); }
});
// Mobile navigation drawer (no inline handlers: strict CSP).
(function () {
  var b = document.body;
  function set(open) {
    b.classList.toggle("nav-open", open);
    var t = document.querySelector("[data-nav-toggle]");
    if (t) t.setAttribute("aria-expanded", open ? "true" : "false");
  }
  document.addEventListener("click", function (e) {
    if (e.target.closest && e.target.closest("[data-nav-toggle]")) set(!b.classList.contains("nav-open"));
    else if (e.target.closest && (e.target.closest("[data-nav-close]") || e.target.closest(".side .nav a"))) set(false);
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") set(false); });
})();
// Copy-to-clipboard buttons: <button data-copy="#id">
document.addEventListener("click", function (e) {
  var b = e.target.closest && e.target.closest("[data-copy]");
  if (!b) return;
  var el = document.querySelector(b.getAttribute("data-copy"));
  if (el && navigator.clipboard) navigator.clipboard.writeText(el.innerText).then(function () {
    var t = b.textContent; b.textContent = "הועתק ✓"; setTimeout(function () { b.textContent = t; }, 1500);
  });
});

// Lead side panel: click a name to read the lead without leaving the list (falls back to the full page without JS).
(function () {
  var drawer = document.getElementById("drawer"), scrim = document.querySelector(".drawer-scrim");
  if (!drawer) return;
  function close() { drawer.hidden = true; scrim.hidden = true; drawer.innerHTML = ""; }
  document.addEventListener("click", function (e) {
    var a = e.target.closest && e.target.closest("[data-drawer]");
    if (a && !(e.ctrlKey || e.metaKey || e.shiftKey)) {
      e.preventDefault();
      var url = a.getAttribute("data-drawer") + "?next=" + encodeURIComponent(location.pathname + location.search);
      fetch(url, { credentials: "same-origin" }).then(function (r) { if (!r.ok) throw 0; return r.text(); }).then(function (html) {
        drawer.innerHTML = html; drawer.hidden = false; scrim.hidden = false;
      }).catch(function () { location.href = a.href; });
      return;
    }
    if (e.target.closest && e.target.closest("[data-drawer-close]")) { close(); return; }
    var tab = e.target.closest && e.target.closest("[data-tab]");
    if (tab && drawer.contains(tab)) {
      drawer.querySelectorAll("[data-tab]").forEach(function (b) { b.classList.toggle("on", b === tab); });
      drawer.querySelectorAll(".dpane").forEach(function (p) { p.classList.toggle("on", p.id === tab.getAttribute("data-tab")); });
    }
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") close(); });
})();
// Status chip menu: one shared popup, pointed at the clicked lead.
(function () {
  var menu = document.getElementById("statusMenu");
  if (!menu) return;
  document.addEventListener("click", function (e) {
    var b = e.target.closest && e.target.closest("[data-status-menu]");
    if (b) {
      menu.action = b.getAttribute("data-action");
      var r = b.getBoundingClientRect(), main = menu.offsetParent || document.body, base = main.getBoundingClientRect();
      menu.hidden = false;
      var left = r.left - base.left, w = menu.offsetWidth;
      if (left + w > main.clientWidth) left = main.clientWidth - w - 8;
      menu.style.top = (r.bottom - base.top + 6) + "px";
      menu.style.left = Math.max(8, left) + "px";
      e.stopPropagation();
      return;
    }
    if (!menu.hidden && !menu.contains(e.target)) menu.hidden = true;
  });
  document.addEventListener("keydown", function (e) { if (e.key === "Escape") menu.hidden = true; });
})();
// <dialog> open/close without inline handlers.
document.addEventListener("click", function (e) {
  var o = e.target.closest && e.target.closest("[data-open]");
  if (o) { var d = document.querySelector(o.getAttribute("data-open")); if (d && d.showModal) d.showModal(); return; }
  var c = e.target.closest && e.target.closest("[data-close]");
  if (c) { var dd = c.closest("dialog"); if (dd) dd.close(); }
});
