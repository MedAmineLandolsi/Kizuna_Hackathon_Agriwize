// Irrigation page: live system map (components + flows), the irrigation cycle, camera vision, and the essentials.
"use strict";

const state = { data: null, zoneId: Number(new URLSearchParams(location.search).get("zone")) || null, hours: 72 };
const $ = id => document.getElementById(id);
const E = AW.esc;

const SENS_FLAG = { normal: "normal", low: "faible", high: "élevé", anomaly: "anomalie",
                    not_installed: "non installé", no_data: "pas de mesure" };
const BASIS_FR = { "calendar": "calendrier", "farmer confirmation": "confirmation de l'agriculteur",
  "degree-days of this season": "degrés-jours de la saison",
  "degree-days, normal temperatures assumed from today": "degrés-jours, températures normales supposées",
  "before the farmer's confirmation": "avant la confirmation" };
const basisFr = b => BASIS_FR[b] || b || "";
const ageMin = iso => iso ? (Date.now() - new Date(iso).getTime()) / 60000 : Infinity;

async function load() {
  try {
    state.data = await AW.api("/api/dashboard/overview");
  } catch (e) {
    $("hero").className = "hero tone-error";
    $("hero").innerHTML = `<div><p class="hero-title">Pas de connexion au backend.</p><p class="hero-line">${E(e.message)}. Démarrez-le : uvicorn app.main:app --port 8000</p></div>`;
    return;
  }
  const d = state.data;
  if (!d.zones.length) { $("zones").innerHTML = `<div class="empty">Aucune zone. Créez une zone dans /docs.</div>`; return; }
  if (!d.zones.find(z => z.id === state.zoneId)) state.zoneId = d.zones[0].id;
  const z = d.zones.find(x => x.id === state.zoneId);
  AW.renderStatus(d.system);
  renderZones(d);
  renderHero(z, d);
  renderAlerts(d);
  renderMap(z, d);
  renderProcedure(z);
  renderVision(z, d);
  renderPlant(z);
  renderIrrigation(z, d);
  renderSensors(z, d);
  renderDetails(z);
  loadChart();
}

function renderZones(d) {
  $("zones").innerHTML = d.zones.map(z => `<button data-id="${z.id}" aria-pressed="${z.id === state.zoneId}">
      <strong>${E(z.name)}</strong><span>${E(z.stage && z.stage.available ? z.stage.stage.name_fr : "stade inconnu")}</span></button>`).join("");
  $("zones").querySelectorAll("button").forEach(b => b.onclick = () => {
    state.zoneId = Number(b.dataset.id);
    history.replaceState(null, "", "?zone=" + state.zoneId);
    load();
  });
}

function renderAlerts(d) {
  const list = d.anomalies.filter(a => a.zone_id === null || a.zone_id === state.zoneId);
  $("alerts").innerHTML = list.map(a => `
    <div class="alert ${E(a.severity)} ${a.blocks ? "blocks" : ""}">
      <span title="${E(a.message)}">${a.blocks ? "<strong>Bloque l'irrigation.</strong> " : ""}${E(a.message_fr || a.message)} <span class="muted small">${AW.ago(a.ts)}</span></span>
      <button class="btn small-btn" data-ack="${a.id}">Vue</button></div>`).join("");
  $("alerts").querySelectorAll("[data-ack]").forEach(b => b.onclick = async () => {
    await AW.api(`/api/anomalies/${b.dataset.ack}/ack`, { method: "POST" }); load();
  });
}

