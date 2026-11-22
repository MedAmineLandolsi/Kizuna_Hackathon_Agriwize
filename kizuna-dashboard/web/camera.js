// Camera procedure page (images come from the field cameras, no manual upload):
// photo -> crop -> visual stage -> health -> ESP32 sensors -> stage kept -> irrigation plan -> farmer.
"use strict";

const cam = { data: null, selected: null, filter: "", zonesFilled: false, newest: null, followNewest: true };
const $ = id => document.getElementById(id);
const E = AW.esc;
const PENDING_STEPS = ["Photo reçue", "Culture", "Stade visuel", "Santé", "Capteurs ESP32 au même moment",
                       "Stade retenu", "Plan d'irrigation", "Action de l'agriculteur"];
const SOURCE = { phone: "Téléphone", mast: "Caméra fixe", carriage: "Chariot", trap: "Piège" };

function imgUrl(ref) { return AW.imageUrl(ref); }

async function load(selectNewest = false) {
  try {
    cam.data = await AW.api("/api/dashboard/camera" + (cam.filter ? `?zone_id=${cam.filter}` : ""));
  } catch (e) {
    $("procedure").innerHTML = `<li data-state="error"><div>Le serveur ne répond pas : ${E(e.message)}</div></li>`;
    return;
  }
  const d = cam.data;
  try { AW.renderStatus((await AW.api("/api/dashboard/overview")).system); } catch (e) { /* status bar only */ }
  if (!cam.zonesFilled) {
    const opts = d.zones.map(z => `<option value="${z.id}">${E(z.name)}${z.crop ? " (" + E(z.crop) + ")" : ""}</option>`).join("");
    $("filter").innerHTML = `<option value="">Toutes</option>` + opts;
    cam.zonesFilled = true;
  }
  renderCameras();
  if (!d.observations.length) {
    $("history").innerHTML = `<div class="empty">Aucune image reçue pour l'instant. Dès que la caméra envoie une image, l'analyse apparaît ici.</div>`;
    renderProcedure(null); return;
  }
  const newest = d.observations[0].id;
  if (cam.newest !== null && newest !== cam.newest) AW.toast("Nouvelle image reçue de la caméra.");
  if (cam.followNewest || selectNewest || !cam.selected || !d.observations.find(o => o.id === cam.selected)) cam.selected = newest;
  cam.newest = newest;
  renderHistory();
  await renderSelected();
}

function every(sec) {
  if (!sec) return "";
  if (sec < 90) return `une image toutes les ${Math.round(sec)} s`;
  return sec < 5400 ? `une image toutes les ${Math.round(sec / 60)} min` : `une image toutes les ${AW.num(sec / 3600, 1)} h`;
}

function renderCameras() {
  const list = cam.data.cameras || [];
  if (!list.length) {
    $("cams").innerHTML = `<div class="empty">Aucune caméra n'a encore envoyé d'image. Sur l'ordinateur relié à la caméra :
      <code>python -m vision.capture --camera 0 --zone 2 --source mast --every 30</code>
      (une image toutes les 30 min pour la zone 2).</div>`;
    return;
  }
  $("cams").innerHTML = list.map(c => {
    const zone = cam.data.zones.find(z => z.id === c.zone_id);
    const st = c.state === "late" ? "late" : "on";
    return `<button class="cam" data-id="${c.last_id}" data-state="${st}">
      <span><i class="dot ${st === "late" ? "off" : "on"}"></i><strong>${E(SOURCE[c.source] || c.source)}</strong> · ${E(zone ? zone.name : "sans zone")}</span>
      <span class="muted small">Dernière image ${AW.ago(c.last_ts)}${c.interval_s ? " · " + every(c.interval_s) : ""}</span>
      ${st === "late" ? `<span class="small missing">En retard : vérifier la caméra et le script de capture.</span>` : ""}
    </button>`;
  }).join("");
  $("cams").querySelectorAll("button").forEach(b => b.onclick = () => {
    cam.selected = Number(b.dataset.id); cam.followNewest = cam.selected === cam.newest; renderHistory(); renderSelected();
  });
}

