// Farm map: the water network and every plot, live. Pan, zoom, rearrange; plots with a problem carry a badge.
"use strict";

const $ = id => document.getElementById(id);
const E = AW.esc;
const NS = "http://www.w3.org/2000/svg";

const P = {
  data: null, layout: null, saved: null, selected: null, edit: false,
  view: { x: 0, y: 0, k: 1 }, fitted: false, show: { water: true, data: true },
};
const focusParam = Number(new URLSearchParams(location.search).get("focus")) || null;

// sizes in map units
const SZ = { zone: { w: 290, h: 176 }, tank: { w: 130, h: 160 }, esp32: { w: 190, h: 64 } };

// ---------------------------------------------------------------- problems of a plot, in plain words
const KIND = {
  sensor: ["Capteur", "Vérifier le capteur d'humidité (câble, position dans le sol)."],
  low_delivery: ["Eau insuffisante", "Vérifier le tuyau, la vanne manuelle, la pompe et la pression."],
  over_delivery: ["Trop d'eau", "Vérifier que l'électrovanne se ferme et que le débitmètre fonctionne."],
  moisture_response: ["Le sol ne s'humidifie pas", "Chercher une fuite après le débitmètre ou un goutteur bouché près du capteur."],
  identification: ["Culture à confirmer", "Ouvrir la page Caméra et confirmer ou rejeter la photo."],
  pest: ["Ravageur possible", "Inspecter les feuilles de la parcelle."],
  animal: ["Animal dans la parcelle", "Aller vérifier la parcelle."],
  motion: ["Mouvement détecté", "Aller vérifier la parcelle."],
  insect_trap: ["Beaucoup d'insectes", "Inspecter les plantes et changer le piège."],
};
const NETWORK_KINDS = ["leak", "rain_forecast"];     // shown on the network, not on a plot
const SEV_ORDER = { error: 3, warn: 2, info: 1 };
const CROP_FR = { tomato: "Tomate", pepper: "Poivron", potato: "Pomme de terre", eggplant: "Aubergine" };
const cropName = z => z.crop ? (CROP_FR[z.crop.key] || z.crop.name.split(" (")[0]) : "culture ?";

function zoneIssues(z, d) {
  const out = [];
  const add = (sev, title, text, todo) => { if (!out.find(i => i.title === title)) out.push({ sev, title, text, todo }); };
  if (d.system.emergency_stop) add("error", "Arrêt d'urgence", "Toutes les vannes sont fermées.", "Réarmer depuis le bouton en haut quand c'est réglé.");
  if (!d.system.controller_online) add("error", "ESP32 hors ligne", `Dernier message ${AW.ago(d.system.controller_last_seen)}.`, "Vérifier l'alimentation et le Wi-Fi du boîtier.");
  for (const a of z.anomalies) {
    const kind = a.kind.split(":")[0];
    if (NETWORK_KINDS.includes(kind)) continue;
    const [title, todo] = KIND[kind] || ["Alerte", ""];
    const sev = a.blocks || a.severity === "critical" || kind === "sensor" ? "error" : a.severity === "info" ? "info" : "warn";
    add(sev, title, a.message_fr || a.message, todo);
  }
  const soil = z.sensors.find(s => s.key === "soil_moisture");
  if (soil && soil.status === "anomaly") add("error", "Capteur", soil.note || "Mesure d'humidité invalide.", KIND.sensor[1]);
  if (soil && soil.status === "no_data" && d.system.controller_online && !out.find(i => i.title === "Capteur")) add("warn", "Pas de mesure du sol", "L'ESP32 n'envoie pas l'humidité de cette parcelle.", "Vérifier le branchement du capteur.");
  const p = z.plan || {};
  if (p.status === "insufficient_data") add("warn", "Données insuffisantes", (p.missing || []).join("; "), "");
  const st = z.stage;
  if (st && st.available && st.field_check_due) add("warn", "Vérification au champ", (st.reasons_fr || [])[0] || "", "Déterrer un plant pour vérifier le stade.");
  else if ((st && st.available && st.needs_confirmation) || (z.vision && z.vision.stage_status === "needs_confirmation"))
    add("warn", "Stade à confirmer", (st && (st.reasons_fr || [])[0]) || "La photo et le calendrier ne concordent pas.", "Ouvrir la page Caméra et confirmer le stade.");
  if (z.vision && !z.vision.health_ok) add("warn", "Santé à inspecter", z.vision.health_fr || "", "Regarder les feuilles de près.");
  return out.sort((a, b) => SEV_ORDER[b.sev] - SEV_ORDER[a.sev]);
}