// ---------------------------------------------------------------- status banner (one sentence: what is happening)
function renderHero(z, d) {
  const p = z.plan || {}, cmd = z.active_command, n = p.next_irrigation;
  const blocking = z.anomalies.find(a => a.blocks);
  const running = cmd && (cmd.status === "sent" || cmd.status === "running");
  let tone = "ok", title, line = "", action = "";
  if (d.system.emergency_stop) {
    tone = "error"; title = "Arrêt d'urgence : aucune irrigation possible.";
    line = "Les vannes sont fermées. Réarmez quand le problème est réglé.";
  } else if (!d.system.controller_online) {
    tone = "error"; title = "L'ESP32 ne répond plus.";
    line = `Dernier message ${AW.ago(d.system.controller_last_seen)}. Vérifier l'alimentation et le Wi-Fi du boîtier.`;
  } else if (running) {
    tone = "water"; title = `Irrigation en cours : ${AW.num(cmd.target_l, 0)} L.`;
    const f = z.flow.find(x => x.key === "flow");
    line = `Débit mesuré : ${f ? f.value : "—"}. Le contrôle de l'humidité suivra.`;
  } else if (blocking) {
    tone = "error"; title = "Irrigation automatique arrêtée pour cette zone.";
    line = blocking.message_fr || blocking.message;
  } else if (p.status === "insufficient_data") {
    tone = "warn"; title = "Pas assez de données pour décider.";
    line = "Manquant : " + (p.missing || []).join("; ");
  } else if (p.status === "postponed_rain") {
    tone = "rain"; title = "Irrigation reportée : de la pluie est prévue.";
    line = n ? `Si la pluie ne vient pas : ${n.when_fr}.` : "";
  } else if (p.status === "irrigation_needed") {
    tone = "water"; title = n ? `Irrigation prévue ${n.when_fr} : ${AW.num(n.volume_l, 0)} L.` : "La plante a besoin d'eau.";
    line = n ? `Environ ${AW.num(n.duration_min, 0)} min, lancée automatiquement.` : "";
  } else {
    title = "Tout va bien : la plante a assez d'eau.";
    line = n ? `Prochaine irrigation ${n.when_fr} (${AW.num(n.volume_l, 0)} L).`
             : p.frequency ? `Rythme estimé : ${p.frequency.text_fr}.` : "";
  }
  const st = z.stage;
  if (st && st.available && (st.needs_confirmation || (z.vision && z.vision.stage_status === "needs_confirmation"))) {
    action = `<a class="btn small-btn" href="camera.html">Confirmer le stade</a>`;
    line += (line ? " " : "") + "À faire : confirmer le stade de la culture.";
  }
  $("hero").className = "hero tone-" + tone;
  $("hero").innerHTML = `<svg class="enso" viewBox="0 0 48 48" aria-hidden="true"><path d="M30 6.5A18 18 0 1 1 12.5 10.5"/></svg><div><p class="hero-title">${E(title)}</p>${line ? `<p class="hero-line">${E(line)}</p>` : ""}</div>${action}`;
}

// ---------------------------------------------------------------- system map (HTML cards, stacks on a phone)
const ICON = {
  camera: '<path d="M4 8h3l2-2.5h6L17 8h3v11H4z"/><circle cx="12" cy="13.5" r="3.8"/>',
  meteo: '<circle cx="8" cy="8" r="3"/><path d="M8 2v1.5M2 8h1.5M3.8 3.8l1 1M12.2 3.8l-1 1"/><path d="M8 20h9a3.5 3.5 0 0 0 0-7 5 5 0 0 0-9.3 1.2A2.9 2.9 0 0 0 8 20z"/>',
  esp32: '<rect x="7" y="7" width="10" height="10" rx="1.5"/><path d="M9.5 3v4M14.5 3v4M9.5 17v4M14.5 17v4M3 9.5h4M3 14.5h4M17 9.5h4M17 14.5h4"/>',
  engine: '<path d="M12 3a7 7 0 0 0-4 12.7V18h8v-2.3A7 7 0 0 0 12 3z"/><path d="M9.5 21h5M10 10.5l2 2 2-2"/>',
  valve: '<path d="M2 14h4M18 14h4"/><path d="M6 9v10l12-10v10z"/><path d="M12 14V6M9 6h6"/>',
  flow: '<path d="M4 17a8 8 0 1 1 16 0"/><path d="M12 17l4.5-5"/><circle cx="12" cy="17" r="1.2"/>',
  field: '<path d="M12 21v-8"/><path d="M12 13c0-4 3-7 8-7 0 4-3 7-8 7zM12 15c0-3.5-2.5-6-7-6 0 3.5 2.5 6 7 6z"/><path d="M3 21h18"/>',
};
const icon = k => `<svg class="ic" viewBox="0 0 24 24" aria-hidden="true">${ICON[k]}</svg>`;

