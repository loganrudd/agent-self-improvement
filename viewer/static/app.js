/* Agent Self-Improvement — viewer front-end.
   Phase B: render the recovery curve (overall + hard/extra + easy/medium strata),
   annotate the drift + correction marks. Reads the precomputed series from /api/state;
   no windowing math here (that lives server-side in viewer/app.py). */

const COL = (() => {
  const css = getComputedStyle(document.documentElement);
  const v = (n) => css.getPropertyValue(n).trim();
  return {
    overall: v("--c-overall"), hard: v("--c-hard"), easy: v("--c-easy"),
    drift: v("--c-drift"), correct: v("--c-correct"),
    muted: v("--muted"), line: v("--line"), text: v("--text"),
  };
})();

const pct = (x) => (x == null ? "—" : Math.round(x * 100) + "%");

function withAlpha(hex, a) {
  const h = hex.replace("#", "");
  const r = parseInt(h.slice(0, 2), 16);
  const g = parseInt(h.slice(2, 4), 16);
  const b = parseInt(h.slice(4, 6), 16);
  return `rgba(${r},${g},${b},${a})`;
}

let chart = null; // the Chart instance
let STATE = null; // the /api/state payload
let currentRun = 0; // the run the scrubber / replay is parked on
let CORR_BY_Q = {}; // question -> teacher's corrected SQL (from the correction event)
let POINTS = {}; // full {x,y} arrays per stratum (sliced for the bright reveal)

// the bright (revealed) datasets, by stratum -> index into chart.data.datasets
// order: [teacher_ceiling(0), faint overall(1), faint hard(2), bright overall(3), bright hard(4)]
const BRIGHT_IDX = { acc_overall: 3, acc_hard: 4 };

// replay transport
let playing = false;
let timer = null;
let speed = 1; // multiplier on BASE_MS
const BASE_MS = 45; // ms per run at 1x  (~240 runs -> ~11s)

const VERDICT = {
  correct: "✓ correct",
  valid_but_wrong: "⚠ valid · wrong result",
  invalid: "✗ invalid SQL",
};

/* --- custom plugin: horizontal SOTA reference label (right edge of chart) -- */
const sotaLabel = {
  id: "sotaLabel",
  afterDatasetsDraw(chart) {
    const { ctx, chartArea: area, scales } = chart;
    const y = scales.y.getPixelForValue(TEACHER_CEILING);
    if (y < area.top || y > area.bottom) return;
    ctx.save();
    ctx.font = "500 11px ui-sans-serif, system-ui, sans-serif";
    ctx.fillStyle = COL.muted;
    ctx.textBaseline = "bottom";
    ctx.textAlign = "right";
    ctx.fillText(`Teacher ceiling ${pct(TEACHER_CEILING)}`, area.right - 4, y - 3);
    ctx.restore();
  },
};

/* --- custom plugin: vertical event marks (drift / correction) ------------- */
const eventMarks = {
  id: "eventMarks",
  afterDatasetsDraw(chart, _args, opts) {
    const marks = opts.marks || [];
    const revealAt = opts.revealAt == null ? Infinity : opts.revealAt;
    const { ctx, chartArea: area, scales } = chart;
    ctx.save();
    for (const m of marks) {
      if (m.at > revealAt) continue; // a mark "fires" only once replay reaches it
      const x = scales.x.getPixelForValue(m.at);
      if (x < area.left || x > area.right) continue;

      // dashed vertical line through the plot
      ctx.beginPath();
      ctx.setLineDash([5, 4]);
      ctx.lineWidth = 1.5;
      ctx.strokeStyle = m.color;
      ctx.moveTo(x, area.top);
      ctx.lineTo(x, area.bottom);
      ctx.stroke();
      ctx.setLineDash([]);

      // label chip, anchored top or bottom so coincident marks don't collide
      ctx.font = "600 12px ui-sans-serif, system-ui, sans-serif";
      const padX = 7, padY = 4, gap = 8;
      const tw = ctx.measureText(m.label).width;
      const chipW = tw + padX * 2, chipH = 20;
      let cx = x + gap;
      if (cx + chipW > area.right) cx = x - gap - chipW; // flip if near right edge
      const cy = m.anchor === "bottom" ? area.bottom - chipH - 6 : area.top + 6;

      ctx.fillStyle = "rgba(11,15,20,0.92)";
      roundRect(ctx, cx, cy, chipW, chipH, 5);
      ctx.fill();
      ctx.strokeStyle = m.color;
      ctx.lineWidth = 1;
      roundRect(ctx, cx, cy, chipW, chipH, 5);
      ctx.stroke();
      ctx.fillStyle = m.color;
      ctx.textBaseline = "middle";
      ctx.fillText(m.label, cx + padX, cy + chipH / 2 + 0.5);
    }
    ctx.restore();
  },
};