function zoneState(z, issues) {
  if (issues.some(i => i.sev === "error")) return "error";
  const c = z.active_command;
  if (c && (c.status === "sent" || c.status === "running")) return "active";
  if (issues.some(i => i.sev === "warn")) return "warn";
  return "ok";
}
const STATE_FR = { ok: "Tout va bien", active: "Irrigation en cours", warn: "À surveiller", error: "Problème" };

// ---------------------------------------------------------------- loading
async function load() {
  try {
    const [d, lay] = await Promise.all([AW.api("/api/dashboard/overview"), P.layout ? null : AW.api("/api/dashboard/layout")]);
    P.data = d;
    if (lay) {
      P.layout = lay.items;
      if (!lay.saved && narrow()) P.layout = verticalLayout(d.zones);
      P.saved = JSON.parse(JSON.stringify(P.layout));
    }
  } catch (e) {
    $("counts").innerHTML = `<div class="hero tone-error" style="margin:0"><div><p class="hero-title">Pas de connexion au backend.</p>
      <p class="hero-line">${E(e.message)}. Démarrez-le : uvicorn app.main:app --port 8000</p></div></div>`;
    return;
  }
  AW.renderStatus(P.data.system);
  for (const z of P.data.zones) {
    z._issues = zoneIssues(z, P.data);
    z._state = zoneState(z, z._issues);
    if (!P.layout[`zone:${z.id}`]) P.layout[`zone:${z.id}`] = { x: 420, y: 30 + z.id * 40 };
  }
  renderSummary();
  renderZoneList();
  draw();
  if (!P.fitted) {
    fit();
    P.fitted = true;
    if (focusParam && P.data.zones.find(z => z.id === focusParam)) { select(focusParam); focusOn(focusParam); }
  }
  if (P.selected) renderDrawer();
}

function narrow() { return window.innerWidth < 700; }
function verticalLayout(zones) {
  // phone: tank and controller on top, plots stacked in one column (until a layout is saved)
  const items = { tank: { x: 0, y: 0 }, esp32: { x: 0, y: 190 } };
  zones.forEach((z, i) => { items[`zone:${z.id}`] = { x: 290, y: 30 + i * 230 }; });
  return items;
}

// ---------------------------------------------------------------- summary above the map
function renderSummary() {
  const zs = P.data.zones;
  const n = s => zs.filter(z => z._state === s).length;
  $("counts").innerHTML = `
    <span class="cnt"><b>${zs.length}</b> parcelle${zs.length > 1 ? "s" : ""}</span>
    <span class="cnt c-active"><i></i><b>${n("active")}</b> en irrigation</span>
    <span class="cnt c-ok"><i></i><b>${n("ok")}</b> tout va bien</span>
    <span class="cnt c-warn"><i></i><b>${n("warn")}</b> à surveiller</span>
    <span class="cnt c-error"><i></i><b>${n("error")}</b> problème${n("error") > 1 ? "s" : ""}</span>`;
  const net = networkIssues();
  const items = [];
  net.forEach(i => items.push({ ...i, zone: null }));
  zs.forEach(z => z._issues.filter(i => i.sev !== "info" && !["Arrêt d'urgence", "ESP32 hors ligne"].includes(i.title))
    .forEach(i => items.push({ ...i, zone: z })));
  const glob = zs.length ? zs[0]._issues.filter(i => ["Arrêt d'urgence", "ESP32 hors ligne"].includes(i.title)) : [];
  glob.forEach(i => items.unshift({ ...i, zone: null }));
  $("issues").innerHTML = items.length ? items.slice(0, 6).map(i => `
    <li><button class="issue sev-${i.sev}" ${i.zone ? `data-zone="${i.zone.id}"` : ""}>
      <i aria-hidden="true">${i.sev === "error" ? "!" : "•"}</i><span><b>${E(i.zone ? i.zone.name : "Réseau")}</b> — ${E(i.title)}</span></button></li>`).join("")
    + (items.length > 6 ? `<li class="muted small">+ ${items.length - 6} autre(s)</li>` : "")
    : `<li class="muted small">Aucun problème : toutes les parcelles sont suivies normalement.</li>`;
  $("issues").querySelectorAll("[data-zone]").forEach(b => b.onclick = () => { const id = Number(b.dataset.zone); select(id); focusOn(id); });
}