function renderMap(z, d) {
  const f = Object.fromEntries(z.flow.map(s => [s.key, s]));
  const v = z.vision, w = d.weather_today, cmd = z.active_command;
  const running = cmd && (cmd.status === "sent" || cmd.status === "running");
  const sensorTs = (z.sensors.find(s => s.key === "soil_moisture") || {}).ts;
  const recent = (iso, min) => ageMin(iso) <= min;
  const st = s => (s ? s.state : "idle");
  const node = (k, title, value, state, live, extra = "") => `
    <div class="sn n-${k} s-${E(state)}${live ? " live" : ""}">
      ${icon(k)}<div class="sn-txt"><span class="sn-title">${E(title)}</span><span class="sn-val">${E(value)}</span>${extra}</div>
      ${live ? '<i class="ping" aria-hidden="true"></i>' : ""}
    </div>`;
  const camVal = v ? ([v.crop_fr, v.stage_fr].filter(Boolean).join(" · ") || "image reçue") : "aucune image";
  const inputsLive = (v && recent(v.ts, 5)) || recent(sensorTs, 3);
  const decide = z.procedure.steps.find(x => x.key === "decide") || {};
  $("map").innerHTML = `<div class="sys">
    <div class="sys-col sys-in">
      ${node("camera", "Caméra", camVal, v ? (v.stage_status === "needs_confirmation" || !v.health_ok ? "warn" : "ok") : "idle",
             v && recent(v.ts, 5), v ? `<span class="sn-ts">${AW.ago(v.ts)}</span>` : "")}
      ${node("meteo", "Météo", w ? `ET0 ${AW.num(w.et0_mm, 1)} mm · pluie ${AW.num(w.precip_probability_pct, 0)} %` : "pas de prévision", w ? "ok" : "warn", false)}
      ${node("esp32", "Capteurs (ESP32)", f.sensors ? f.sensors.value : "—", st(f.sensors), recent(sensorTs, 3),
             sensorTs ? `<span class="sn-ts">${AW.ago(sensorTs)}</span>` : "")}
    </div>
    <div class="sys-link${inputsLive ? " on" : ""}"><span>mesures, images, météo</span><i></i></div>
    <div class="sys-col sys-core">
      <div class="sn core s-${E(running ? "active" : (decide.state || "idle"))}">
        ${icon("engine")}<div class="sn-txt"><span class="sn-title">Calcul AGRIWISE</span>
        <span class="sn-val">${E(running ? `Irrigation ${cmd.origin === "manual" ? "manuelle " : ""}en cours` : (decide.value || "pas encore calculé"))}</span>
        <span class="sn-ts">besoin en eau FAO-56 · ${z.decision ? AW.ago(z.decision.ts) : "—"}</span></div>
      </div>
    </div>
    <div class="sys-link${cmd ? " on" : ""}"><span>commande</span><i></i></div>
    <div class="sys-col sys-out">
      ${node("valve", "Électrovanne", f.valve ? f.valve.value : "—", st(f.valve), false)}
      <div class="wline${running ? " on" : ""}" aria-hidden="true"><i></i></div>
      ${node("flow", "Débitmètre", f.flow ? f.flow.value : "—", st(f.flow), running)}
      <div class="wline${running ? " on" : ""}" aria-hidden="true"><i></i></div>
      ${node("field", "Parcelle", z.crop ? z.crop.name.split(" (")[0] : "—", "ok", false,
             `<span class="sn-ts">${z.stage && z.stage.available ? E(z.stage.stage.name_fr) : ""}</span>`)}
    </div>
  </div>
  <p class="sys-loop muted small">↺ Les capteurs remesurent la parcelle : le volume apporté et la hausse d'humidité sont vérifiés.</p>`;
  $("map-note").textContent = z.procedure.note;
}

function renderProcedure(z) {
  const p = z.procedure;
  const cur = p.steps.findIndex(s => s.key === p.current);
  $("procedure").innerHTML = p.steps.map((s, i) => `
    <li data-state="${E(s.state)}" class="${i === cur ? "current" : ""}" ${i === cur ? 'aria-current="step"' : ""}>
      <span class="c-num">${i + 1}</span>
      <span class="c-title">${E(s.title)}${i === cur ? ' <em>maintenant</em>' : ""}</span>
      <span class="c-val" title="${E(s.value)}">${E(s.value)}</span>
      <span class="c-ts">${s.ts ? AW.ago(s.ts) : ""}</span>
    </li>`).join("");
}