/* --- custom plugin: the current-run cursor (driven by scrubber / replay) -- */
const runCursor = {
  id: "runCursor",
  afterDatasetsDraw(chart, _args, opts) {
    const at = opts.at;
    if (at == null || !STATE) return;
    const { ctx, chartArea: area, scales } = chart;
    const x = scales.x.getPixelForValue(at);
    if (x < area.left || x > area.right) return;
    ctx.save();
    // faint vertical line marking "you are here"
    ctx.beginPath();
    ctx.lineWidth = 1;
    ctx.strokeStyle = "rgba(230,237,243,0.30)";
    ctx.moveTo(x, area.top);
    ctx.lineTo(x, area.bottom);
    ctx.stroke();
    // dot on the overall line at this run
    const r = STATE.runs[at];
    if (r && r.acc_overall != null) {
      const y = scales.y.getPixelForValue(r.acc_overall);
      ctx.beginPath();
      ctx.arc(x, y, 4, 0, Math.PI * 2);
      ctx.fillStyle = COL.overall;
      ctx.fill();
      ctx.lineWidth = 2;
      ctx.strokeStyle = "#0b0f14";
      ctx.stroke();
    }
    ctx.restore();
  },
};

function roundRect(ctx, x, y, w, h, r) {
  ctx.beginPath();
  ctx.moveTo(x + r, y);
  ctx.arcTo(x + w, y, x + w, y + h, r);
  ctx.arcTo(x + w, y + h, x, y + h, r);
  ctx.arcTo(x, y + h, x, y, r);
  ctx.arcTo(x, y, x + w, y, r);
  ctx.closePath();
}

/* --- legend --------------------------------------------------------------- */
function buildLegend() {
  const keys = [
    { label: "Overall", color: COL.overall, strong: true },
    { label: "Hard / extra", color: COL.hard, strong: true },
  ];
  document.getElementById("legend").innerHTML = keys
    .map(
      (k) =>
        `<span class="key${k.strong ? " strong" : ""}">` +
        `<span class="swatch${k.dashed ? " dashed" : ""}" style="border-top-color:${k.color}"></span>` +
        `${k.label}</span>`
    )
    .join("");
}

const TEACHER_CEILING = 0.400; // teacher (MiniMax-M3) hard-bucket on same 30 held-out questions — python orchestrator.py --ceiling

/* --- caption: a one-line, data-driven framing of the V -------------------- */
function buildCaption(state) {
  const runs = state.runs;
  const hard = runs.map((r) => r.acc_hard).filter((x) => x != null);
  if (!hard.length) return "";
  const valley = Math.min(...hard);
  const n = state.correction ? state.correction.examples.length : 0;

  // Recovered accuracy = UNIQUE-question hard accuracy over the recovery region (after
  // correction fired), matching the headline metric. NOT the trailing-window endpoint —
  // that swings with whichever questions land in the last 20 samples (it ends on a trough
  // here even though the curve recovers and peaks well above it mid-region).
  const corrAt = state.correction ? state.correction.at : 0;
  const byQ = {};
  for (const r of runs.slice(corrAt)) {
    if (r.difficulty === "hard" && !(r.question in byQ)) byQ[r.question] = r.accuracy_raw;
  }
  const vals = Object.values(byQ);
  const recovered = vals.length
    ? vals.reduce((a, b) => a + b, 0) / vals.length
    : hard[hard.length - 1];

  let teacherClause;
  if (recovered >= TEACHER_CEILING) {
    teacherClause =
      `That pushed it past the teacher model itself (${pct(TEACHER_CEILING)} on the same ` +
      `questions) — the weak agent ended up more accurate than the model that taught it.`;
  } else {
    const pctClosed = Math.round(((recovered - valley) / (TEACHER_CEILING - valley)) * 100);
    teacherClause =
      `That autonomously closed ${pctClosed}% of the gap to the teacher model ` +
      `(${pct(TEACHER_CEILING)}, same questions).`;
  }
  return (
    `Hard-query accuracy collapsed to ${pct(valley)} under the harder distribution, ` +
    `then recovered to ${pct(recovered)} on unique held-out questions — same difficulty — ` +
    `after the agent learned ${n} example${n === 1 ? "" : "s"} from its own failures. ` +
    teacherClause
  );
}