function renderHistory() {
  $("history").innerHTML = cam.data.observations.map(o => {
    const url = imgUrl(o.image_ref);
    const warn = o.steps.filter(s => s.state === "warn" || s.state === "error").length;
    const zone = cam.data.zones.find(z => z.id === o.zone_id);
    return `<button data-id="${o.id}" aria-pressed="${o.id === cam.selected}">
      <div class="thumb">${url ? `<img src="${url}" alt="" loading="lazy" onerror="this.replaceWith(document.createTextNode('image indisponible'))">` : "sans image"}</div>
      <div class="meta"><b>${E(o.crop_fr || SOURCE[o.source] || o.source)}${o.growth_stage ? " · " + E(o.growth_stage_fr || o.growth_stage.replace("_", " ")) : ""}</b>
        ${E(zone ? zone.name : "sans zone")}<br>${AW.date(o.ts)}${warn ? ` · <span class="missing">${warn} point(s) à voir</span>` : ""}</div>
    </button>`;
  }).join("");
  $("history").querySelectorAll("button").forEach(b => b.onclick = () => {
    cam.selected = Number(b.dataset.id);
    cam.followNewest = cam.selected === cam.newest;          // looking at an older image: stay on it
    renderHistory(); renderSelected();
  });
}

async function renderSelected() {
  let o = cam.data.observations.find(x => x.id === cam.selected);
  if (!o) return;
  if (!o.current_plan && o.source !== "trap") {             // details (current stage and plan) for older photos
    try { o = { ...o, ...(await AW.api(`/api/dashboard/camera/${o.id}`)) }; } catch (e) { /* keep list version */ }
  }
  const url = imgUrl(o.image_ref);
  $("photo").innerHTML = url ? `<img src="${url}" alt="Photo analysée le ${AW.date(o.ts)}" onerror="this.parentNode.textContent='Image indisponible : le service de vision (${E(AW.vision)}) ne répond pas.'">` : "Pas d'image enregistrée";
  const zone = cam.data.zones.find(z => z.id === o.zone_id);
  $("proc-meta").textContent = `${SOURCE[o.source] || o.source} · ${zone ? zone.name : "sans zone"} · ${AW.date(o.ts)}`;
  renderProcedure(o);
  renderCurrent(o);
}

function renderProcedure(o) {
  if (!o) { $("proc-meta").textContent = "En attente de la première image"; $("procedure").innerHTML = PENDING_STEPS.map(t => `<li data-state="idle"><div class="p-head"><span>${E(t)}</span></div></li>`).join(""); return; }
  $("procedure").innerHTML = o.steps.map(s => {
    let extra = "";
    if (s.key === "stage" && s.status === "needs_confirmation" && s.stage_observation_id) {
      extra = `<div class="actions">${(s.candidates || []).map(c =>
        `<button class="btn" data-confirm="${s.stage_observation_id}" data-stage="${E(c)}">Confirmer : ${E((s.candidate_names || {})[c] || c)}</button>`).join("")}
        ${s.calendar_stage ? `<button class="btn" data-keep="${s.zone_id}" data-stage="${E(s.calendar_stage.key)}">Garder : ${E(s.calendar_stage.name_fr)}</button>`
          : `<button class="btn" data-reject="${s.stage_observation_id}">Ce n'est pas ça</button>`}</div>`;
    }
    const conf = s.confidence != null ? ` <span class="muted small">(${Math.round(s.confidence * 100)} %, score relatif)</span>` : "";
    return `<li data-state="${E(s.state)}">
      <div class="p-head"><span>${E(s.title)}</span><span class="p-val">${E(s.value)}</span>${conf} ${AW.kind(s.kind)}</div>
      ${s.detail ? `<p class="p-det">${E(s.detail)}</p>` : ""}${extra}</li>`;
  }).join("");
  $("procedure").querySelectorAll("[data-confirm]").forEach(b => b.onclick = async () => {
    await AW.api(`/api/stage-observations/${b.dataset.confirm}/confirm`, { method: "POST", body: { stage_key: b.dataset.stage } });
    AW.toast("Stade confirmé : le cycle et le calcul d'irrigation sont recalés."); load();
  });
  $("procedure").querySelectorAll("[data-keep]").forEach(b => b.onclick = async () => {
    await AW.api(`/api/zones/${b.dataset.keep}/stage/confirm`, { method: "POST", body: { stage_key: b.dataset.stage, note: "confirmed from the camera page" } });
    AW.toast("Stade du calendrier confirmé."); load();
  });
  $("procedure").querySelectorAll("[data-reject]").forEach(b => b.onclick = async () => {
    await AW.api(`/api/stage-observations/${b.dataset.reject}/reject`, { method: "POST" });
    AW.toast("Proposition rejetée : le calendrier reste la référence."); load();
  });
}