// ---------------------------------------------------------------- camera vision widget
function renderVision(z, d) {
  const v = z.vision;
  if (!v) {
    $("vision").innerHTML = `<div class="photo small-photo">Aucune image</div>
      <p class="muted small" style="margin-top:8px">Démarrez la caméra de la zone :<br><code>python -m vision.capture --camera 0 --zone ${z.id} --every 30</code></p>`;
    return;
  }
  const url = AW.imageUrl(v.image_ref);
  $("vision").innerHTML = `
    <div class="photo small-photo">${url ? `<img src="${url}" alt="Dernière image de la zone" onerror="this.parentNode.textContent='Image indisponible (service de vision arrêté ?)'">` : "Pas d'image"}</div>
    <dl class="kv">
      ${v.trap_count != null ? `<div><dt>Piège</dt><dd>${v.trap_count} insectes</dd></div>` : `
      <div><dt>Culture</dt><dd>${E(v.crop_fr || "non identifiée")} ${v.crop_confidence != null ? `<span class="muted small">${Math.round(v.crop_confidence * 100)} %</span>` : ""} ${AW.kind("ai")}</dd></div>
      <div><dt>Stade vu</dt><dd>${E(v.stage_fr || "—")}${v.stage_status === "needs_confirmation" ? ' <a class="small" href="camera.html">à confirmer</a>' : ""}</dd></div>
      <div><dt>Santé</dt><dd class="${v.health_ok ? "" : "missing"}">${E(v.health_ok ? "rien de visible" : "à inspecter")} ${AW.kind(v.health_ok ? "ai_hint" : "ai_hint")}</dd></div>`}
      <div><dt>Image</dt><dd>${AW.ago(v.ts)} <span class="muted small">(${E({ phone: "téléphone", mast: "caméra fixe", carriage: "chariot", trap: "piège" }[v.source] || v.source)})</span></dd></div>
    </dl>`;
}

// ---------------------------------------------------------------- essentials
function renderPlant(z) {
  const s = z.stage;
  if (!s || !s.available) { $("plant").innerHTML = `<p class="muted">${E(s && s.reason ? s.reason : "Pas de culture ou de date de plantation.")}</p>`; return; }
  const idx = s.stage_index;
  const bars = s.timeline.map((t, i) => {
    const days = (i + 1 < s.timeline.length ? s.timeline[i + 1].start_cycle_day : s.cycle_length_days) - t.start_cycle_day;
    return `<div class="${i < idx ? "done" : i === idx ? "now" : ""}${t.visible ? "" : " hidden-stage"}" style="--d:${days}" title="${E(t.name_fr)}">${i === idx ? `<i style="width:${s.stage_progress_pct}%"></i>` : ""}</div>`;
  }).join("");
  const cert = s.certainty || {};
  $("plant").innerHTML = `
    <p class="muted small">${E(z.crop.name)} · jour ${s.effective_day}</p>
    <p class="mid">${E(s.stage.name_fr)}</p>
    <div class="cycle" aria-label="Progression du cycle : ${s.cycle_progress_pct} %">${bars}</div>
    <p class="small">Ensuite : <strong>${s.next_stage ? E(s.next_stage.name_fr) : "fin du cycle"}</strong>
      ${s.next_stage_date ? `vers le ${AW.date(s.next_stage_date, false)} <span class="muted">(dans ${s.days_to_next_stage} j)</span>` : ""}</p>
    <p class="small muted" style="margin-top:4px">Certitude ${E(AW.CERTAINTY[cert.level] || cert.level || "")}${(s.reasons_fr || []).length ? ` · <span class="missing">${E(s.reasons_fr[0])}</span>` : ""}</p>`;
}

