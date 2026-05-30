// Latency tab: waveform with AI/Human segment overlays and per-turn markers,
// bound to the shared #ppv-audio element (the player drives playback/seek).
// Initialised lazily (wavesurfer mis-measures width inside a hidden panel).
(function () {
  "use strict";
  var dataEl = document.getElementById("analysis-data");
  var container = document.getElementById("waveform");
  var audio = document.getElementById("ppv-audio");
  if (!dataEl || !container || !audio || typeof WaveSurfer === "undefined") return;

  var data;
  try { data = JSON.parse(dataEl.textContent); } catch (e) { return; }

  var turns = [];
  var idxEl = document.getElementById("seek-index");
  if (idxEl) { try { turns = JSON.parse(idxEl.textContent) || []; } catch (e) { turns = []; } }

  var built = false;
  function build() {
    if (built) return;
    built = true;
    var regions = WaveSurfer.Regions ? WaveSurfer.Regions.create() : null;
    var ws = WaveSurfer.create({
      container: container,
      height: 96,
      waveColor: "rgba(160,160,170,0.45)",
      progressColor: "rgba(160,160,170,0.8)",
      cursorColor: "#F72A72",
      media: audio,
      plugins: regions ? [regions] : [],
    });
    ws.on("decode", function () {
      if (!regions) return;
      (data.ai_segments || []).forEach(function (s) {
        regions.addRegion({ start: s.start, end: s.end, color: "rgba(247,42,114,0.18)", drag: false, resize: false });
      });
      (data.human_segments || []).forEach(function (s) {
        regions.addRegion({ start: s.start, end: s.end, color: "rgba(4,78,244,0.16)", drag: false, resize: false });
      });
      // Thin marker at each spoken turn's start (blue = human, fuchsia = AI).
      turns.forEach(function (t) {
        var c = t.kind === "human" ? "rgba(4,78,244,0.9)" : "rgba(247,42,114,0.9)";
        regions.addRegion({ start: t.seek, end: t.seek + 0.12, color: c, drag: false, resize: false });
      });
    });
  }

  var panel = document.getElementById("panel-latency");
  function maybe() { if (panel && panel.classList.contains("active")) build(); }
  var tab = document.querySelector('.tab[data-tab="latency"]');
  if (tab) tab.addEventListener("click", function () { setTimeout(maybe, 60); });
  maybe();
})();
