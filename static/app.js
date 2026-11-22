async function postJSON(url, body) {
  const res = await fetch(url, {
    method: "POST",
    headers: { "Content-Type": "application/json" },
    body: body ? JSON.stringify(body) : undefined,
  });
  return res.json();
}

function setPill(row, status) {
  const pill = row.querySelector(".pill");
  if (pill) {
    pill.textContent = status;
    pill.className = "pill " + status;
  }
}

// Shows UTC timestamps (data-ts) in the viewer's own time zone.
function showLocalTimes() {
  document.querySelectorAll("td.ts").forEach((el) => {
    const raw = el.dataset.ts;
    if (!raw) return;
    const d = new Date(raw);
    if (!isNaN(d)) el.textContent = d.toLocaleString([], { dateStyle: "short", timeStyle: "medium" });
  });
}

// Dashboard: every `interval` seconds ask whether any check has finished since the page
// was loaded; reload ONLY if so. No new check = no reload.
function watchForNewChecks(meta) {
  const seconds = parseInt(meta.dataset.interval, 10);
  if (!seconds || seconds < 1) return;
  let known = meta.dataset.stamp || "";
  setInterval(async () => {
    try {
      const res = await fetch("/api/stamp", { headers: { Accept: "application/json" } });
      if (!res.ok) return;
      const data = await res.json();
      if ((data.stamp || "") !== known) location.reload();
    } catch (e) {
      /* offline or logged out: try again next time */
    }
  }, seconds * 1000);
}

document.addEventListener("DOMContentLoaded", () => {
  showLocalTimes();
  const dashMeta = document.getElementById("dash-meta");
  if (dashMeta) watchForNewChecks(dashMeta);

  const checkAllBtn = document.getElementById("check-all-btn");
  if (checkAllBtn) {
    checkAllBtn.addEventListener("click", async () => {
      checkAllBtn.disabled = true;
      checkAllBtn.textContent = "Checking...";
      const data = await postJSON("/api/check-all");
      (data.results || []).forEach((r) => {
        const row = document.querySelector(`tr[data-device-id="${r.device_id}"]`);
        if (row) setPill(row, r.status);
      });
      checkAllBtn.disabled = false;
      checkAllBtn.textContent = "Check all devices";
      location.reload();
    });
  }

  document.querySelectorAll(".check-one").forEach((btn) => {
    btn.addEventListener("click", async () => {
      const id = btn.dataset.id;
      btn.disabled = true;
      const data = await postJSON(`/api/check/${id}`);
      const row = document.querySelector(`tr[data-device-id="${id}"]`);
      if (row && data.status) setPill(row, data.status);
      btn.disabled = false;
    });
  });

  const backupAllBtn = document.getElementById("backup-all-btn");
  if (backupAllBtn) {
    backupAllBtn.addEventListener("click", async () => {
      backupAllBtn.disabled = true;
      backupAllBtn.textContent = "Backing up...";
      await postJSON("/backups/api/run", { device_ids: [] });
      location.reload();
    });
  }

  const checkDeviceBtn = document.getElementById("check-device-btn");
  if (checkDeviceBtn) {
    checkDeviceBtn.addEventListener("click", async () => {
      checkDeviceBtn.disabled = true;
      checkDeviceBtn.textContent = "Checking...";
      await postJSON(`/api/check/${checkDeviceBtn.dataset.id}`);
      location.reload();
    });
  }

  const backupDeviceBtn = document.getElementById("backup-device-btn");
  if (backupDeviceBtn) {
    backupDeviceBtn.addEventListener("click", async () => {
      backupDeviceBtn.disabled = true;
      backupDeviceBtn.textContent = "Backing up...";
      await postJSON("/backups/api/run", { device_ids: [parseInt(backupDeviceBtn.dataset.id, 10)] });
      location.reload();
    });
  }

  const topoStage = document.getElementById("topoStage");
  const topoCanvas = document.getElementById("topoCanvas");
  if (topoStage && topoCanvas) {
    let scale = 1, panX = 0, panY = 0, dragging = false, startX = 0, startY = 0;
    const apply = () => {
      topoCanvas.style.transform = `translate(${panX}px, ${panY}px) scale(${scale})`;
    };
    topoStage.addEventListener("mousedown", (e) => {
      dragging = true;
      startX = e.clientX - panX;
      startY = e.clientY - panY;
    });
    window.addEventListener("mouseup", () => (dragging = false));
    window.addEventListener("mousemove", (e) => {
      if (!dragging) return;
      panX = e.clientX - startX;
      panY = e.clientY - startY;
      apply();
    });
    topoStage.addEventListener("wheel", (e) => {
      e.preventDefault();
      scale = Math.min(2, Math.max(0.3, scale - e.deltaY * 0.001));
      apply();
    });

    const resetView = () => {
      scale = 1;
      panX = 0;
      panY = 0;
      apply();
    };
    document.getElementById("topo-fit-btn")?.addEventListener("click", resetView);
    document.getElementById("topo-reset-btn")?.addEventListener("click", resetView);

    const scanBtn = document.getElementById("scan-topo-btn");
    if (scanBtn) {
      scanBtn.addEventListener("click", async () => {
        scanBtn.disabled = true;
        scanBtn.textContent = "Scanning...";
        const params = new URLSearchParams(location.search);
        const site = parseInt(params.get("site") || "0", 10) || null;
        await postJSON("/topology/api/scan", { site_id: site });
        location.reload();
      });
    }
  }

  const runTemplateBtn = document.getElementById("run-template-btn");
  if (runTemplateBtn) {
    runTemplateBtn.addEventListener("click", async () => {
      const templateId = parseInt(document.getElementById("template-select").value, 10);
      const deviceIds = Array.from(document.querySelectorAll(".device-check:checked")).map(
        (cb) => parseInt(cb.value, 10)
      );
      const output = document.getElementById("run-output");
      output.textContent = "Running...";
      const data = await postJSON("/commands/api/run", {
        template_id: templateId,
        device_ids: deviceIds,
      });
      if (data.error) {
        output.textContent = "Error: " + data.error;
        return;
      }
      const lines = Object.entries(data.outputs || {}).map(
        ([deviceId, out]) => `--- device #${deviceId} ---\n${out}`
      );
      output.textContent = lines.join("\n\n");
    });
  }
});
