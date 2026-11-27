/* Topology page (CR-010).
 *
 * The first half is plain functions (no page needed), so they can be tested in Node.
 * The second half draws the diagram from the JSON in #topo-data and keeps it up to date:
 * after every check round the numbers and colours change IN PLACE (no reload), so your zoom,
 * pan and dragged positions stay.
 */

const TOPO_NODE_W = 156, TOPO_NODE_H = 84;

function topoCenter(node) {
  return { x: node.x + TOPO_NODE_W / 2, y: node.y + TOPO_NODE_H / 2 };
}

/* Line between two devices. Several links between the same pair are drawn side by side. */
function topoLinkLine(a, b, slot, slots) {
  const ca = topoCenter(a), cb = topoCenter(b);
  const dx = cb.x - ca.x, dy = cb.y - ca.y, len = Math.hypot(dx, dy) || 1;
  const nx = -dy / len, ny = dx / len;
  const off = (slot - (slots - 1) / 2) * 22;
  return { x1: ca.x + nx * off, y1: ca.y + ny * off, x2: cb.x + nx * off, y2: cb.y + ny * off };
}

function topoPointOn(line, t) {
  return { x: line.x1 + (line.x2 - line.x1) * t, y: line.y1 + (line.y2 - line.y1) * t };
}

/* What changed between two states? `reload` only when the SET of devices/links changed
 * (someone added or removed one); a new status or a new traffic number is just an update. */
function topoDiff(prev, next) {
  const nodeKey = (n) => [n.id, n.name].join("|");
  const linkKey = (l) => [l.id, l.a.id, l.a.port, l.b.id, l.b.port, l.slot, l.slots].join("|");
  const same =
    prev.nodes.length === next.nodes.length &&
    prev.nodes.every((n, i) => nodeKey(n) === nodeKey(next.nodes[i])) &&
    prev.links.length === next.links.length &&
    prev.links.every((l, i) => linkKey(l) === linkKey(next.links[i])) &&
    JSON.stringify(prev.agent_lines) === JSON.stringify(next.agent_lines);
  if (!same) return { reload: true, nodes: [], links: [] };
  return {
    reload: false,
    nodes: next.nodes
      .filter((n, i) => n.status !== prev.nodes[i].status || n.info !== prev.nodes[i].info)
      .map((n) => ({ id: n.id, status: n.status, info: n.info })),
    links: next.links
      .filter((l, i) => l.state !== prev.links[i].state || l.traffic.text !== prev.links[i].traffic.text)
      .map((l) => ({ id: l.id, state: l.state, text: l.traffic.text })),
  };
}