function renderIrrigation(z, d) {
  const p = z.plan || {};
  const cmd = z.active_command;
  let h = `<span class="status-pill st-${E(p.status)}">${E(p.status_text_fr || p.status || "—")}</span>`;
  if (p.frequency) {
    const n = p.next_irrigation;
    h += `<p class="mid" style="margin-top:6px">${E(p.frequency.text_fr)} <span class="muted small">≈ ${AW.num(p.irrigations_per_week, 1)} / semaine</span></p>
      <dl class="kv">
        <div><dt>Prochaine</dt><dd>${n ? E(n.when_fr) : p.status === "irrigating" ? "après celle en cours" : "non déterminée"}</dd></div>
        <div><dt>Par irrigation</dt><dd>${AW.num(p.volume_per_irrigation_l, 0)} L · ${AW.num(p.duration_per_irrigation_min, 0)} min</dd></div>
        <div><dt>Sur 7 jours</dt><dd>${AW.num(p.weekly_water_l, 0)} L ${AW.kind("calculated")}</dd></div>
      </dl>`;
  }
  if ((p.missing || []).length) h += `<p class="small missing" style="margin-top:6px">Manquant : ${p.missing.map(E).join("; ")}</p>`;
  h += `<div class="actions">
      <button class="btn small-btn primary" id="decide">Recalculer</button>
      <button class="btn small-btn water" id="manual" ${d.system.emergency_stop ? "disabled" : ""}>Irriguer…</button>
      ${cmd ? `<button class="btn small-btn" id="stop">Arrêter</button>` : ""}</div>`;
  $("irrigation").innerHTML = h;
  $("decide").onclick = async () => { const r = await AW.api(`/api/zones/${z.id}/decide`, { method: "POST" }); AW.toast("Décision recalculée."); load(); };
  $("manual").onclick = async () => {
    const vol = prompt("Volume à apporter (litres) :", Math.round(p.volume_per_irrigation_l || 20));
    if (!vol) return;
    try { await AW.api(`/api/zones/${z.id}/manual-irrigation`, { method: "POST", body: { volume_l: Number(vol) } });
          AW.toast(`Irrigation manuelle de ${vol} L envoyée.`); load(); }
    catch (e) { AW.toast("Refusé : " + e.message); }
  };
  if (cmd) $("stop").onclick = async () => { await AW.api(`/api/commands/${cmd.id}/stop`, { method: "POST" }); AW.toast("Arrêt demandé."); load(); };
}

function sensorValue(s) {
  if (s.value === null || s.value === undefined) return "—";
  if (s.key === "soil_moisture") return AW.num(s.value * 100, 1) + " %";
  if (s.key === "rain") return s.value ? "pluie" : "sec";
  return AW.num(s.value, 1) + (s.unit ? " " + s.unit : "");
}

function renderSensors(z, d) {
  const live = z.sensors.filter(s => s.status !== "not_installed" && s.status !== "no_data");
  const absent = z.sensors.filter(s => s.status === "not_installed").map(s => s.name);
  const noData = z.sensors.filter(s => s.status === "no_data").map(s => s.name);
  $("sensors").innerHTML = `<ul class="plain srows">${live.map(s => `
      <li data-status="${E(s.status)}"><span>${E(s.name)}</span><b>${E(sensorValue(s))}</b>
        <span class="flag ${E(s.status)}" title="${E(s.note || "")}">${E(SENS_FLAG[s.status] || s.status)}</span></li>`).join("")}</ul>
    ${noData.length ? `<p class="small muted">Sans mesure : ${noData.map(E).join(", ")}</p>` : ""}
    ${absent.length ? `<p class="small muted">Non installés : ${absent.map(E).join(", ")}</p>` : ""}
    ${live.length ? `<p class="small muted">Mesuré ${AW.ago(live[0].ts)} par l'ESP32</p>` : ""}`;
  $("sensors-all").innerHTML = z.sensors.map(s => `<div class="sensor" data-status="${E(s.status)}">
      <span class="s-name">${E(s.name)}</span><b>${E(sensorValue(s))}</b>
      <span class="flag ${E(s.status)}">${E(SENS_FLAG[s.status] || s.status)}</span> ${AW.kind(s.kind)}
      <p class="muted small">${s.ts ? AW.ago(s.ts) : ""}${s.note ? (s.ts ? " · " : "") + E(s.note) : ""}</p></div>`).join("");
}