/* --- chart ----------------------------------------------------------------
   Two layers: the full curve drawn FAINT (context, shown on load), and a BRIGHT
   curve revealed left-to-right up to the current run (driven by scrubber/replay). */
function faintDS(key, color, width, dash) {
  return {
    label: "",
    data: POINTS[key],
    borderColor: withAlpha(color, 0.4),
    borderWidth: width,
    borderDash: dash || [],
    pointRadius: 0,
    pointHitRadius: 0,
    tension: 0.25,
    spanGaps: false,
    _faint: true,
  };
}
function brightDS(key, label, color, width, dash, order) {
  return {
    label,
    data: POINTS[key].slice(0, currentRun + 1),
    borderColor: color,
    borderWidth: width,
    borderDash: dash || [],
    pointRadius: 0,
    pointHoverRadius: 0,
    tension: 0.25,
    spanGaps: false,
    borderJoinStyle: "round",
    order,
  };
}

/* --- channel panel + scrubber --------------------------------------------- */
function tone(el, kind) {
  el.classList.remove("good", "warn", "bad");
  if (kind) el.classList.add(kind);
}

function updateChannels(r) {
  const acc = document.getElementById("ch-acc");
  acc.textContent = pct(r.acc_overall);
  tone(acc, r.acc_overall >= 0.7 ? "good" : r.acc_overall < 0.5 ? "bad" : null);

  const valid = document.getElementById("ch-valid");
  valid.textContent = pct(r.validity_rate);
  tone(valid, r.validity_rate >= 0.9 ? "good" : r.validity_rate < 0.7 ? "bad" : null);

  const gap = document.getElementById("ch-gap");
  gap.textContent = (r.complexity_gap >= 0 ? "+" : "") + r.complexity_gap.toFixed(1);
  tone(gap, r.complexity_gap >= 2 ? "warn" : r.complexity_gap <= 0.5 ? "good" : null);

  const lat = document.getElementById("ch-lat");
  lat.textContent = Math.round(r.latency_ms) + " ms";
}

function updateSqlPanel(r, k) {
  document.getElementById("ex-run").textContent = k;
  document.getElementById("ex-db").textContent = r.db_id;
  document.getElementById("ex-question").textContent = r.question;
  document.getElementById("ex-generated").textContent = r.generated_sql || "—";

  const badge = document.getElementById("ex-verdict");
  badge.textContent = VERDICT[r.verdict] || r.verdict;
  badge.className = "verdict " + r.verdict;

  // show how many same-DB learned examples the agent had available for this schema
  const n = r.same_db_examples_active || 0;
  const activeEl = document.getElementById("ex-active");
  if (n > 0) {
    document.getElementById("ex-active-count").textContent = n;
    document.getElementById("ex-active-plural").textContent = n === 1 ? "" : "s";
    activeEl.hidden = false;
  } else {
    activeEl.hidden = true;
  }
}

function setCurrentRun(k) {
  currentRun = k;
  const r = STATE.runs[k];
  document.getElementById("scrub-run").textContent = "run " + k;
  document.getElementById("scrub-diff").textContent = r.difficulty;
  document.getElementById("scrub").value = k;
  updateChannels(r);
  updateSqlPanel(r, k);
  if (chart) {
    for (const key in BRIGHT_IDX) {
      chart.data.datasets[BRIGHT_IDX[key]].data = POINTS[key].slice(0, k + 1);
    }
    chart.options.plugins.runCursor.at = k;
    chart.options.plugins.eventMarks.revealAt = k;
    chart.update("none");
  }
}

function initScrubber(state) {
  const scrub = document.getElementById("scrub");
  scrub.max = state.n_runs - 1;
  scrub.value = state.n_runs - 1;
  scrub.addEventListener("input", (e) => {
    pause(); // taking manual control stops playback
    setCurrentRun(parseInt(e.target.value, 10));
  });
  setCurrentRun(state.n_runs - 1); // open on the full, bright curve; Play restarts the reveal from run 0
}

