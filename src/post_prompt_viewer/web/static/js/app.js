// Post Prompt Viewer — small vanilla-JS islands: theme, tabs, polling, filters.
(function () {
  "use strict";
  var root = document.documentElement;

  // ---- Theme toggle (persisted) ----
  var saved = localStorage.getItem("ppv-theme");
  if (saved) root.setAttribute("data-theme", saved);
  var toggle = document.getElementById("theme-toggle");
  if (toggle) {
    toggle.addEventListener("click", function () {
      var next = root.getAttribute("data-theme") === "light" ? "dark" : "light";
      root.setAttribute("data-theme", next);
      localStorage.setItem("ppv-theme", next);
    });
  }

  // ---- Tabs (hash-linkable) ----
  var tabs = Array.prototype.slice.call(document.querySelectorAll(".tab"));
  var panels = Array.prototype.slice.call(document.querySelectorAll(".tab-panel"));
  function activate(id, push) {
    var found = false;
    tabs.forEach(function (t) {
      var on = t.dataset.tab === id;
      t.setAttribute("aria-selected", String(on));
      if (on) found = true;
    });
    panels.forEach(function (p) {
      p.classList.toggle("active", p.id === "panel-" + id);
    });
    var ap = document.getElementById("panel-" + id);
    if (ap) { var fl = ap.querySelector(".flow"); if (fl) fl.classList.add("play"); }
    if (found && push) history.replaceState(null, "", "#" + id);
    return found;
  }
  if (tabs.length) {
    tabs.forEach(function (t) {
      t.addEventListener("click", function () { activate(t.dataset.tab, true); });
    });
    var initial = (location.hash || "").replace("#", "");
    activate(activate(initial, false) ? initial : tabs[0].dataset.tab, false);
    if (!location.hash) activate(tabs[0].dataset.tab, false);
  }

  // ---- Index: auto-submit on filter change ----
  var form = document.getElementById("filter-form");
  if (form) {
    form.querySelectorAll("select").forEach(function (s) {
      s.addEventListener("change", function () { form.submit(); });
    });
  }

  // ---- Recording status poll ----
  var poll = document.querySelector("[data-status-url]");
  if (poll) {
    var url = poll.dataset.statusUrl;
    var initial = poll.dataset.recInitial;
    var statusEl = poll.querySelector("[data-rec-status]");
    var active = ["pending", "downloading", "analyzing"];
    var tick = function () {
      fetch(url)
        .then(function (r) { return r.ok ? r.json() : null; })
        .then(function (d) {
          if (!d) { setTimeout(tick, 5000); return; }
          if (statusEl) {
            statusEl.textContent = d.status;
            statusEl.className = "status-pill " + d.status;
          }
          if (d.status === "done") { location.reload(); return; }
          if (d.status === "failed") return;
          setTimeout(tick, 3000);
        })
        .catch(function () { setTimeout(tick, 5000); });
    };
    if (active.indexOf(initial) !== -1) setTimeout(tick, 2500);
  }

  // ---- Analyze / retry recording button ----
  Array.prototype.slice.call(document.querySelectorAll("[data-analyze-url]")).forEach(function (btn) {
    btn.addEventListener("click", function () {
      btn.disabled = true;
      var label = btn.textContent;
      btn.textContent = "Queued…";
      fetch(btn.dataset.analyzeUrl, { method: "POST" })
        .then(function () { setTimeout(function () { location.reload(); }, 1500); })
        .catch(function () { btn.disabled = false; btn.textContent = label; });
    });
  });

  // ---- Index: upload a saved post_prompt JSON (same ingest path as /collect) ----
  var upBtn = document.getElementById("upload-btn");
  var upInput = document.getElementById("upload-input");
  if (upBtn && upInput) {
    upBtn.addEventListener("click", function () { upInput.click(); });
    upInput.addEventListener("change", function () {
      var file = upInput.files && upInput.files[0];
      if (!file) return;
      var label = upBtn.textContent;
      upBtn.disabled = true;
      upBtn.textContent = "Uploading…";
      var reader = new FileReader();
      reader.onload = function () {
        fetch(upBtn.dataset.uploadUrl, {
          method: "POST",
          headers: { "content-type": "application/json" },
          body: reader.result,
        })
          .then(function (r) { return r.json().then(function (d) { return { ok: r.ok, d: d }; }); })
          .then(function (res) {
            if (res.ok && res.d.url) { location.href = res.d.url; return; }
            alert("Upload failed: " + (res.d.detail || "unknown error"));
            upBtn.disabled = false; upBtn.textContent = label;
          })
          .catch(function () { alert("Upload failed."); upBtn.disabled = false; upBtn.textContent = label; });
      };
      reader.readAsText(file);
      upInput.value = "";
    });
  }

  // ---- Index: show + copy the post_prompt_url (built from the real origin) ----
  var cu = document.getElementById("collect-url");
  if (cu) {
    var prefix = cu.dataset.prefix || "";
    var user = cu.dataset.user || "";
    var pass = cu.dataset.pass || "";
    var path = prefix ? prefix + "/" : "/collect";
    var creds = (user && pass) ? encodeURIComponent(user) + ":" + encodeURIComponent(pass) + "@" : "";
    var url = location.protocol + "//" + creds + location.host + path;
    cu.textContent = url;
    var copyBtn = document.getElementById("copy-collect");
    if (copyBtn) {
      copyBtn.addEventListener("click", function () {
        var ok = function () {
          var t = copyBtn.textContent;
          copyBtn.textContent = "Copied";
          setTimeout(function () { copyBtn.textContent = t; }, 1200);
        };
        var fallback = function () {
          var ta = document.createElement("textarea");
          ta.value = url; ta.style.position = "fixed"; ta.style.opacity = "0";
          document.body.appendChild(ta); ta.select();
          try { document.execCommand("copy"); ok(); } catch (e) {}
          document.body.removeChild(ta);
        };
        if (navigator.clipboard && navigator.clipboard.writeText) {
          navigator.clipboard.writeText(url).then(ok).catch(fallback);
        } else { fallback(); }
      });
    }
  }

  // ---- Timeline flow: click a turn to expand its latency detail ----
  Array.prototype.slice.call(document.querySelectorAll(".fx-lat")).forEach(function (btn) {
    btn.addEventListener("click", function () {
      var detail = btn.parentNode.querySelector(".fx-detail");
      if (!detail) return;
      var willOpen = detail.hasAttribute("hidden");
      if (willOpen) { detail.removeAttribute("hidden"); } else { detail.setAttribute("hidden", ""); }
      btn.setAttribute("aria-expanded", willOpen ? "true" : "false");
    });
  });

  // ---- Sub-tabs (Timeline: Flow / Waterfall) ----
  Array.prototype.slice.call(document.querySelectorAll(".subtab")).forEach(function (btn) {
    btn.addEventListener("click", function () {
      var sub = btn.dataset.sub;
      var scope = btn.closest(".tab-panel") || document;
      Array.prototype.slice.call(scope.querySelectorAll(".subtab")).forEach(function (b) {
        b.classList.toggle("active", b === btn);
      });
      Array.prototype.slice.call(scope.querySelectorAll(".sub-panel")).forEach(function (p) {
        p.classList.toggle("active", p.dataset.subpanel === sub);
      });
    });
  });

  // ---- Waterfall: click an event row to expand its metadata ----
  Array.prototype.slice.call(document.querySelectorAll(".wf-row")).forEach(function (row) {
    row.addEventListener("click", function () {
      var detail = row.nextElementSibling;
      if (!detail || !detail.classList.contains("wf-detail")) return;
      if (detail.hasAttribute("hidden")) detail.removeAttribute("hidden");
      else detail.setAttribute("hidden", "");
    });
  });
})();
