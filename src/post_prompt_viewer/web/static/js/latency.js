// Recording tab: a live trace on the stereo waveform. Speech regions, the
// measured latency gaps (H->AI fuchsia / AI->H turquoise) with ms labels, and
// the pipeline milestones (tool calls, EOT, token, audio) riding the waveform —
// each lit up as the playhead passes it. Zoom + horizontal scroll, a latency
// strip overview, prev/next-latency nav, and a 1x/1.5x/2x speed control.
// Bound to the shared #ppv-audio. Built lazily (wavesurfer needs a visible width).
(function () {
  "use strict";
  var dataEl = document.getElementById("analysis-data");
  var container = document.getElementById("waveform");
  var audio = document.getElementById("ppv-audio");
  if (!dataEl || !container || !audio || typeof WaveSurfer === "undefined") return;

  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }

  var FUCHSIA = "247,42,114", TURQ = "64,224,208";

  // Measured latency gaps, in time order.
  var gaps = [];
  (data.latencies || []).forEach(function (l) {
    if (l.human_stop != null && l.ai_start != null)
      gaps.push({ start: l.human_stop, end: l.ai_start, ms: Math.round(l.latency * 1000), dir: "h2ai" });
  });
  (data.human_response_latencies || []).forEach(function (l) {
    if (l.ai_stop != null && l.human_start != null)
      gaps.push({ start: l.ai_stop, end: l.human_start, ms: Math.round(l.latency * 1000), dir: "ai2h" });
  });
  gaps.sort(function (a, b) { return a.start - b.start; });

  // Milestone marker colours/opacity by kind.
  var MK = { tool: "96,27,230", turn_decided: "255,215,0", first_token: FUCHSIA, first_audio: FUCHSIA };
  var MK_OP = { tool: 0.26, turn_decided: 0.5, first_token: 0.45, first_audio: 0.65 };
  var markerEls = [];

  var built = false, ws = null;
  function build() {
    if (built) return;
    built = true;
    var regions = WaveSurfer.Regions ? WaveSurfer.Regions.create() : null;
    ws = WaveSurfer.create({
      container: container, height: 116,
      waveColor: "rgba(160,160,170,0.4)", progressColor: "rgba(160,160,170,0.75)",
      cursorColor: "#F72A72", autoScroll: true, autoCenter: true,
      media: audio, plugins: regions ? [regions] : [],
    });
    var zoom = document.getElementById("wf-zoom");
    ws.on("decode", function () {
      if (regions) {
        // faint speech regions, behind everything
        (data.ai_segments || []).forEach(function (s) {
          regions.addRegion({ start: s.start, end: s.end, color: "rgba(" + FUCHSIA + ",0.09)", drag: false, resize: false });
        });
        (data.human_segments || []).forEach(function (s) {
          regions.addRegion({ start: s.start, end: s.end, color: "rgba(4,78,244,0.09)", drag: false, resize: false });
        });
        // measured latency gaps, shaded with an ms label
        gaps.forEach(function (g) {
          regions.addRegion({ start: g.start, end: g.end,
            color: "rgba(" + (g.dir === "h2ai" ? FUCHSIA : TURQ) + ",0.40)", content: g.ms + "ms", drag: false, resize: false });
        });
        // pipeline milestones — markers that ride the waveform and fire on pass-through
        (data.markers || []).forEach(function (m) {
          var isTool = m.kind === "tool";
          var lab = document.createElement("span");
          lab.className = "mk-label";
          lab.textContent = m.label;
          var r = regions.addRegion({
            start: m.t, end: m.t + (m.dur > 0 ? m.dur : (isTool ? 0.25 : 0.05)),
            color: "rgba(" + (MK[m.kind] || "255,255,255") + "," + (MK_OP[m.kind] || 0.4) + ")",
            content: lab, drag: false, resize: false,
          });
          if (r && r.element) {
            r.element.classList.add("wave-mk", isTool ? "mk-tool" : "mk-ms");
            markerEls.push({ t: m.t, el: r.element, on: false });
          }
        });
      }
      // start zoomed in (px/sec) so it scrolls; clamp the slider's floor to "fit"
      var fit = container.clientWidth / (data.duration || 1);
      if (zoom) {
        zoom.min = String(Math.max(5, Math.floor(fit)));
        if (parseFloat(zoom.value) < parseFloat(zoom.min)) zoom.value = zoom.min;
      }
      try { ws.zoom(zoom ? parseFloat(zoom.value) : 60); } catch (e) {}
    });
    if (zoom) zoom.addEventListener("input", function () {
      if (ws) { try { ws.zoom(parseFloat(zoom.value)); } catch (e) {} }
    });
    renderStrip();
  }

  // Light up each milestone marker as the playhead reaches it.
  audio.addEventListener("timeupdate", function () {
    if (!markerEls.length) return;
    var ct = audio.currentTime;
    for (var i = 0; i < markerEls.length; i++) {
      var m = markerEls[i], on = ct >= m.t - 0.05 && ct <= m.t + 0.3;
      if (on !== m.on) { m.on = on; m.el.classList.toggle("firing", on); }
    }
  });

  // Latency-strip overview: a bar per gap (position by time, height by ms).
  function renderStrip() {
    var strip = document.getElementById("latency-strip");
    if (!strip || !data.duration || !gaps.length) return;
    var maxMs = gaps.reduce(function (m, g) { return Math.max(m, g.ms); }, 1);
    strip.innerHTML = gaps.map(function (g, i) {
      var left = (g.start / data.duration) * 100;
      var w = Math.max(0.5, ((g.end - g.start) / data.duration) * 100);
      var h = 18 + 78 * (g.ms / maxMs);
      var col = g.dir === "h2ai" ? FUCHSIA : TURQ;
      return '<span class="ls-bar" data-i="' + i + '" data-tip="' +
        (g.dir === "h2ai" ? "H→AI " : "AI→H ") + g.ms + 'ms" style="left:' + left.toFixed(2) +
        "%;width:" + w.toFixed(2) + "%;height:" + h.toFixed(0) + "%;background:rgb(" + col + ')"></span>';
    }).join("");
    Array.prototype.forEach.call(strip.querySelectorAll(".ls-bar"), function (b) {
      b.addEventListener("click", function () { seekGap(parseInt(b.getAttribute("data-i"), 10)); });
    });
  }

  var cur = -1;
  function seekGap(i) {
    if (!gaps.length) return;
    cur = ((i % gaps.length) + gaps.length) % gaps.length;
    var g = gaps[cur];
    audio.currentTime = Math.max(0, g.start - 0.4);
    audio.play().catch(function () {});
    var now = document.getElementById("lat-now");
    if (now) now.textContent = (g.dir === "h2ai" ? "H→AI " : "AI→H ") + g.ms + "ms  (" + (cur + 1) + "/" + gaps.length + ")";
    Array.prototype.forEach.call(document.querySelectorAll(".ls-bar"), function (b, j) { b.classList.toggle("on", j === cur); });
  }
  var prevB = document.getElementById("prev-lat"), nextB = document.getElementById("next-lat");
  if (prevB) prevB.addEventListener("click", function () { seekGap(cur < 0 ? gaps.length - 1 : cur - 1); });
  if (nextB) nextB.addEventListener("click", function () { seekGap(cur + 1); });

  // Playback speed (1x / 1.5x / 2x).
  Array.prototype.forEach.call(document.querySelectorAll(".spd"), function (b) {
    b.addEventListener("click", function () {
      audio.playbackRate = parseFloat(b.getAttribute("data-spd"));
      Array.prototype.forEach.call(document.querySelectorAll(".spd"), function (x) { x.classList.toggle("active", x === b); });
    });
  });

  // Build when the Recording panel is first shown.
  var panel = document.getElementById("panel-recording");
  function maybe() { if (panel && panel.classList.contains("active")) build(); }
  var tab = document.querySelector('.tab[data-tab="recording"]');
  if (tab) tab.addEventListener("click", function () { setTimeout(maybe, 60); });
  maybe();
})();
