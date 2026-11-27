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