(function () {
  const dataEl = typeof document !== "undefined" && document.getElementById("topo-data");
  if (!dataEl) return;

  const state = JSON.parse(dataEl.textContent);
  const stage = document.getElementById("topoStage");
  const canvas = document.getElementById("topoCanvas");
  const svg = document.getElementById("topoSvg");
  const nodesLayer = document.getElementById("topoNodes");
  const SVG_NS = "http://www.w3.org/2000/svg";
  const nodeById = {};
  const nodeEls = {};
  const linkEls = {};
  const agentEls = [];
  let scale = 1, panX = 0, panY = 0, drag = null, pan = null;

  function svgEl(name, attrs, cls) {
    const e = document.createElementNS(SVG_NS, name);
    Object.entries(attrs || {}).forEach(([k, v]) => e.setAttribute(k, v));
    if (cls) e.setAttribute("class", cls);
    return e;
  }

  function render() {
    state.nodes.forEach((n) => {
      nodeById[n.id] = n;
      const el = document.createElement("div");
      el.className = "node-card " + n.status;
      el.dataset.id = n.id;
      el.style.left = n.x + "px";
      el.style.top = n.y + "px";
      const name = document.createElement("div"); name.className = "node-name"; name.textContent = n.name;
      const ip = document.createElement("div"); ip.className = "node-ip"; ip.textContent = n.ip;
      const info = document.createElement("div"); info.className = "node-metrics"; info.textContent = n.info;
      el.append(name, ip, info);
      el.addEventListener("mousedown", (e) => startDrag(e, n));
      nodesLayer.appendChild(el);
      nodeEls[n.id] = { el, info };
    });
    state.agent_lines.forEach((a) => {
      const line = svgEl("line", {}, "topo-agent");
      svg.appendChild(line);
      agentEls.push({ line, from: a.from, to: a.to });
    });
    state.links.forEach((l) => {
      const g = svgEl("g", {}, "topo-link " + l.state);
      const line = svgEl("line");
      const pa = svgEl("text", { "text-anchor": "middle" }, "topo-port"); pa.textContent = l.a.port;
      const pb = svgEl("text", { "text-anchor": "middle" }, "topo-port"); pb.textContent = l.b.port;
      const tr = svgEl("text", { "text-anchor": "middle" }, "topo-traffic"); tr.textContent = l.traffic.text;
      g.append(line, pa, pb, tr);
      svg.appendChild(g);
      linkEls[l.id] = { g, line, pa, pb, tr };
    });
    layoutLines();
  }

  function layoutLines() {
    agentEls.forEach((a) => {
      const f = topoCenter(nodeById[a.from]), t = topoCenter(nodeById[a.to]);
      a.line.setAttribute("x1", f.x); a.line.setAttribute("y1", f.y);
      a.line.setAttribute("x2", t.x); a.line.setAttribute("y2", t.y);
    });
    state.links.forEach((l) => {
      const els = linkEls[l.id];
      const ln = topoLinkLine(nodeById[l.a.id], nodeById[l.b.id], l.slot, l.slots);
      els.line.setAttribute("x1", ln.x1); els.line.setAttribute("y1", ln.y1);
      els.line.setAttribute("x2", ln.x2); els.line.setAttribute("y2", ln.y2);
      const place = (el, t, dy) => {
        const p = topoPointOn(ln, t);
        el.setAttribute("x", p.x); el.setAttribute("y", p.y + dy);
      };
      place(els.pa, 0.2, -5); place(els.pb, 0.8, -5); place(els.tr, 0.5, -6);
    });
  }

  function applyView() {
    canvas.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
  }

  /* ---- dragging devices (positions are saved) and panning the view ---- */
  function startDrag(e, n) {
    e.preventDefault(); e.stopPropagation();
    drag = { n, sx: e.clientX, sy: e.clientY, ox: n.x, oy: n.y, moved: false };
  }
  window.addEventListener("mousemove", (e) => {
    if (drag) {
      drag.moved = true;
      drag.n.x = Math.max(0, Math.round(drag.ox + (e.clientX - drag.sx) / scale));
      drag.n.y = Math.max(0, Math.round(drag.oy + (e.clientY - drag.sy) / scale));
      const el = nodeEls[drag.n.id].el;
      el.style.left = drag.n.x + "px"; el.style.top = drag.n.y + "px";
      layoutLines();
    } else if (pan) {
      panX = e.clientX - pan.sx; panY = e.clientY - pan.sy; applyView();
    }
  });
  window.addEventListener("mouseup", async () => {
    if (drag && drag.moved) {
      try {
        await fetch("/topology/api/position", {
          method: "POST", headers: { "Content-Type": "application/json" },
          body: JSON.stringify({ device_id: drag.n.id, x: drag.n.x, y: drag.n.y }),
        });
      } catch (err) { /* the position is simply not saved this time */ }
    }
    drag = null; pan = null;
  });
  stage.addEventListener("mousedown", (e) => {
    if (e.target.closest(".node-card")) return;
    pan = { sx: e.clientX - panX, sy: e.clientY - panY };
  });
  stage.addEventListener("wheel", (e) => {
    e.preventDefault();
    scale = Math.min(2, Math.max(0.3, scale - e.deltaY * 0.001));
    applyView();
  });
  document.getElementById("topo-fit-btn").addEventListener("click", () => { scale = 1; panX = 0; panY = 0; applyView(); });
  document.getElementById("topo-reset-layout-btn").addEventListener("click", async () => {
    if (!confirm("Return all devices to the automatic arrangement?")) return;
    await fetch("/topology/api/reset-layout", { method: "POST" });
    location.reload();
  });

  /* ---- update numbers and colours in place after every check round ---- */
  function applyDiff(d) {
    d.nodes.forEach((n) => {
      const ref = nodeEls[n.id];
      ref.el.className = "node-card " + n.status; ref.info.textContent = n.info;
      nodeById[n.id].status = n.status; nodeById[n.id].info = n.info;
    });
    d.links.forEach((l) => {
      const ref = linkEls[l.id];
      ref.g.setAttribute("class", "topo-link " + l.state); ref.tr.textContent = l.text;
      const link = state.links.find((x) => x.id === l.id);
      link.state = l.state; link.traffic.text = l.text;
    });
  }

  async function poll() {
    try {
      const res = await fetch("/topology/api/state?site=" + encodeURIComponent(state.config.site || 0));
      if (!res.ok) return;
      const next = await res.json();
      const d = topoDiff(state, next);
      if (d.reload) { location.reload(); return; }
      applyDiff(d);
    } catch (err) { /* offline or logged out: try again next time */ }
  }

  render();
  const seconds = parseInt(state.config.interval, 10);
  if (seconds > 0) setInterval(poll, seconds * 1000);
})();