function networkIssues() {
  const out = [];
  const seen = new Set();
  for (const z of P.data.zones) for (const a of z.anomalies) {
    const kind = a.kind.split(":")[0];
    if (!NETWORK_KINDS.includes(kind) || seen.has(a.id)) continue;
    seen.add(a.id);
    out.push(kind === "leak"
      ? { sev: "error", title: "Fuite possible", text: a.message_fr, todo: "Vérifier les tuyaux et que toutes les vannes sont fermées." }
      : { sev: "info", title: "Pluie prévue", text: a.message_fr, todo: "" });
  }
  return out;
}

function renderZoneList() {
  $("zone-list").innerHTML = P.data.zones.map(z => `
    <li><button class="zl s-${z._state}" data-zone="${z.id}" aria-pressed="${P.selected === z.id}">
      <i aria-hidden="true"></i><span>${E(z.name)}</span>${z._issues.filter(i => i.sev !== "info").length ? `<b>${z._issues.filter(i => i.sev !== "info").length}</b>` : ""}</button></li>`).join("");
  $("zone-list").querySelectorAll("[data-zone]").forEach(b => b.onclick = () => { const id = Number(b.dataset.zone); select(id); focusOn(id); });
}

// ---------------------------------------------------------------- drawing
function el(tag, attrs = {}, parent) {
  const n = document.createElementNS(NS, tag);
  for (const [k, v] of Object.entries(attrs)) if (v !== undefined && v !== null) n.setAttribute(k, v);
  if (parent) parent.appendChild(n);
  return n;
}
function txt(parent, x, y, s, cls, attrs = {}) { const t = el("text", { x, y, class: cls, ...attrs }, parent); t.textContent = s; return t; }

const ICON = {
  camera: "M4 8h3l2-2.5h6L17 8h3v11H4z M12 17.3a3.8 3.8 0 1 0 0-7.6 3.8 3.8 0 0 0 0 7.6z",
  sensor: "M12 3v10 M8.5 13a3.5 3.5 0 1 0 7 0 M7 7h3 M7 10h3",
  valve: "M2 14h4 M18 14h4 M6 9v10l12-10v10z M12 14V6 M9 6h6",
};
function icon(parent, name, x, y, size, cls) {
  const g = el("g", { transform: `translate(${x},${y}) scale(${size / 24})`, class: "pi " + (cls || "") }, parent);
  el("path", { d: ICON[name] }, g);
  return g;
}

function pos(key) { return P.layout[key] || { x: 0, y: 0 }; }

