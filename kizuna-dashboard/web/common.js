// Shared helpers for the two AGRIWISE dashboard pages (no framework, no build step).
"use strict";

const CFG = window.AGRIWISE_CONFIG || {};
const sameHost = port => `${location.protocol}//${location.hostname}:${port}`;

const AW = {
  // where the backend API and the photo analyzer are (server.py --backend / --vision, else same machine)
  backend: (CFG.backend || sameHost(8000)).replace(/\/$/, ""),
  vision: (CFG.vision || sameHost(8100)).replace(/\/$/, ""),
  imageUrl(ref) { return ref ? `${AW.vision}/images/${encodeURIComponent(ref)}` : null; },

  async api(path, opts = {}) {
    let r;
    try {
      r = await fetch(AW.backend + path, {
      headers: opts.body ? { "Content-Type": "application/json" } : {},
      ...opts,
      body: opts.body ? JSON.stringify(opts.body) : undefined,
      });
    } catch (e) {
      throw new Error(`le backend ne répond pas sur ${AW.backend}`);
    }
    if (!r.ok) {
      let msg = r.status + " " + r.statusText;
      try { const j = await r.json(); if (j.detail) msg = typeof j.detail === "string" ? j.detail : JSON.stringify(j.detail); } catch (e) { /* not json */ }
      throw new Error(msg);
    }
    return r.json();
  },

  esc(s) {
    return String(s ?? "").replace(/[&<>"']/g, c => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
  },

  num(v, d = 1) { return v === null || v === undefined || Number.isNaN(v) ? "—" : Number(v).toFixed(d).replace(".", ","); },

  date(iso, withTime = true) {
    if (!iso) return "—";
    const d = new Date(iso.length === 10 ? iso + "T12:00:00" : iso);
    return withTime
      ? d.toLocaleString("fr-FR", { day: "2-digit", month: "2-digit", hour: "2-digit", minute: "2-digit" })
      : d.toLocaleDateString("fr-FR", { day: "2-digit", month: "short", year: "numeric" });
  },

  ago(iso) {
    if (!iso) return "jamais";
    const s = (Date.now() - new Date(iso).getTime()) / 1000;
    if (s < 90) return `il y a ${Math.max(0, Math.round(s))} s`;
    if (s < 5400) return `il y a ${Math.round(s / 60)} min`;
    if (s < 172800) return `il y a ${Math.round(s / 3600)} h`;
    return `il y a ${Math.round(s / 86400)} j`;
  },

  KIND: { measured: "mesuré", calculated: "calculé", forecast: "prévision", ai: "IA", ai_hint: "indice IA",
          missing: "manquant", simulated: "simulé", confirmed: "confirmé", estimated: "estimé" },
  kind(k) { return k ? `<span class="kind ${AW.esc(k)}">${AW.KIND[k] || AW.esc(k)}</span>` : ""; },

  CERTAINTY: { confirmed: "confirmé par l'agriculteur", high: "élevée", medium: "moyenne", low: "faible" },

  toast(msg) {
    let t = document.querySelector(".toast");
    if (!t) { t = document.createElement("div"); t.className = "toast"; t.setAttribute("role", "status"); document.body.append(t); }
    t.textContent = msg; t.classList.add("show");
    clearTimeout(AW._tt); AW._tt = setTimeout(() => t.classList.remove("show"), 3200);
  },

  // top bar: ESP32 link, mode, tank, emergency stop (same on both pages)
  renderStatus(sys) {
    const el = document.getElementById("status");
    if (!el || !sys) return;
    const on = sys.controller_online;
    el.innerHTML = `
      <span><i class="dot ${on ? "on" : "off"}"></i>ESP32 ${on ? "en ligne" : "hors ligne"}
        <span class="muted small st-ago">(${AW.ago(sys.controller_last_seen)})</span></span>
      <span class="st-mode">Mode ${sys.mode === "auto" ? "automatique" : "manuel"}</span>
      ${sys.tank_level_pct != null ? `<span>Réservoir ${AW.num(sys.tank_level_pct, 0)} %</span>` : ""}
      <button class="estop" id="estop" aria-pressed="${sys.emergency_stop}">
        ${sys.emergency_stop ? "Réarmer l'irrigation" : "Arrêt d'urgence"}</button>`;
    document.getElementById("estop").onclick = async () => {
      const active = !sys.emergency_stop;
      if (active || confirm("Réarmer l'irrigation automatique ?")) {
        await AW.api("/api/system/emergency-stop", { method: "POST", body: { active } });
        AW.toast(active ? "Arrêt d'urgence activé : vannes fermées." : "Irrigation réarmée.");
        window.dispatchEvent(new Event("aw:refresh"));
      }
    };
  },
};
