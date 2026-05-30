// Recording player: click any turn to hear it, play/pause (space), prev/next
// turn, and highlight the turn currently playing. Driven by per-turn seek
// times embedded in #seek-index and data-seek/data-turn on turns + bars.
(function () {
  "use strict";
  var audio = document.getElementById("ppv-audio");
  if (!audio) return;

  var pp = document.getElementById("pp");
  var prevB = document.getElementById("prev-turn");
  var nextB = document.getElementById("next-turn");
  var timeEl = document.getElementById("player-time");
  var nowEl = document.getElementById("player-now");

  var seekIndex = [];
  var dataEl = document.getElementById("seek-index");
  if (dataEl) {
    try { seekIndex = JSON.parse(dataEl.textContent) || []; } catch (e) { seekIndex = []; }
  }
  seekIndex.sort(function (a, b) { return a.seek - b.seek; });

  function fmtTime(s) {
    s = Math.max(0, Math.floor(s || 0));
    return Math.floor(s / 60) + ":" + ("0" + (s % 60)).slice(-2);
  }
  function play() { audio.play().catch(function () {}); }
  function seekTo(sec) { audio.currentTime = sec; play(); }

  // Click any [data-seek] turn / bar to hear it (ignore inner links/buttons).
  document.addEventListener("click", function (e) {
    if (e.target.closest("a, button, summary, input, select, audio")) return;
    var el = e.target.closest("[data-seek]");
    if (!el) return;
    var sec = parseFloat(el.getAttribute("data-seek"));
    if (!isNaN(sec)) seekTo(sec);
  });

  // Play / pause (button + spacebar).
  function toggle() { if (audio.paused) play(); else audio.pause(); }
  if (pp) pp.addEventListener("click", toggle);
  audio.addEventListener("play", function () { if (pp) { pp.innerHTML = "&#10073;&#10073;"; } });
  audio.addEventListener("pause", function () { if (pp) { pp.innerHTML = "&#9654;"; } });
  document.addEventListener("keydown", function (e) {
    if (e.code === "Space" && !/^(INPUT|TEXTAREA|SELECT)$/.test(e.target.tagName || "")) {
      e.preventDefault();
      toggle();
    }
  });

  // Prev / next spoken turn.
  function jump(dir) {
    if (!seekIndex.length) return;
    var t = audio.currentTime, target = null, i;
    if (dir > 0) {
      for (i = 0; i < seekIndex.length; i++) {
        if (seekIndex[i].seek > t + 0.2) { target = seekIndex[i]; break; }
      }
    } else {
      for (i = seekIndex.length - 1; i >= 0; i--) {
        if (seekIndex[i].seek < t - 0.4) { target = seekIndex[i]; break; }
      }
    }
    if (target) seekTo(target.seek);
  }
  if (prevB) prevB.addEventListener("click", function () { jump(-1); });
  if (nextB) nextB.addEventListener("click", function () { jump(1); });

  // Highlight the turn currently playing.
  var lastIdx = null;
  audio.addEventListener("timeupdate", function () {
    if (timeEl) timeEl.textContent = fmtTime(audio.currentTime);
    var cur = null, i;
    for (i = 0; i < seekIndex.length; i++) {
      if (seekIndex[i].seek <= audio.currentTime + 0.05) cur = seekIndex[i]; else break;
    }
    var idx = cur ? cur.idx : null;
    if (idx === lastIdx) return;
    Array.prototype.forEach.call(document.querySelectorAll(".playing"), function (el) {
      el.classList.remove("playing");
    });
    if (idx != null) {
      Array.prototype.forEach.call(document.querySelectorAll('[data-turn="' + idx + '"]'), function (el) {
        el.classList.add("playing");
      });
      if (nowEl && cur.label) nowEl.textContent = cur.label;
    }
    lastIdx = idx;
  });
})();
