"""Regenerate docs/CROP_CYCLES.md from app/agronomy/crop_cycles.py.

Run from kizuna-backend:  python tools/make_crop_cycles_doc.py
"""
from __future__ import annotations

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from app.agronomy.crop_cycles import CYCLES  # noqa: E402

SENS = {"critical": "**critical**", "sensitive": "sensitive", "less_sensitive": "less sensitive",
        "not_singled_out": "not singled out by FAO", "": "not documented yet"}


def main() -> None:
    out = ["# Crop development cycles (AGRIWISE)", "",
           "Generated from `app/agronomy/crop_cycles.py` by `tools/make_crop_cycles_doc.py` – do not edit by hand.", "",
           "Each crop has its **own** cycle: stages, durations, BBCH codes, visual cues, field checks and, for",
           "pepper and potato, the documented needs of every stage. Stage durations add up, period by period,",
           "to the crop's FAO-56 lengths (Table 11) so that Kc and root depth follow the same stage.",
           "Source keys in brackets are listed in [REFERENCES.md](REFERENCES.md).", "",
           "**How the current stage is estimated:** planting date (or the farmer's last confirmation) → calendar",
           "of the crop's own cycle → corrected by the degree-days of the season compared with the local climate",
           "normals (pepper, potato) → checked against the last photo → confirmed by the farmer (always required",
           "for underground potato stages).", ""]
    for c in CYCLES.values():
        out += [f"## {c.crop_key.capitalize()}  ({', '.join(c.sources)})", ""]
        if c.thermal:
            out += [f"Degree-days: base {c.thermal.t_base_c} °C, upper cutoff {c.thermal.t_upper_c} °C, "
                    f"single sine ({c.thermal.source}). Reference season: planting on "
                    f"{c.reference_planting[1]:02d}/{c.reference_planting[0]:02d} – {c.reference_planting_note}", ""]
        else:
            out += ["Degree-days: no documented thresholds yet → calendar only.", ""]
        out += ["| # | Stage (EN) | Stade (FR) | BBCH | FAO-56 period | Days | Visible on a photo | "
                "Water-deficit sensitivity (FAO) | Field check |", "|---|---|---|---|---|---|---|---|---|"]
        for i, s in enumerate(c.stages, 1):
            out.append(f"| {i} | {s.name_en} | {s.name_fr} | {s.bbch} | {s.fao_period} | {s.days} | "
                       f"{'yes' if s.visible else '**no (underground)**'} | {SENS.get(s.water_sensitivity, s.water_sensitivity)} | "
                       f"{s.field_check or '–'} |")
        out.append("")
        if any(s.needs for s in c.stages):
            out += ["### Stage descriptions and needs", ""]
            for s in c.stages:
                out += [f"**{s.name_en} / {s.name_fr}** – {s.description_en}", ""]
                out += [f"- ({n.topic}) {n.text_en} [{n.source}]" for n in s.needs]
                if s.max_depletion is not None:
                    out.append(f"- Irrigation engine: depletion limited to {s.max_depletion:.0%} of TAW in this stage "
                               f"[{s.max_depletion_source}].")
                out.append("")
        out.append("Camera classes → compatible stages: " + "; ".join(
            f"`{k}` → {', '.join(v)}" for k, v in c.visual_classes.items()))
        out += ["", "Assumptions:"] + [f"- {a}" for a in c.assumptions] + [""]
    (ROOT / "docs" / "CROP_CYCLES.md").write_text("\n".join(out), encoding="utf-8")
    print("docs/CROP_CYCLES.md written")


if __name__ == "__main__":
    main()