function draw() {
  const svg = $("plan");
  const d = P.data;
  svg.innerHTML = "";
  const defs = el("defs", {}, svg);
  const pat = el("pattern", { id: "rows", width: 18, height: 18, patternUnits: "userSpaceOnUse" }, defs);
  el("circle", { cx: 9, cy: 9, r: 3.2, class: "plant-dot" }, pat);
  const world = el("g", { id: "world" }, svg);
  applyView();

  const tank = pos("tank"), esp = pos("esp32");
  const tw = SZ.tank.w, th = SZ.tank.h;
  const outY = tank.y + th - 30, outX = tank.x + tw;
  const fmX = outX + 56;                      // flow meter on the main line
  const trunkX = outX + 120;                  // the manifold every plot branches from
  const running = z => z.active_command && (z.active_command.status === "sent" || z.active_command.status === "running");
  const anyRunning = d.zones.some(running);
  const leak = networkIssues().find(i => i.title === "Fuite possible");
  const layerWater = el("g", { class: "layer-water" + (P.show.water ? "" : " off") }, world);
  const layerData = el("g", { class: "layer-data" + (P.show.data ? "" : " off") }, world);
  const layerItems = el("g", {}, world);

  // ---- water network: one path per plot (tank -> flow meter -> manifold -> valve)
  for (const z of d.zones) {
    const zp = pos(`zone:${z.id}`);
    const vx = zp.x - 26, vy = zp.y + SZ.zone.h / 2;
    const laneY = zp.y - 22;                     // lateral running above the plot's row, then down to its valve
    const path = vx > trunkX + 10 ? `M${outX},${outY} H${trunkX} V${laneY} H${vx} V${vy}` : `M${outX},${outY} H${trunkX} V${vy} H${vx}`;
    el("path", { d: path, class: "pipe" }, layerWater);
    if (running(z)) {
      el("path", { d: path, class: "pipe-flow" }, layerWater);
    }
    // valve on the plot's edge
    const v = el("g", { class: "valve " + (running(z) ? "open" : "closed") }, layerWater);
    el("circle", { cx: vx + 12, cy: vy, r: 14 }, v);
    icon(v, "valve", vx + 1, vy - 11, 22);
    txt(layerWater, vx + 12, vy + 28, running(z) ? "ouverte" : "fermée", "pl-mini", { "text-anchor": "middle" });
  }
  // flow meter
  const fm = el("g", { class: "flowmeter" + (leak ? " leak" : anyRunning ? " on" : "") }, layerWater);
  el("circle", { cx: fmX, cy: outY, r: 20 }, fm);
  el("path", { d: `M${fmX - 11},${outY + 6} A12 12 0 1 1 ${fmX + 11},${outY + 6} M${fmX},${outY + 4} L${fmX + 7},${outY - 6}`, class: "fm-needle" }, fm);
  const flow = d.system && P.data.zones.length ? (P.data.zones[0].sensors.find(s => s.key === "flow") || {}).value : null;
  txt(layerWater, fmX, outY + 40, "Débitmètre", "pl-mini", { "text-anchor": "middle" });
  txt(layerWater, fmX, outY + 56, flow != null ? AW.num(flow, 1) + " L/min" : "—", "pl-label", { "text-anchor": "middle" });
  if (leak) txt(layerWater, fmX, outY + 74, "Fuite possible !", "pl-alarm", { "text-anchor": "middle" });

  // ---- data: ESP32 <-> sensors (readings) and ESP32 -> valves (commands)
  const ew = SZ.esp32.w, eh = SZ.esp32.h;
  const ex = esp.x + ew, ey = esp.y + eh / 2;
  const online = d.system.controller_online;
  for (const z of d.zones) {
    const zp = pos(`zone:${z.id}`);
    const sx = zp.x + 24, sy = zp.y + SZ.zone.h - 22;
    const soil = z.sensors.find(s => s.key === "soil_moisture") || {};
    const fresh = soil.ts && (Date.now() - new Date(soil.ts).getTime()) < 3 * 60000;
    const pth = `M${ex},${ey} C${ex + 120},${ey} ${sx - 140},${sy + 50} ${sx},${sy}`;
    const id = `d${z.id}`;
    el("path", { id, d: pth, class: "dataline" + (online && soil.status !== "no_data" ? "" : " dead") }, layerData);
    if (fresh && online) packet(layerData, id, "pk-data", 2.4);
    const c = z.active_command;
    if (c && ["pending", "sent", "running"].includes(c.status)) {
      const vx = zp.x - 14, vy = zp.y + SZ.zone.h / 2 - 14;
      const cid = `c${z.id}`;
      el("path", { id: cid, d: `M${ex},${ey - 14} C${ex + 160},${ey - 14} ${vx - 120},${vy - 60} ${vx},${vy}`, class: "cmdline" }, layerData);
      packet(layerData, cid, "pk-cmd", 1.8);
    }
  }

  // ---- tank
  const tg = el("g", { class: "item tank", "data-key": "tank" }, layerItems);
  const level = d.system.tank_level_pct;
  el("rect", { x: tank.x, y: tank.y, width: tw, height: th, rx: 18, class: "tank-body" }, tg);
  if (level != null) {
    const lh = (th - 16) * Math.max(0, Math.min(level, 100)) / 100;
    el("rect", { x: tank.x + 8, y: tank.y + th - 8 - lh, width: tw - 16, height: lh, rx: 10, class: "tank-water" + (level < 20 ? " low" : "") }, tg);
  }
  txt(tg, tank.x + tw / 2, tank.y + 30, "Réservoir", "pl-title", { "text-anchor": "middle" });
  txt(tg, tank.x + tw / 2, tank.y + 56, level != null ? `${AW.num(level, 0)} %` : "niveau inconnu", "pl-big", { "text-anchor": "middle" });

  // ---- ESP32 controller
  const eg = el("g", { class: "item esp32 " + (online ? "on" : "off"), "data-key": "esp32" }, layerItems);
  el("rect", { x: esp.x, y: esp.y, width: ew, height: eh, rx: 12, class: "esp-body" }, eg);
  txt(eg, esp.x + 16, esp.y + 26, "ESP32 (boîtier)", "pl-title light");
  txt(eg, esp.x + 16, esp.y + 47, online ? `en ligne · ${AW.ago(d.system.controller_last_seen)}` : "hors ligne", "pl-sub light");
  el("circle", { cx: esp.x + ew - 18, cy: esp.y + 20, r: 6, class: "esp-dot" }, eg);

  // ---- plots
  for (const z of d.zones) drawZone(layerItems, z);
}