function renderCurrent(o) {
  const st = o.current_stage, pl = o.current_plan;
  if (!st || !st.available) {
    $("current").innerHTML = `<h3>Situation actuelle de la zone</h3><p class="muted small" style="margin-top:6px">
      ${o.source === "trap" ? "Photo de piège : pas de stade ni de plan." : "Pas de stade ni de plan pour cette photo (culture différente de la zone ou zone sans date de plantation)."}</p>`;
    return;
  }
  const n = pl && pl.next_irrigation;
  $("current").innerHTML = `
    <h3>Situation actuelle de la zone</h3>
    <div class="facts">
      <div><b>${E(st.stage.name_fr)}</b><span>stade (certitude ${E(AW.CERTAINTY[st.certainty.level] || st.certainty.level)})</span></div>
      <div><b>${st.next_stage ? E(st.next_stage.name_fr) : "Fin du cycle"}</b><span>${st.next_stage_date ? "vers le " + AW.date(st.next_stage_date, false) + ", dans " + st.days_to_next_stage + " j" : ""}</span></div>
      ${pl && pl.frequency ? `
      <div><b>${E(pl.frequency.text_fr)}</b><span>≈ ${AW.num(pl.irrigations_per_week, 1)} par semaine</span></div>
      <div><b>${n ? E(n.when_fr) : pl.status === "irrigating" ? "après celle en cours" : "non déterminée"}</b><span>prochaine irrigation${n ? ", " + AW.num(n.volume_l, 0) + " L" : ""}</span></div>
      <div><b>${AW.num(pl.volume_per_irrigation_l, 0)} L · ${AW.num(pl.duration_per_irrigation_min, 0)} min</b><span>par irrigation</span></div>` : ""}
    </div>
    ${pl && (pl.missing || []).length ? `<ul class="notes missing">${pl.missing.map(m => `<li>Manquant : ${E(m)}</li>`).join("")}</ul>` : ""}
    <p class="muted small"><a href="zone.html?zone=${o.zone_id}">Voir la zone</a> · <a href="./?focus=${o.zone_id}">sur le plan</a></p>`;
}

$("filter").onchange = e => { cam.filter = e.target.value; cam.selected = null; load(); };

(async () => {
  await load();
  try {
    const h = await fetch(AW.vision + "/health").then(r => r.json());
    const off = Object.keys((h.engines || {}).unavailable || {});
    $("vision-state").textContent = "Service de vision connecté" + (off.length ? ` (modules indisponibles : ${off.join(", ")})` : "") +
      ". Chaque image est analysée puis croisée avec les capteurs de l'ESP32.";
  } catch (e) {
    $("vision-state").innerHTML = `<span class="missing">Service de vision injoignable sur ${E(AW.vision)} : les images de la caméra ne seront pas analysées.</span>`;
  }
})();
setInterval(() => { if (!document.hidden) load(); }, 10000);