function renderDetails(z) {
  const s = z.stage, rows = z.plan_daily || [], names = {};
  ((s && s.timeline) || []).forEach(t => { names[t.key] = t.name_fr; });
  $("week").innerHTML = rows.length ? `<table><thead><tr><th>Jour</th><th>Stade</th><th class="num">ET0</th><th class="num">Kc</th>
      <th class="num">ETc</th><th class="num">Pluie utile</th><th class="num">Réserve utilisée / seuil</th><th>Irrigation</th></tr></thead><tbody>
    ${rows.map(r => `<tr><td>${AW.date(r.day, false)}${r.et0_kind !== "forecast" ? ' <span class="kind missing">ET0 supposée</span>' : ""}</td>
      <td>${E(names[r.stage] || r.stage)}</td><td class="num">${AW.num(r.et0_mm, 1)}</td><td class="num">${AW.num(r.kc, 2)}</td>
      <td class="num">${AW.num(r.etc_mm, 1)}</td><td class="num">${AW.num(r.effective_rain_mm, 1)}</td>
      <td class="num">${AW.num(r.depletion_end_mm, 1)} / ${AW.num(r.raw_mm, 1)} mm</td>
      <td>${r.irrigations.length ? r.irrigations.map(t => new Date(t).toLocaleTimeString("fr-FR", { hour: "2-digit", minute: "2-digit" })).join(", ") : (r.postponed_for_rain ? "reportée (pluie)" : "—")}</td></tr>`).join("")}
    </tbody></table>` : `<p class="muted">Pas de plan : ${(z.plan.missing || []).map(E).join("; ") || "données insuffisantes"}.</p>`;
  if (s && s.available) {
    const needs = (s.stage.needs || []).map(n => `<li>${E(n.text_fr)} <span class="muted small">[${E(n.source)}]</span></li>`).join("");
    $("stages").innerHTML = `<div class="two">
      <ol class="plain timeline">${s.timeline.map((t, i) => `<li class="${i < s.stage_index ? "past" : i === s.stage_index ? "current" : ""}">
        <span>${E(t.name_fr)}${t.visible ? "" : ' <span class="muted small">(souterrain)</span>'}</span>
        <span class="muted small">${t.estimated_start ? AW.date(t.estimated_start, false) : "—"}</span></li>`).join("")}</ol>
      <div><p>${E(s.stage.description_fr)}</p>${needs ? `<ul class="notes">${needs}</ul>` : ""}
        <p class="muted small" style="margin-top:8px">Début du stade : ${AW.date(s.stage_start_date, false)} (${E(basisFr(s.stage_start_basis))}).
        ${(s.certainty.reasons_fr || []).map(E).join(" ")}</p></div></div>`;
  } else $("stages").innerHTML = "";
  const p = z.plan || {}, dec = z.decision;
  $("calc").innerHTML = `${dec ? `<p><strong>Décision :</strong> ${E(dec.summary_fr || "")} <span class="muted small">(${AW.ago(dec.ts)})</span></p>` : ""}
    <ul class="notes">${z.flow.map(f => `<li><strong>${E(f.title)} :</strong> ${E(f.value)}${f.detail ? " — " + E(f.detail) : ""}</li>`).join("")}</ul>
    ${p.inputs ? `<p class="muted small" style="margin-top:8px">Efficacité ${AW.num(p.inputs.efficiency * 100, 0)} %, débit ${AW.num(p.inputs.flow_rate_lpm, 0)} L/min,
      ${AW.num(p.inputs.area_m2, 0)} m², fenêtres ${E(p.inputs.irrigation_windows.join(", "))}. ${(p.assumptions || []).map(E).join(" ")}</p>` : ""}`;
}