function packet(parent, pathId, cls, dur) {
  if (window.matchMedia("(prefers-reduced-motion: reduce)").matches) return;
  const c = el("circle", { r: 5, class: cls }, parent);
  const m = el("animateMotion", { dur: `${dur}s`, repeatCount: "indefinite" }, c);
  el("mpath", { href: `#${pathId}` }, m);
}

function drawZone(parent, z) {
  const p = pos(`zone:${z.id}`);
  const { w, h } = SZ.zone;
  const g = el("g", { class: `item zone s-${z._state}` + (P.selected === z.id ? " sel" : ""), "data-key": `zone:${z.id}`,
                      "data-zone": z.id, tabindex: 0, role: "button",
                      "aria-label": `${z.name} : ${STATE_FR[z._state]}${z._issues.length ? ", " + z._issues.length + " point(s)" : ""}` }, parent);
  el("rect", { x: p.x, y: p.y, width: w, height: h, rx: 16, class: "zone-body" }, g);
  el("rect", { x: p.x + 8, y: p.y + h - 44, width: w - 16, height: 36, rx: 8, fill: "url(#rows)", class: "zone-rows" }, g);
  // title
  txt(g, p.x + 16, p.y + 28, z.name.replace(/^Zone (\d+) - /, "Zone $1 · "), "pl-title");
  const st = z.stage && z.stage.available ? z.stage.stage.name_fr : "stade inconnu";
  txt(g, p.x + 16, p.y + 48, `${cropName(z)} — ${trim(st, 26)}`, "pl-sub");
  // soil moisture bar with the irrigation threshold
  const soil = z.sensors.find(s => s.key === "soil_moisture") || {};
  const fc = z.soil.theta_fc, wp = z.soil.theta_wp;
  const bx = p.x + 16, by = p.y + 62, bw = w - 32;
  el("rect", { x: bx, y: by, width: bw, height: 10, rx: 5, class: "bar-bg" }, g);
  if (soil.value != null) {
    const frac = Math.max(0, Math.min((soil.value - wp) / (fc - wp), 1.1)) / 1.1;
    el("rect", { x: bx, y: by, width: Math.max(6, bw * frac), height: 10, rx: 5, class: "bar-soil s-" + soil.status }, g);
  }
  const dv = z.decision && z.decision.values || {};
  const raw = dv.raw && dv.raw.value, zr = dv.root_depth && dv.root_depth.value;
  if (raw && zr) {
    const thr = fc - raw / (1000 * zr);
    const tx = bx + bw * Math.max(0, Math.min((thr - wp) / (fc - wp), 1.1)) / 1.1;
    el("line", { x1: tx, x2: tx, y1: by - 4, y2: by + 14, class: "bar-thr" }, g);
  }
  txt(g, bx, by + 28, `Sol ${soil.value != null ? AW.num(soil.value * 100, 1) + " %" : "—"}`, "pl-sub strong");
  // status line
  const plan = z.plan || {}, c = z.active_command, n = plan.next_irrigation;
  let line;
  if (c && (c.status === "sent" || c.status === "running")) line = `Irrigation en cours · ${AW.num(c.target_l, 0)} L`;
  else if (n) line = `Prochaine : ${n.when_fr}`;
  else if (plan.frequency) line = `Rythme : ${plan.frequency.text_fr}`;
  else line = "Plan non calculé";
  txt(g, bx + 92, by + 28, trim(line, 30), "pl-sub");
  // camera & sensor icons
  icon(g, "sensor", p.x + 12, p.y + h - 34, 22, soil.status === "anomaly" ? "bad" : soil.ts ? "good" : "dim");
  if (z.vision) {
    const recentCam = (Date.now() - new Date(z.vision.ts).getTime()) < 24 * 3600 * 1000;
    icon(g, "camera", p.x + w - 40, p.y + h - 36, 24, z.vision.health_ok && z.vision.stage_status !== "needs_confirmation" ? (recentCam ? "good" : "dim") : "warn");
    txt(g, p.x + w - 46, p.y + h - 18, AW.ago(z.vision.ts).replace("il y a ", ""), "pl-mini", { "text-anchor": "end" });
  }
  // problem badge
  const count = z._issues.filter(i => i.sev !== "info").length;
  if (count) {
    const b = el("g", { class: "badge b-" + (z._state === "error" ? "error" : "warn") }, g);
    el("circle", { cx: p.x + w - 4, cy: p.y + 4, r: 15 }, b);
    txt(b, p.x + w - 4, p.y + 9, String(count), "badge-n", { "text-anchor": "middle" });
  }
}
function trim(s, n) { s = String(s || ""); return s.length > n ? s.slice(0, n - 1) + "…" : s; }

