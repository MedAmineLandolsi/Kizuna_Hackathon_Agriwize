"""Crop-specific development cycles.

Each crop has its OWN stages, durations, visual cues and field checks. No generic cycle is
shared between crops. A crop without a cycle defined here falls back to its four FAO-56
periods, clearly flagged as generic (only for crops a user adds later).

Durations: the stages of a crop add up, period by period, to the crop's FAO-56 stage
lengths (Table 11), so the Kc curve and root growth stay consistent with the water engine.
Where an FAO period is split into two agronomic stages (e.g. potato mid-season =
tuber initiation + tuber bulking), the split is an assumption, listed in `assumptions`.

References: see docs/REFERENCES.md (keys in brackets).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .thermal import ThermalParams

FAO_PERIODS = ("initial", "development", "mid_season", "late_season")


@dataclass(frozen=True)
class Stage:
    key: str
    name_en: str
    name_fr: str
    fao_period: str                 # which FAO-56 Kc period it belongs to
    days: int                       # default duration (days) for the reference planting season
    bbch: str                       # BBCH principal/secondary codes covered
    visible: bool                   # can a photo of the above-ground plant show this stage?
    visual_cues: tuple[str, ...]    # what the camera / farmer looks for
    field_check: str = ""           # how to verify on the plant (dig, rub skin, count flowers…)
    description_en: str = ""
    description_fr: str = ""
    # sensitivity of yield to water deficit in this stage, AS WORDED BY THE FAO crop pages (qualitative):
    # critical (named most sensitive) | sensitive (deficit damages yield/quality) |
    # less_sensitive (named less sensitive) | not_singled_out (FAO says nothing specific) | "" (not documented)
    water_sensitivity: str = ""
    needs: tuple["Need", ...] = ()  # documented needs and risks of this stage
    # management limit on soil-water depletion (fraction of TAW) when a reference gives one for
    # this stage; the irrigation engine uses min(FAO-56 p, this value)
    max_depletion: float | None = None
    max_depletion_source: str = ""


@dataclass(frozen=True)
class Need:
    topic: str                      # water | temperature | salinity | rain | management
    text_en: str
    text_fr: str
    source: str                     # reference key (docs/REFERENCES.md)


@dataclass(frozen=True)
class CropCycle:
    crop_key: str
    stages: tuple[Stage, ...]
    # visual class (what the vision service can recognise) -> agronomic stages it is compatible with
    visual_classes: dict[str, tuple[str, ...]]
    sources: tuple[str, ...]
    assumptions: tuple[str, ...] = field(default_factory=tuple)
    generic: bool = False
    thermal: ThermalParams | None = None     # None: no documented thresholds -> calendar only
    # planting date of the season the FAO-56 lengths describe (month, day): the degree-days of each
    # stage are measured over that reference season in the local climate normals
    reference_planting: tuple[int, int] | None = None
    reference_planting_note: str = ""

    @property
    def length(self) -> int:
        return sum(s.days for s in self.stages)

    def index(self, stage_key: str) -> int:
        for i, s in enumerate(self.stages):
            if s.key == stage_key:
                return i
        raise KeyError(f"{stage_key} is not a stage of {self.crop_key}")

    def get(self, stage_key: str) -> Stage:
        return self.stages[self.index(stage_key)]

    def start_day(self, stage_key: str) -> int:
        i = self.index(stage_key)
        return sum(s.days for s in self.stages[:i])

    def period_days(self) -> dict[str, int]:
        out = {p: 0 for p in FAO_PERIODS}
        for s in self.stages:
            out[s.fao_period] += s.days
        return out


# ------------------------------------------------------------------ sweet pepper
PEPPER = CropCycle(
    crop_key="pepper",
    stages=(
        Stage("establishment", "Germination and plant establishment", "Germination et établissement du plant",
              "initial", 25, "BBCH 00–1x (germination, first true leaves; transplant establishment)", True,
              ("seedling or young transplant", "a few true leaves", "no flower buds")),
        Stage("vegetative", "Vegetative growth", "Croissance végétative",
              "development", 35, "BBCH 1x–2x (leaf development, side shoots)", True,
              ("many leaves and branches", "no open flowers")),
        Stage("flowering", "Flowering", "Floraison",
              "mid_season", 15, "BBCH 5x–6x (flower buds, open flowers)", True,
              ("flower buds or open white flowers", "no fruit yet or very small fruit"),
              "Count open flowers on 5 plants."),
        Stage("fruit_set", "Fruit set and fruit development", "Nouaison et développement des fruits",
              "mid_season", 25, "BBCH 7x (fruit development)", True,
              ("small to full-size green fruits", "flowering may continue on upper nodes")),
        Stage("ripening", "Ripening and harvest", "Maturation et récolte",
              "late_season", 20, "BBCH 8x (fruit colouring/ripening)", True,
              ("fruits turning red, yellow or orange", "harvest under way")),
    ),
    visual_classes={
        "seedling": ("establishment",),
        "leafy_no_flowers": ("vegetative",),
        "flowers": ("flowering",),
        "green_fruits": ("fruit_set",),
        "coloured_fruits": ("ripening",),
    },
    sources=("FAO56", "FAO_CROP_PEPPER", "BBCH_SOLANACEOUS", "BBCH_MONOGRAPH", "OSU_CROPTIME"),
    assumptions=(
        "FAO-56 lengths for April/June planting, Mediterranean: 25/35/40/20 days.",
        "The FAO mid-season (40 d) is split into flowering 15 d + fruit set 25 d (assumption; "
        "pepper keeps flowering while fruits develop).",
        "Degree-day thresholds 11.1 °C / 37.8 °C (52 °F / 100 °F) come from the Oregon Croptime sweet "
        "pepper models; they are used only to compare the season with the local normals, not as "
        "stage requirements.",
    ),
    thermal=ThermalParams(t_base_c=11.1, t_upper_c=37.8, source="OSU_CROPTIME",
                          note="52 °F / 100 °F, single sine, horizontal cutoff"),
    reference_planting=(5, 15),
    reference_planting_note="FAO-56 Table 11 pepper lengths are for April/June planting: mid-May taken as the "
                            "reference date (assumption).",
)

_PEPPER_DETAILS = {
    "establishment": dict(
        description_en="Seedling or freshly transplanted plant building its first roots and true leaves. "
                       "Roots are shallow, so the plant depends on the top layer of soil.",
        description_fr="Jeune plant (semis ou repiquage) qui forme ses premières racines et vraies feuilles. "
                       "Les racines sont superficielles : la plante dépend de la couche de surface.",
        water_sensitivity="not_singled_out",
        needs=(
            Need("water", "Shallow root zone (about 0.25 m at the start): light, frequent irrigations keep it moist.",
                 "Zone racinaire superficielle (environ 0,25 m au départ) : des irrigations légères et fréquentes la gardent humide.",
                 "FAO_CROP_PEPPER"),
            Need("salinity", "Seedlings are more sensitive to soil salinity than mature plants: watch the EC.",
                 "Les jeunes plants sont plus sensibles à la salinité que les plants adultes : surveiller la conductivité (EC).",
                 "FAO_CROP_PEPPER"),
            Need("temperature", "Optimal soil temperature for germination: 20–24 °C (direct seeding).",
                 "Température du sol optimale pour la germination : 20–24 °C (semis direct).", "FAO_CROP_PEPPER"),
        )),
    "vegetative": dict(
        description_en="Leaves and side shoots multiply; the canopy and the root system grow fast, so the water "
                       "demand rises day after day.",
        description_fr="Les feuilles et ramifications se multiplient ; le feuillage et les racines se développent "
                       "vite, la demande en eau augmente de jour en jour.",
        water_sensitivity="not_singled_out",
        needs=(
            Need("water", "Crop coefficient rises from 0.6 to 1.05 during this stage: water needs increase with the canopy.",
                 "Le coefficient cultural passe de 0,6 à 1,05 pendant ce stade : les besoins augmentent avec le feuillage.",
                 "FAO56"),
            Need("water", "Waterlogging, even for short periods, causes leaf shedding: avoid over-irrigation.",
                 "L'engorgement, même de courte durée, provoque la chute des feuilles : éviter l'excès d'irrigation.",
                 "FAO_CROP_PEPPER"),
            Need("temperature", "Good growth with day temperatures of 18–27 °C and nights of 15–18 °C.",
                 "Bonne croissance avec 18–27 °C le jour et 15–18 °C la nuit.", "FAO_CROP_PEPPER"),
        )),
    "flowering": dict(
        description_en="Flower buds and open white flowers. Each flower that drops is a lost fruit, so this is "
                       "the stage where water stress costs the most.",
        description_fr="Boutons floraux et fleurs blanches ouvertes. Chaque fleur qui tombe est un fruit perdu : "
                       "c'est le stade où un stress hydrique coûte le plus.",
        water_sensitivity="critical",
        max_depletion=0.25, max_depletion_source="FAO_CROP_PEPPER",
        needs=(
            Need("water", "Start of flowering is the most sensitive period: soil water depletion should not exceed 25 % "
                          "of the available water.",
                 "Le début de la floraison est la période la plus sensible : l'épuisement de l'eau du sol ne doit pas "
                 "dépasser 25 % de l'eau disponible.", "FAO_CROP_PEPPER"),
            Need("rain", "Heavy rain during flowering causes flower shedding and poor fruit set.",
                 "De fortes pluies pendant la floraison provoquent la chute des fleurs et une mauvaise nouaison.",
                 "FAO_CROP_PEPPER"),
            Need("temperature", "Warmer nights bring flowering forward.",
                 "Des nuits plus chaudes avancent la floraison.", "FAO_CROP_PEPPER"),
        )),
    "fruit_set": dict(
        description_en="Fruits set and grow to full size while the upper nodes may still flower. Peak water use.",
        description_fr="Les fruits nouent et grossissent jusqu'à leur taille finale, les étages supérieurs peuvent "
                       "encore fleurir. Consommation d'eau maximale.",
        water_sensitivity="sensitive",
        needs=(
            Need("water", "Water deficit during yield formation gives shrivelled, malformed fruit.",
                 "Un déficit hydrique pendant la formation du rendement donne des fruits ridés et déformés.",
                 "FAO_CROP_PEPPER"),
            Need("water", "Mid-season crop coefficient 1.05 (before climate adjustment).",
                 "Coefficient cultural de mi-saison 1,05 (avant ajustement climatique).", "FAO56"),
            Need("water", "For optimum yield, depletion should not exceed 30–40 % of the total available water.",
                 "Pour un rendement optimal, l'épuisement ne doit pas dépasser 30–40 % de l'eau totale disponible.",
                 "FAO_CROP_PEPPER"),
        )),
    "ripening": dict(
        description_en="Fruits change colour (red, yellow or orange) and are picked. Water use decreases slowly.",
        description_fr="Les fruits changent de couleur (rouge, jaune ou orange) et sont récoltés. La consommation "
                       "d'eau diminue lentement.",
        water_sensitivity="not_singled_out",
        needs=(
            Need("rain", "Heavy rain during ripening causes fruit rot.",
                 "De fortes pluies pendant la maturation provoquent la pourriture des fruits.", "FAO_CROP_PEPPER"),
            Need("water", "If water is limited, give priority to irrigation up to the first picking.",
                 "Si l'eau est limitée, prioriser l'irrigation jusqu'à la première récolte.", "FAO_CROP_PEPPER"),
            Need("water", "End-of-season crop coefficient 0.9.", "Coefficient cultural de fin de saison 0,9.", "FAO56"),
        )),
}

# ------------------------------------------------------------------ potato
POTATO = CropCycle(
    crop_key="potato",
    stages=(
        Stage("sprouting_emergence", "Sprouting and emergence", "Germination et levée",
              "initial", 25, "BBCH 00–09 (sprouting, emergence)", True,
              ("shoots breaking through the soil", "few or no unfolded leaves")),
        Stage("vegetative", "Vegetative development", "Développement végétatif",
              "development", 30, "BBCH 1x–3x (leaves, basal side shoots, stem elongation / crop cover)", True,
              ("leafy stems", "canopy closing between rows")),
        Stage("tuber_initiation", "Tuber initiation", "Initiation et tubérisation",
              "mid_season", 15, "BBCH 40 (tuber initiation)", False,
              ("UNDERGROUND – proxy only: flower buds appearing (varieties that flower, well-watered crop)",),
              "Dig one plant: tuber initiation = first stolon tips swollen to twice the stolon diameter (BBCH 40)."),
        Stage("tuber_bulking", "Tuber bulking", "Grossissement des tubercules",
              "mid_season", 30, "BBCH 41–48 (tuber mass increasing)", False,
              ("UNDERGROUND – proxy only: full canopy, flowering in flowering varieties",),
              "Dig one plant: tubers clearly growing; weigh a sample of tubers every week."),
        Stage("maturation", "Maturation and senescence", "Maturation et sénescence",
              "late_season", 30, "BBCH 48–49 and 9x (skin set, senescence)", True,
              ("leaves yellowing", "vines dying back"),
              "Rub a tuber: skin set when the skin no longer comes off with the thumb."),
    ),
    visual_classes={
        "emergence": ("sprouting_emergence",),
        "leafy_canopy": ("vegetative", "tuber_initiation", "tuber_bulking"),
        "flowering": ("tuber_initiation", "tuber_bulking"),
        "senescence": ("maturation",),
    },
    sources=("FAO56", "FAO_CROP_POTATO", "BBCH_POTATO", "BBCH_MONOGRAPH", "POTATO_STAGES_APS",
             "POTATO_ABOVE_BELOW", "POTATO_GDD_FL"),
    assumptions=(
        "FAO-56 lengths for Jan/Nov planting, (semi) arid climate: 25/30/45/30 days (mid-season 45 d option).",
        "The FAO mid-season (45 d) is split into tuber initiation 15 d + tuber bulking 30 d (assumption).",
        "Tuber initiation and bulking happen underground: a photo can only give a proxy (canopy, flower buds, "
        "flowering). The link between above- and below-ground development is described for well-watered "
        "crops and depends on the variety; the stage is confirmed by digging a plant.",
        "Degree-days: base 7 °C as in the potato growing-degree-day study [POTATO_GDD_FL], horizontal cutoff "
        "at 30 °C, above which FAO reports tuber growth sharply inhibited [FAO_CROP_POTATO]. Day length also "
        "affects tuber initiation and is NOT modelled.",
    ),
    thermal=ThermalParams(t_base_c=7.0, t_upper_c=30.0, source="POTATO_GDD_FL, FAO_CROP_POTATO",
                          note="base 7 °C, cutoff 30 °C, single sine"),
    reference_planting=(1, 15),
    reference_planting_note="FAO-56 Table 11 potato lengths used are for Jan/Nov planting in a (semi) arid "
                            "climate: mid-January taken as the reference date (assumption).",
)

_POTATO_DETAILS = {
    "sprouting_emergence": dict(
        description_en="The seed tuber sprouts and the shoots break through the soil. The speed depends mostly on "
                       "soil temperature.",
        description_fr="Le tubercule-semence germe et les pousses percent le sol. La vitesse dépend surtout de la "
                       "température du sol.",
        water_sensitivity="less_sensitive",
        needs=(
            Need("water", "Early vegetative periods are less sensitive to water deficit than tuber initiation and "
                          "yield formation.",
                 "Les périodes végétatives précoces sont moins sensibles au déficit hydrique que l'initiation et la "
                 "formation des tubercules.", "FAO_CROP_POTATO"),
            Need("water", "Initial crop coefficient 0.5.", "Coefficient cultural initial 0,5.", "FAO56"),
        )),
    "vegetative": dict(
        description_en="Leaves, stems and side shoots develop until the rows close. Stolons start to form below "
                       "ground.",
        description_fr="Feuilles, tiges et ramifications se développent jusqu'à la fermeture des rangs. Les "
                       "stolons commencent à se former sous terre.",
        water_sensitivity="less_sensitive",
        needs=(
            Need("water", "About 70 % of the water is taken from the top 0.3 m of soil: keep that layer supplied.",
                 "Environ 70 % de l'eau est prélevée dans les 0,3 m supérieurs du sol : garder cette couche alimentée.",
                 "FAO_CROP_POTATO"),
            Need("water", "Crop coefficient rises from 0.5 to 1.15 as the canopy closes.",
                 "Le coefficient cultural passe de 0,5 à 1,15 à mesure que le feuillage couvre le sol.", "FAO56"),
        )),
    "tuber_initiation": dict(
        description_en="Stolon tips swell into the first tubers. It happens underground: it cannot be seen on a "
                       "photo of the leaves and is checked by digging one plant.",
        description_fr="Les extrémités des stolons gonflent et forment les premiers tubercules. Cela se passe sous "
                       "terre : invisible sur une photo des feuilles, on le vérifie en déterrant un plant.",
        water_sensitivity="critical",
        needs=(
            Need("water", "Stolonization and tuber initiation are among the periods where water deficit has the "
                          "greatest effect on yield.",
                 "La stolonisation et l'initiation des tubercules font partie des périodes où un déficit hydrique a "
                 "le plus d'effet sur le rendement.", "FAO_CROP_POTATO"),
            Need("temperature", "Tuber initiation needs night temperatures below 15 °C; optimum mean daily temperature "
                                "18–20 °C.",
                 "L'initiation des tubercules demande des nuits sous 15 °C ; température moyenne optimale 18–20 °C.",
                 "FAO_CROP_POTATO"),
        )),
    "tuber_bulking": dict(
        description_en="Tubers fill and gain most of the yield. Also underground: progress is checked by weighing "
                       "a sample of tubers.",
        description_fr="Les tubercules se remplissent et forment l'essentiel du rendement. Également souterrain : "
                       "on suit la progression en pesant un échantillon de tubercules.",
        water_sensitivity="critical",
        needs=(
            Need("water", "Yield formation is the other most sensitive period; keep depletion within 30–50 % of the "
                          "available water.",
                 "La formation du rendement est l'autre période la plus sensible ; garder l'épuisement entre 30 et "
                 "50 % de l'eau disponible.", "FAO_CROP_POTATO"),
            Need("water", "Frequent irrigation reduces tuber malformation.",
                 "Une irrigation fréquente réduit les déformations des tubercules.", "FAO_CROP_POTATO"),
            Need("temperature", "Tuber growth is sharply inhibited below 10 °C and above 30 °C; optimal soil "
                                "temperature 15–18 °C.",
                 "La croissance des tubercules est fortement freinée sous 10 °C et au-dessus de 30 °C ; température "
                 "du sol optimale 15–18 °C.", "FAO_CROP_POTATO"),
        )),
    "maturation": dict(
        description_en="Leaves yellow and vines die back while the tuber skin sets. Water use falls.",
        description_fr="Les feuilles jaunissent et les fanes se dessèchent pendant que la peau des tubercules "
                       "se raffermit. La consommation d'eau baisse.",
        water_sensitivity="less_sensitive",
        needs=(
            Need("water", "Ripening is less sensitive to water deficit; correct timing can save irrigations, "
                          "including the last one before harvest.",
                 "La maturation est moins sensible au déficit hydrique ; un bon calendrier peut économiser des "
                 "irrigations, dont la dernière avant la récolte.", "FAO_CROP_POTATO"),
            Need("water", "End-of-season crop coefficient 0.75.", "Coefficient cultural de fin de saison 0,75.", "FAO56"),
        )),
}


def _with_details(cycle: CropCycle, details: dict[str, dict]) -> CropCycle:
    from dataclasses import replace
    return replace(cycle, stages=tuple(replace(s, **details.get(s.key, {})) for s in cycle.stages))


PEPPER = _with_details(PEPPER, _PEPPER_DETAILS)
POTATO = _with_details(POTATO, _POTATO_DETAILS)

# ------------------------------------------------------------------ tomato
TOMATO = CropCycle(
    crop_key="tomato",
    stages=(
        Stage("establishment", "Germination and plant establishment", "Germination et établissement du plant",
              "initial", 30, "BBCH 00–1x", True, ("seedling or young transplant", "no flower trusses")),
        Stage("vegetative", "Vegetative growth", "Croissance végétative",
              "development", 40, "BBCH 1x–2x", True, ("hairy serrated leaves, side shoots", "no open flowers")),
        Stage("flowering", "Flowering (first trusses)", "Floraison (premiers bouquets)",
              "mid_season", 15, "BBCH 5x–6x", True, ("yellow star-shaped flowers on trusses",)),
        Stage("fruit_set", "Fruit set and fruit development", "Nouaison et développement des fruits",
              "mid_season", 30, "BBCH 7x", True, ("green fruits on lower trusses", "upper trusses still flowering")),
        Stage("ripening", "Ripening and harvest", "Maturation et récolte",
              "late_season", 30, "BBCH 8x", True, ("fruits turning orange/red", "harvest under way")),
    ),
    visual_classes={
        "seedling": ("establishment",), "leafy_no_flowers": ("vegetative",), "flowers": ("flowering",),
        "green_fruits": ("fruit_set",), "coloured_fruits": ("ripening",),
    },
    sources=("FAO56", "BBCH_SOLANACEOUS", "BBCH_MONOGRAPH"),
    assumptions=("FAO-56 lengths for April/May planting, Mediterranean: 30/40/45/30 days.",
                 "Mid-season split: flowering 15 d + fruit set 30 d (assumption).",
                 "Stage needs and degree-day thresholds not documented yet in AGRIWISE: calendar only."),
)

# ------------------------------------------------------------------ eggplant
EGGPLANT = CropCycle(
    crop_key="eggplant",
    stages=(
        Stage("establishment", "Germination and plant establishment", "Germination et établissement du plant",
              "initial", 30, "BBCH 00–1x", True, ("seedling or young transplant",)),
        Stage("vegetative", "Vegetative growth", "Croissance végétative",
              "development", 45, "BBCH 1x–2x", True, ("large fuzzy lobed leaves", "no open flowers")),
        Stage("flowering", "Flowering", "Floraison",
              "mid_season", 15, "BBCH 5x–6x", True, ("purple flowers",)),
        Stage("fruit_set", "Fruit set and fruit development", "Nouaison et développement des fruits",
              "mid_season", 25, "BBCH 7x", True, ("growing glossy purple fruits",)),
        Stage("ripening", "Ripening and harvest", "Maturation et récolte",
              "late_season", 25, "BBCH 8x", True, ("full-size glossy fruits", "harvest under way")),
    ),
    visual_classes={
        "seedling": ("establishment",), "leafy_no_flowers": ("vegetative",), "flowers": ("flowering",),
        "green_fruits": ("fruit_set",), "coloured_fruits": ("ripening",),
    },
    sources=("FAO56", "BBCH_SOLANACEOUS", "BBCH_MONOGRAPH"),
    assumptions=("FAO-56 lengths for May/June planting, Mediterranean: 30/45/40/25 days.",
                 "Mid-season split: flowering 15 d + fruit set 25 d (assumption).",
                 "Stage needs and degree-day thresholds not documented yet in AGRIWISE: calendar only."),
)

CYCLES: dict[str, CropCycle] = {c.crop_key: c for c in (PEPPER, POTATO, TOMATO, EGGPLANT)}


def fao_fallback(crop_key: str, l_ini: int, l_dev: int, l_mid: int, l_late: int) -> CropCycle:
    """Only for crops added later without a cycle: the four FAO periods, flagged generic."""
    return CropCycle(
        crop_key=crop_key,
        stages=(
            Stage("initial", "Initial (FAO-56)", "Initiale (FAO-56)", "initial", l_ini, "–", True, ()),
            Stage("development", "Crop development (FAO-56)", "Développement (FAO-56)", "development", l_dev, "–", True, ()),
            Stage("mid_season", "Mid-season (FAO-56)", "Mi-saison (FAO-56)", "mid_season", l_mid, "–", True, ()),
            Stage("late_season", "Late season (FAO-56)", "Arrière-saison (FAO-56)", "late_season", l_late, "–", True, ()),
        ),
        visual_classes={},
        sources=("FAO56",),
        assumptions=("No crop-specific cycle defined yet: FAO-56 periods used. Define one in crop_cycles.py.",),
        generic=True,
    )


def cycle_for(crop_key: str, l_ini: int, l_dev: int, l_mid: int, l_late: int) -> CropCycle:
    return CYCLES.get(crop_key) or fao_fallback(crop_key, l_ini, l_dev, l_mid, l_late)


def need_to_dict(n: Need) -> dict:
    return {"topic": n.topic, "text_en": n.text_en, "text_fr": n.text_fr, "source": n.source}


def stage_to_dict(s: Stage, lang_fr: bool = False) -> dict:
    return {"key": s.key, "name": s.name_fr if lang_fr else s.name_en, "name_en": s.name_en, "name_fr": s.name_fr,
            "fao_period": s.fao_period, "days": s.days, "bbch": s.bbch, "visible": s.visible,
            "visual_cues": list(s.visual_cues), "field_check": s.field_check,
            "description_en": s.description_en, "description_fr": s.description_fr,
            "water_sensitivity": s.water_sensitivity, "needs": [need_to_dict(n) for n in s.needs],
            "max_depletion": s.max_depletion, "max_depletion_source": s.max_depletion_source}


def cycle_to_dict(c: CropCycle) -> dict:
    return {"crop_key": c.crop_key, "generic": c.generic, "length_days": c.length,
            "stages": [stage_to_dict(s) for s in c.stages],
            "visual_classes": {k: list(v) for k, v in c.visual_classes.items()},
            "fao_period_days": c.period_days(), "sources": list(c.sources), "assumptions": list(c.assumptions),
            "thermal": ({"t_base_c": c.thermal.t_base_c, "t_upper_c": c.thermal.t_upper_c,
                         "source": c.thermal.source, "note": c.thermal.note} if c.thermal else None),
            "reference_planting": ("%02d-%02d" % c.reference_planting) if c.reference_planting else None,
            "reference_planting_note": c.reference_planting_note}