// ---------------------------------------------------------------- chart (plain SVG)
async function loadChart() {
  let h;
  try { h = await AW.api(`/api/dashboard/zones/${state.zoneId}/history?hours=${state.hours}`); }
  catch (e) { $("chart").innerHTML = `<div class="empty">${E(e.message)}</div>`; return; }
  const zone = state.data.zones.find(z => z.id === state.zoneId);
  if (!h.soil.length && !h.irrigations.length) {
    $("chart").innerHTML = `<div class="empty">Aucune mesure sur cette période : démarrez l'ESP32 (ou python tools/esp32_simulator.py).</div>`;
    return;
  }
  const W = 960, H = 210, L = 46, R = 40, T = 10, B = 26;
  const t0 = new Date(h.from).getTime(), t1 = new Date(h.to).getTime();
  const x = t => L + (new Date(t).getTime() - t0) / (t1 - t0) * (W - L - R);
  const thMax = Math.max(h.theta_fc * 1.15, ...h.soil.map(s => s.theta)) * 100;
  const y = v => T + (1 - v * 100 / thMax) * (H - T - B);
  const area = zone.area_m2 || 10;
  const mm = h.irrigations.map(i => (i.delivered_l ?? i.target_l) / area);
  const etcs = h.needs.filter(n => n.etc_mm != null).map(n => n.etc_mm);
  const mmMax = Math.max(4, ...mm, ...etcs) * 1.15;
  const y2 = v => T + (1 - v / mmMax) * (H - T - B);
  let g = "";
  for (let k = 0; k <= 2; k++) {
    const yy = T + (1 - k / 2) * (H - T - B);
    g += `<line x1="${L}" x2="${W - R}" y1="${yy}" y2="${yy}" stroke="#e3e8e2"/>
      <text x="${L - 6}" y="${yy + 4}" text-anchor="end" font-size="11" fill="#4a5a52">${(thMax * k / 2).toFixed(0)} %</text>
      <text x="${W - R + 6}" y="${yy + 4}" font-size="11" fill="var(--ayame)">${(mmMax * k / 2).toFixed(1)}</text>`;
  }
  const days = Math.ceil((t1 - t0) / 86400000);
  for (let k = 0; k <= days; k++) {
    const t = new Date(t0 + k * 86400000); t.setHours(0, 0, 0, 0);
    if (t.getTime() < t0) continue;
    const xx = x(t);
    g += `<line x1="${xx}" x2="${xx}" y1="${T}" y2="${H - B}" stroke="#eef1ed"/>
      <text x="${xx + 3}" y="${H - B + 15}" font-size="11" fill="#4a5a52">${t.toLocaleDateString("fr-FR", { day: "2-digit", month: "2-digit" })}</text>`;
  }
  h.irrigations.forEach((i, k) => {
    const xx = x(i.ts), v = mm[k];
    g += `<rect x="${xx - 5}" y="${y2(v)}" width="10" height="${H - B - y2(v)}" fill="var(--sky)"><title>${AW.date(i.ts)} : ${AW.num(i.delivered_l ?? i.target_l, 0)} L (${E(i.origin)})</title></rect>`;
  });
  g += `<line x1="${L}" x2="${W - R}" y1="${y(h.theta_fc)}" y2="${y(h.theta_fc)}" stroke="var(--leaf)" stroke-width="2" stroke-dasharray="6 4"/>`;
  const thr = h.needs.filter(n => n.raw_mm != null && n.root_depth_m).map(n => [x(n.ts), y(h.theta_fc - n.raw_mm / (1000 * n.root_depth_m))]);
  if (thr.length) g += `<polyline fill="none" stroke="var(--yamabuki)" stroke-width="2.5" stroke-dasharray="4 3" points="${thr.map(p => p.join(",")).join(" ")}"/>`;
  const etc = h.needs.filter(n => n.etc_mm != null).map(n => [x(n.ts), y2(n.etc_mm)]);
  if (etc.length) g += `<polyline fill="none" stroke="var(--ayame)" stroke-width="2" points="${etc.map(p => p.join(",")).join(" ")}"/>`;
  const pts = h.soil.map(s => [x(s.ts), y(s.theta)]);
  if (pts.length) g += `<polyline fill="none" stroke="var(--water)" stroke-width="2.5" points="${pts.map(p => p.join(",")).join(" ")}"/>`;
  h.soil.filter(s => !s.valid).forEach(s => { g += `<circle cx="${x(s.ts)}" cy="${y(s.theta)}" r="4" fill="var(--alarm)"><title>Mesure invalide</title></circle>`; });
  const names = {};
  ((zone.stage && zone.stage.timeline) || []).forEach(t => { names[t.key] = t.name_fr; });
  const SRC = { ai: "photo", farmer: "agriculteur", system: "calendrier" };
  h.stage_changes.forEach((s, k) => {
    const xx = x(s.ts), right = xx > W - R - 170;
    g += `<line x1="${xx}" x2="${xx}" y1="${T}" y2="${H - B}" stroke="var(--leaf)" stroke-dasharray="2 4"/>
      <text x="${right ? xx - 4 : xx + 4}" y="${T + 12 + (k % 3) * 13}" text-anchor="${right ? "end" : "start"}" font-size="11" fill="var(--leaf)">${E(names[s.stage] || s.stage)} (${E(SRC[s.source] || s.source)})</text>`;
  });
  $("chart").innerHTML = `<svg class="chart" viewBox="0 0 ${W} ${H}" role="img" aria-label="Humidité du sol, seuil, ETc et irrigations sur ${state.hours} heures">${g}</svg>`;
}

document.querySelectorAll(".seg button").forEach(b => b.onclick = () => {
  document.querySelectorAll(".seg button").forEach(x => x.setAttribute("aria-pressed", x === b));
  state.hours = Number(b.dataset.h); loadChart();
});
window.addEventListener("aw:refresh", load);

load();
setInterval(() => { if (!document.hidden) load(); }, 10000);