// ---------------------------------------------------------------- view: pan, zoom, fit
function applyView() {
  const w = $("world");
  if (w) w.setAttribute("transform", `translate(${P.view.x},${P.view.y}) scale(${P.view.k})`);
}
function bounds() {
  const keys = Object.keys(P.layout);
  let x0 = Infinity, y0 = Infinity, x1 = -Infinity, y1 = -Infinity;
  for (const k of keys) {
    const p = P.layout[k], s = k.startsWith("zone:") ? SZ.zone : SZ[k] || SZ.zone;
    x0 = Math.min(x0, p.x - 60); y0 = Math.min(y0, p.y - 50); x1 = Math.max(x1, p.x + s.w + 30); y1 = Math.max(y1, p.y + s.h + 40);
  }
  return { x0, y0, x1, y1 };
}
function fit() {
  const r = $("plan").getBoundingClientRect(), b = bounds();
  const left = narrow() ? 0 : 230, top = narrow() ? 96 : 40;     // floating tools
  const W = r.width - left, H = r.height - top - 20;
  const k = Math.min(W / (b.x1 - b.x0), H / (b.y1 - b.y0), 1.6);
  P.view = { k, x: left + (W - (b.x1 - b.x0) * k) / 2 - b.x0 * k, y: top + (H - (b.y1 - b.y0) * k) / 2 - b.y0 * k };
  applyView();
}
function focusOn(id) {
  const p = pos(`zone:${id}`), r = $("plan").getBoundingClientRect();
  const k = Math.max(P.view.k, Math.min(1.3, r.width / (SZ.zone.w * (r.width < 700 ? 1.25 : 2.2))));
  const nar = r.width < 700;
  const cx = nar ? r.width / 2 : (r.width - 380) / 2;              // leave room for the drawer
  const cy = nar ? 104 + SZ.zone.h * k / 2 : r.height / 2;          // phone: just under the floating tools
  P.view = { k, x: cx - (p.x + SZ.zone.w / 2) * k, y: cy - (p.y + SZ.zone.h / 2) * k };
  const w = $("world");
  if (w && !window.matchMedia("(prefers-reduced-motion: reduce)").matches) w.style.transition = "transform .45s ease";
  applyView();
  setTimeout(() => { if (w) w.style.transition = ""; }, 500);
}
function zoomAt(f, cx, cy) {
  const k = Math.max(0.25, Math.min(P.view.k * f, 3));
  const ratio = k / P.view.k;
  P.view = { k, x: cx - (cx - P.view.x) * ratio, y: cy - (cy - P.view.y) * ratio };
  applyView();
}
function toWorld(cx, cy) { return { x: (cx - P.view.x) / P.view.k, y: (cy - P.view.y) / P.view.k }; }