/* --- replay transport ----------------------------------------------------- */
function startTimer() {
  timer = setInterval(tick, BASE_MS / speed);
}
function stopTimer() {
  if (timer) {
    clearInterval(timer);
    timer = null;
  }
}
function tick() {
  if (currentRun >= STATE.n_runs - 1) {
    pause();
    return;
  }
  setCurrentRun(currentRun + 1);
}
function play() {
  if (currentRun >= STATE.n_runs - 1) setCurrentRun(0); // restart from the top
  playing = true;
  const b = document.getElementById("play");
  b.textContent = "❚❚ Pause";
  b.classList.add("playing");
  startTimer();
}
function pause() {
  playing = false;
  stopTimer();
  const b = document.getElementById("play");
  b.textContent = "▶ Play";
  b.classList.remove("playing");
}
function setupTransport(state) {
  document.getElementById("play").addEventListener("click", () => (playing ? pause() : play()));
  document.getElementById("speed").addEventListener("change", (e) => {
    speed = parseFloat(e.target.value);
    if (playing) {
      stopTimer();
      startTimer();
    }
  });
  const jump = document.getElementById("jump-drift");
  if (state.drift) {
    jump.addEventListener("click", () => {
      pause();
      setCurrentRun(state.drift.at);
    });
  } else {
    jump.disabled = true;
  }
}

function render(state) {
  STATE = state;
  CORR_BY_Q = {};
  if (state.correction) {
    for (const e of state.correction.examples) CORR_BY_Q[e.question] = e.correct_sql;
  }
  document.getElementById("window-size").textContent = state.window;
  buildLegend();
  document.getElementById("caption").textContent = buildCaption(state);

  const runs = state.runs;
  POINTS = {
    acc_hard: runs.map((r) => ({ x: r.run_index, y: r.acc_hard })),
    acc_overall: runs.map((r) => ({ x: r.run_index, y: r.acc_overall })),
    acc_easy: runs.map((r) => ({ x: r.run_index, y: r.acc_easy })),
  };

  const marks = [];
  if (state.drift)
    marks.push({ at: state.drift.at, color: COL.drift, label: "⚠ drift detected", anchor: "top" });
  if (state.correction)
    marks.push({
      at: state.correction.at,
      color: COL.correct,
      label: `✚ learned +${state.correction.examples.length}`,
      anchor: "bottom",
    });

  // Static SOTA reference line — constant across all runs
  const sotaData = state.runs.map((r) => ({ x: r.run_index, y: TEACHER_CEILING }));

  chart = new Chart(document.getElementById("curve"), {
    type: "line",
    data: {
      // order must match BRIGHT_IDX: [teacher_ceiling, faint overall, faint hard, bright overall, bright hard]
      datasets: [
        {
          label: "Teacher ceiling (same questions)",
          data: sotaData,
          borderColor: withAlpha(COL.muted, 0.75),
          borderWidth: 1.5,
          borderDash: [6, 5],
          pointRadius: 0,
          pointHitRadius: 0,
          tension: 0,
          spanGaps: true,
          _faint: true, // exclude from tooltip (same flag as faint strata)
        },
        faintDS("acc_overall", COL.overall, 2.5),
        faintDS("acc_hard", COL.hard, 3),
        brightDS("acc_overall", "Overall", COL.overall, 2.5, [], 31),
        brightDS("acc_hard", "Hard / extra", COL.hard, 3, [], 30),
      ],
    },
    options: {
      responsive: true,
      maintainAspectRatio: false,
      animation: false,
      interaction: { mode: "index", intersect: false },
      scales: {
        x: {
          type: "linear",
          min: 0,
          max: state.n_runs - 1,
          title: { display: true, text: "run", color: COL.muted },
          ticks: { color: COL.muted, maxTicksLimit: 10 },
          grid: { color: COL.line },
        },
        y: {
          min: 0,
          max: 1,
          title: { display: true, text: "execution accuracy", color: COL.muted },
          ticks: { color: COL.muted, callback: (v) => Math.round(v * 100) + "%" },
          grid: { color: COL.line },
        },
      },
      plugins: {
        legend: { display: false },
        tooltip: {
          filter: (item) => !item.dataset._faint, // bright layer only
          callbacks: {
            title: (items) => "run " + items[0].parsed.x,
            label: (item) => item.dataset.label + ": " + pct(item.parsed.y),
          },
        },
        eventMarks: { marks, revealAt: currentRun },
        runCursor: { at: currentRun },
      },
    },
    plugins: [sotaLabel, eventMarks, runCursor],
  });

  initScrubber(state);
  setupTransport(state);
}

/* --- boot ----------------------------------------------------------------- */
fetch("/api/state")
  .then((r) => r.json())
  .then(render)
  .catch((err) => {
    document.getElementById("caption").textContent = "Failed to load /api/state: " + err;
  });
