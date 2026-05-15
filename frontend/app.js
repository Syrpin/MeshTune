const state = {
  ws: null,
  tempChart: null,
  labels: [],
  nozzle: [],
  bed: [],
};

function setText(id, val) {
  const el = document.getElementById(id);
  if (!el) return;
  el.textContent = val;
}

function toNum(v) {
  return typeof v === "number" && Number.isFinite(v) ? v : null;
}

function fmt(v, digits = 2) {
  const n = toNum(v);
  return n === null ? "-" : n.toFixed(digits);
}

function setConn(ok, txt) {
  const chip = document.getElementById("connState");
  chip.textContent = txt;
  chip.classList.toggle("ok", ok);
  chip.classList.toggle("bad", !ok);
}

function initChart() {
  const ctx = document.getElementById("tempChart");
  state.tempChart = new Chart(ctx, {
    type: "line",
    data: {
      labels: state.labels,
      datasets: [
        { label: "Nozzle", data: state.nozzle, borderColor: "#ffb84a", tension: 0.25 },
        { label: "Bed", data: state.bed, borderColor: "#1fb6a6", tension: 0.25 },
      ],
    },
    options: {
      animation: false,
      responsive: true,
      plugins: { legend: { labels: { color: "#cfe4e8" } } },
      scales: {
        x: { ticks: { color: "#8aa0a6" } },
        y: { ticks: { color: "#8aa0a6" } },
      },
    },
  });
}

function pushTemps(nozzle, bed) {
  const now = new Date();
  const label = `${String(now.getHours()).padStart(2, "0")}:${String(now.getMinutes()).padStart(2, "0")}:${String(now.getSeconds()).padStart(2, "0")}`;
  state.labels.push(label);
  state.nozzle.push(nozzle);
  state.bed.push(bed);
  if (state.labels.length > 120) {
    state.labels.shift();
    state.nozzle.shift();
    state.bed.shift();
  }
  state.tempChart.update();
}

function updateEndstop(id, val) {
  const el = document.getElementById(id);
  el.classList.remove("ok", "bad");
  if (val === "triggered") el.classList.add("ok");
  else if (val === "open") el.classList.add("bad");
}

function renderMesh(mesh) {
  if (!mesh || !mesh.points) {
    setText("meshStats", "Нет данных mesh.");
    return;
  }

  Plotly.newPlot("meshPlot", [
    {
      z: mesh.points,
      type: "surface",
      colorscale: "Portland",
      contours: { z: { show: true, usecolormap: true } },
    },
  ], {
    paper_bgcolor: "rgba(0,0,0,0)",
    plot_bgcolor: "rgba(0,0,0,0)",
    margin: { l: 0, r: 0, b: 0, t: 0 },
    scene: {
      xaxis: { color: "#9fb5ba" },
      yaxis: { color: "#9fb5ba" },
      zaxis: { color: "#9fb5ba" },
    },
  }, { responsive: true });

  const s = mesh.stats || {};
  setText(
    "meshStats",
    `min ${fmt(s.z_min, 3)} mm | max ${fmt(s.z_max, 3)} mm | range ${fmt(s.z_range, 3)} mm`
  );
}

function applySnapshot(data) {
  const t = data.temperatures || {};
  const p = data.position || {};
  const e = data.endstops || {};
  const pr = data.print_status || {};
  const proto = data.protocol || {};

  setText("nozzleTemp", `${fmt(t.nozzle, 1)} / ${fmt(t.target_nozzle, 1)} C`);
  setText("bedTemp", `${fmt(t.bed, 1)} / ${fmt(t.target_bed, 1)} C`);

  setText("posX", fmt(p.x, 3));
  setText("posY", fmt(p.y, 3));
  setText("posZ", fmt(p.z, 3));
  setText("posE", fmt(p.e, 3));
  setText("m114Hint", proto.m114_payload_available ? "M114 payload доступен." : "M114 payload недоступен на этом канале; показывается последний известный кэш.");

  updateEndstop("xStop", e.x);
  updateEndstop("yStop", e.y);
  updateEndstop("zStop", e.z);

  setText("printState", pr.state || "unknown");
  setText("printBytes", `${pr.bytes ?? "-"} / ${pr.bytes_total ?? "-"}`);
  setText("printLayer", `${pr.layer ?? "-"} / ${pr.layer_total ?? "-"}`);

  let progress = 0;
  if (typeof pr.bytes === "number" && typeof pr.bytes_total === "number" && pr.bytes_total > 0) {
    progress = Math.max(0, Math.min(100, (pr.bytes / pr.bytes_total) * 100));
  }
  document.getElementById("printProgress").style.width = `${progress.toFixed(1)}%`;

  if (proto.last_raw_response) {
    setText("lastRaw", proto.last_raw_response);
  }

  if (toNum(t.nozzle) !== null && toNum(t.bed) !== null) {
    pushTemps(t.nozzle, t.bed);
  }

  renderMesh(data.bed_mesh);
}

async function refreshMesh() {
  const resp = await fetch("/api/refresh_mesh", { method: "POST" });
  if (!resp.ok) {
    const txt = await resp.text();
    throw new Error(txt);
  }
  const status = await fetch("/api/status");
  const data = await status.json();
  applySnapshot(data);
}

function connectWs() {
  const proto = location.protocol === "https:" ? "wss" : "ws";
  const ws = new WebSocket(`${proto}://${location.host}/ws`);
  state.ws = ws;

  ws.onopen = () => setConn(true, "connected");
  ws.onclose = () => {
    setConn(false, "disconnected");
    setTimeout(connectWs, 1500);
  };
  ws.onerror = () => setConn(false, "error");
  ws.onmessage = (ev) => {
    try {
      const data = JSON.parse(ev.data);
      if (data && data.temperatures) {
        applySnapshot(data);
      }
    } catch (_) {
      // ignore text control frames
    }
  };
}

async function bootstrap() {
  initChart();
  setConn(false, "connecting...");

  try {
    const resp = await fetch("/api/status");
    if (resp.ok) applySnapshot(await resp.json());
  } catch (_) {
    // ignore at startup
  }

  connectWs();

  document.getElementById("refreshMeshBtn").addEventListener("click", async () => {
    try {
      await refreshMesh();
    } catch (e) {
      alert(`Не удалось обновить mesh: ${e.message}`);
    }
  });
}

bootstrap();