// pointers: one finger/mouse pans (or drags an item in edit mode), two fingers pinch-zoom
const ptr = new Map();
let gesture = null;
function setupPointers() {
  const svg = $("plan");
  svg.addEventListener("pointerdown", e => {
    svg.setPointerCapture(e.pointerId);
    ptr.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const r = svg.getBoundingClientRect();
    if (ptr.size === 2) {
      const [a, b] = [...ptr.values()];
      gesture = { type: "pinch", dist: Math.hypot(a.x - b.x, a.y - b.y), k: P.view.k };
      return;
    }
    const item = e.target.closest(".item");
    if (P.edit && item) {
      const key = item.dataset.key, w0 = toWorld(e.clientX - r.left, e.clientY - r.top);
      gesture = { type: "drag", key, dx: w0.x - pos(key).x, dy: w0.y - pos(key).y, moved: false };
    } else {
      gesture = { type: "pan", sx: e.clientX, sy: e.clientY, vx: P.view.x, vy: P.view.y, moved: false,
                  zone: item && item.dataset.zone ? Number(item.dataset.zone) : null };
    }
  });
  svg.addEventListener("pointermove", e => {
    if (!ptr.has(e.pointerId)) return;
    ptr.set(e.pointerId, { x: e.clientX, y: e.clientY });
    const r = svg.getBoundingClientRect();
    if (gesture && gesture.type === "pinch" && ptr.size === 2) {
      const [a, b] = [...ptr.values()];
      const dist = Math.hypot(a.x - b.x, a.y - b.y);
      zoomAt(gesture.k * dist / gesture.dist / P.view.k, (a.x + b.x) / 2 - r.left, (a.y + b.y) / 2 - r.top);
    } else if (gesture && gesture.type === "pan") {
      const dx = e.clientX - gesture.sx, dy = e.clientY - gesture.sy;
      if (Math.abs(dx) + Math.abs(dy) > 4) gesture.moved = true;
      P.view.x = gesture.vx + dx; P.view.y = gesture.vy + dy; applyView();
    } else if (gesture && gesture.type === "drag") {
      const w0 = toWorld(e.clientX - r.left, e.clientY - r.top);
      P.layout[gesture.key] = { x: Math.round((w0.x - gesture.dx) / 10) * 10, y: Math.round((w0.y - gesture.dy) / 10) * 10 };
      gesture.moved = true;
      draw();
    }
  });
  const end = e => {
    ptr.delete(e.pointerId);
    if (gesture && gesture.type === "pan" && !gesture.moved && gesture.zone) select(gesture.zone);
    else if (gesture && gesture.type === "pan" && !gesture.moved && !gesture.zone) select(null);
    if (ptr.size === 0) gesture = null;
  };
  svg.addEventListener("pointerup", end);
  svg.addEventListener("pointercancel", end);
  svg.addEventListener("wheel", e => {
    e.preventDefault();
    const r = svg.getBoundingClientRect();
    zoomAt(e.deltaY < 0 ? 1.12 : 1 / 1.12, e.clientX - r.left, e.clientY - r.top);
  }, { passive: false });
  svg.addEventListener("keydown", e => {
    const r = svg.getBoundingClientRect();
    if (e.key === "+" || e.key === "=") zoomAt(1.2, r.width / 2, r.height / 2);
    else if (e.key === "-") zoomAt(1 / 1.2, r.width / 2, r.height / 2);
    else if (e.key === "Enter" && e.target.dataset && e.target.dataset.zone) select(Number(e.target.dataset.zone));
    else if (e.key === "Escape") select(null);
    else if (["ArrowLeft", "ArrowRight", "ArrowUp", "ArrowDown"].includes(e.key)) {
      const s = 40;
      P.view.x += e.key === "ArrowLeft" ? s : e.key === "ArrowRight" ? -s : 0;
      P.view.y += e.key === "ArrowUp" ? s : e.key === "ArrowDown" ? -s : 0;
      applyView(); e.preventDefault();
    }
  });
}

// ---------------------------------------------------------------- detail drawer
function select(id) {
  P.selected = id;
  document.querySelectorAll(".zone.sel").forEach(n => n.classList.remove("sel"));
  if (id) document.querySelectorAll(`.zone[data-zone="${id}"]`).forEach(n => n.classList.add("sel"));
  $("zone-list").querySelectorAll("[data-zone]").forEach(b => b.setAttribute("aria-pressed", Number(b.dataset.zone) === id));
  if (!id) { $("drawer").hidden = true; return; }
  renderDrawer();
  $("drawer").hidden = false;
  const r = $("plan").getBoundingClientRect(), zp = pos(`zone:${id}`);
  const right = P.view.x + (zp.x + SZ.zone.w) * P.view.k;
  if (narrow() || right > r.width - 380) focusOn(id);                // keep the plot visible beside the panel
}

function renderDrawer() {
  const z = P.data.zones.find(x => x.id === P.selected);
  if (!z) return;
  const s = z.stage, p = z.plan || {}, n = p.next_irrigation, v = z.vision;
  const soil = z.sensors.find(x => x.key === "soil_moisture") || {};
  const cur = z.procedure.steps.find(x => x.key === z.procedure.current) || {};
  const issues = z._issues;
  $("drawer-body").innerHTML = `
    <p class="muted small">${E(cropName(z))}</p>
    <h2 id="dr-title">${E(z.name)}</h2>
    <span class="status-pill st-z-${z._state}">${E(STATE_FR[z._state])}</span>
    ${issues.length ? `<ul class="plain dr-issues">${issues.map(i => `
      <li class="sev-${i.sev}"><b>${E(i.title)}</b>${i.text ? `<span>${E(i.text)}</span>` : ""}${i.todo ? `<em>À faire : ${E(i.todo)}</em>` : ""}</li>`).join("")}</ul>`
      : `<p class="small" style="margin-top:10px">Aucun problème sur cette parcelle.</p>`}
    <dl class="kv">
      <div><dt>Stade</dt><dd>${s && s.available ? E(s.stage.name_fr) : "—"}</dd></div>
      <div><dt>Prochain stade</dt><dd>${s && s.next_stage ? `${E(s.next_stage.name_fr)}, ${AW.date(s.next_stage_date, false)}` : "—"}</dd></div>
      <div><dt>Humidité du sol</dt><dd>${soil.value != null ? AW.num(soil.value * 100, 1) + " %" : "—"}</dd></div>
      <div><dt>Irrigation</dt><dd>${p.frequency ? E(p.frequency.text_fr) : "—"}</dd></div>
      <div><dt>Prochaine</dt><dd>${n ? E(n.when_fr) + ` · ${AW.num(n.volume_l, 0)} L` : p.status === "irrigating" ? "après celle en cours" : "—"}</dd></div>
      <div><dt>Étape du cycle</dt><dd>${E(cur.title || "—")} · ${E(cur.value || "")}</dd></div>
    </dl>
    ${v ? `<div class="dr-cam">${AW.imageUrl(v.image_ref) ? `<img src="${AW.imageUrl(v.image_ref)}" alt="Dernière image de la parcelle" onerror="this.remove()">` : ""}
      <p class="small">Caméra ${AW.ago(v.ts)} : ${E([v.crop_fr, v.stage_fr].filter(Boolean).join(" · ") || "image reçue")}</p></div>` : ""}
    <div class="actions">
      <a class="btn small-btn primary" href="zone.html?zone=${z.id}">Ouvrir la zone</a>
      <a class="btn small-btn" href="camera.html">Caméra</a>
    </div>`;
}

// ---------------------------------------------------------------- toolbar
function setupTools() {
  $("zin").onclick = () => { const r = $("plan").getBoundingClientRect(); zoomAt(1.25, r.width / 2, r.height / 2); };
  $("zout").onclick = () => { const r = $("plan").getBoundingClientRect(); zoomAt(0.8, r.width / 2, r.height / 2); };
  $("zfit").onclick = fit;
  $("dr-close").onclick = () => select(null);
  const toggle = (btn, key) => { $(btn).onclick = () => { P.show[key] = !P.show[key]; $(btn).setAttribute("aria-pressed", P.show[key]); draw(); }; };
  toggle("t-water", "water");
  toggle("t-data", "data");
  $("edit").onclick = () => setEdit(!P.edit);
  $("cancel").onclick = () => { P.layout = JSON.parse(JSON.stringify(P.saved)); setEdit(false); draw(); };
  $("save").onclick = async () => {
    try { await AW.api("/api/dashboard/layout", { method: "PUT", body: { data: { items: P.layout } } });
          P.saved = JSON.parse(JSON.stringify(P.layout)); setEdit(false); AW.toast("Plan enregistré."); }
    catch (e) { AW.toast("Non enregistré : " + e.message); }
  };
  $("reset").onclick = async () => {
    await AW.api("/api/dashboard/layout", { method: "DELETE" });
    const lay = await AW.api("/api/dashboard/layout");
    P.layout = lay.items; P.saved = JSON.parse(JSON.stringify(lay.items)); setEdit(false); draw(); fit();
  };
  window.addEventListener("resize", () => { if (!P.edit) fit(); });
  window.addEventListener("aw:refresh", load);
}
function setEdit(on) {
  P.edit = on;
  $("edit").setAttribute("aria-pressed", on);
  $("edit").hidden = on;
  $("edit-actions").hidden = !on;
  $("plan").classList.toggle("editing", on);
  $("hint").textContent = on ? "Faire glisser le réservoir, le boîtier ou une parcelle, puis Enregistrer."
                             : "Glisser pour se déplacer · molette ou pincer pour zoomer · toucher une parcelle pour le détail";
}

setupPointers();
setupTools();
load();
setInterval(() => { if (!document.hidden && !P.edit && !gesture) load(); }, 10000);
