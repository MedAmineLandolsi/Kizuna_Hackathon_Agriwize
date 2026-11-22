# Crop development cycles (AGRIWISE)

Generated from `app/agronomy/crop_cycles.py` by `tools/make_crop_cycles_doc.py` – do not edit by hand.

Each crop has its **own** cycle: stages, durations, BBCH codes, visual cues, field checks and, for
pepper and potato, the documented needs of every stage. Stage durations add up, period by period,
to the crop's FAO-56 lengths (Table 11) so that Kc and root depth follow the same stage.
Source keys in brackets are listed in [REFERENCES.md](REFERENCES.md).

**How the current stage is estimated:** planting date (or the farmer's last confirmation) → calendar
of the crop's own cycle → corrected by the degree-days of the season compared with the local climate
normals (pepper, potato) → checked against the last photo → confirmed by the farmer (always required
for underground potato stages).

## Pepper  (FAO56, FAO_CROP_PEPPER, BBCH_SOLANACEOUS, BBCH_MONOGRAPH, OSU_CROPTIME)

Degree-days: base 11.1 °C, upper cutoff 37.8 °C, single sine (OSU_CROPTIME). Reference season: planting on 15/05 – FAO-56 Table 11 pepper lengths are for April/June planting: mid-May taken as the reference date (assumption).

| # | Stage (EN) | Stade (FR) | BBCH | FAO-56 period | Days | Visible on a photo | Water-deficit sensitivity (FAO) | Field check |
|---|---|---|---|---|---|---|---|---|
| 1 | Germination and plant establishment | Germination et établissement du plant | BBCH 00–1x (germination, first true leaves; transplant establishment) | initial | 25 | yes | not singled out by FAO | – |
| 2 | Vegetative growth | Croissance végétative | BBCH 1x–2x (leaf development, side shoots) | development | 35 | yes | not singled out by FAO | – |
| 3 | Flowering | Floraison | BBCH 5x–6x (flower buds, open flowers) | mid_season | 15 | yes | **critical** | Count open flowers on 5 plants. |
| 4 | Fruit set and fruit development | Nouaison et développement des fruits | BBCH 7x (fruit development) | mid_season | 25 | yes | sensitive | – |
| 5 | Ripening and harvest | Maturation et récolte | BBCH 8x (fruit colouring/ripening) | late_season | 20 | yes | not singled out by FAO | – |

### Stage descriptions and needs

**Germination and plant establishment / Germination et établissement du plant** – Seedling or freshly transplanted plant building its first roots and true leaves. Roots are shallow, so the plant depends on the top layer of soil.

- (water) Shallow root zone (about 0.25 m at the start): light, frequent irrigations keep it moist. [FAO_CROP_PEPPER]
- (salinity) Seedlings are more sensitive to soil salinity than mature plants: watch the EC. [FAO_CROP_PEPPER]
- (temperature) Optimal soil temperature for germination: 20–24 °C (direct seeding). [FAO_CROP_PEPPER]

**Vegetative growth / Croissance végétative** – Leaves and side shoots multiply; the canopy and the root system grow fast, so the water demand rises day after day.

- (water) Crop coefficient rises from 0.6 to 1.05 during this stage: water needs increase with the canopy. [FAO56]
- (water) Waterlogging, even for short periods, causes leaf shedding: avoid over-irrigation. [FAO_CROP_PEPPER]
- (temperature) Good growth with day temperatures of 18–27 °C and nights of 15–18 °C. [FAO_CROP_PEPPER]

**Flowering / Floraison** – Flower buds and open white flowers. Each flower that drops is a lost fruit, so this is the stage where water stress costs the most.

- (water) Start of flowering is the most sensitive period: soil water depletion should not exceed 25 % of the available water. [FAO_CROP_PEPPER]
- (rain) Heavy rain during flowering causes flower shedding and poor fruit set. [FAO_CROP_PEPPER]
- (temperature) Warmer nights bring flowering forward. [FAO_CROP_PEPPER]
- Irrigation engine: depletion limited to 25% of TAW in this stage [FAO_CROP_PEPPER].

**Fruit set and fruit development / Nouaison et développement des fruits** – Fruits set and grow to full size while the upper nodes may still flower. Peak water use.

- (water) Water deficit during yield formation gives shrivelled, malformed fruit. [FAO_CROP_PEPPER]
- (water) Mid-season crop coefficient 1.05 (before climate adjustment). [FAO56]
- (water) For optimum yield, depletion should not exceed 30–40 % of the total available water. [FAO_CROP_PEPPER]

**Ripening and harvest / Maturation et récolte** – Fruits change colour (red, yellow or orange) and are picked. Water use decreases slowly.

- (rain) Heavy rain during ripening causes fruit rot. [FAO_CROP_PEPPER]
- (water) If water is limited, give priority to irrigation up to the first picking. [FAO_CROP_PEPPER]
- (water) End-of-season crop coefficient 0.9. [FAO56]

Camera classes → compatible stages: `seedling` → establishment; `leafy_no_flowers` → vegetative; `flowers` → flowering; `green_fruits` → fruit_set; `coloured_fruits` → ripening

Assumptions:
- FAO-56 lengths for April/June planting, Mediterranean: 25/35/40/20 days.
- The FAO mid-season (40 d) is split into flowering 15 d + fruit set 25 d (assumption; pepper keeps flowering while fruits develop).
- Degree-day thresholds 11.1 °C / 37.8 °C (52 °F / 100 °F) come from the Oregon Croptime sweet pepper models; they are used only to compare the season with the local normals, not as stage requirements.

## Potato  (FAO56, FAO_CROP_POTATO, BBCH_POTATO, BBCH_MONOGRAPH, POTATO_STAGES_APS, POTATO_ABOVE_BELOW, POTATO_GDD_FL)

Degree-days: base 7.0 °C, upper cutoff 30.0 °C, single sine (POTATO_GDD_FL, FAO_CROP_POTATO). Reference season: planting on 15/01 – FAO-56 Table 11 potato lengths used are for Jan/Nov planting in a (semi) arid climate: mid-January taken as the reference date (assumption).

| # | Stage (EN) | Stade (FR) | BBCH | FAO-56 period | Days | Visible on a photo | Water-deficit sensitivity (FAO) | Field check |
|---|---|---|---|---|---|---|---|---|
| 1 | Sprouting and emergence | Germination et levée | BBCH 00–09 (sprouting, emergence) | initial | 25 | yes | less sensitive | – |
| 2 | Vegetative development | Développement végétatif | BBCH 1x–3x (leaves, basal side shoots, stem elongation / crop cover) | development | 30 | yes | less sensitive | – |
| 3 | Tuber initiation | Initiation et tubérisation | BBCH 40 (tuber initiation) | mid_season | 15 | **no (underground)** | **critical** | Dig one plant: tuber initiation = first stolon tips swollen to twice the stolon diameter (BBCH 40). |
| 4 | Tuber bulking | Grossissement des tubercules | BBCH 41–48 (tuber mass increasing) | mid_season | 30 | **no (underground)** | **critical** | Dig one plant: tubers clearly growing; weigh a sample of tubers every week. |
| 5 | Maturation and senescence | Maturation et sénescence | BBCH 48–49 and 9x (skin set, senescence) | late_season | 30 | yes | less sensitive | Rub a tuber: skin set when the skin no longer comes off with the thumb. |

### Stage descriptions and needs

**Sprouting and emergence / Germination et levée** – The seed tuber sprouts and the shoots break through the soil. The speed depends mostly on soil temperature.

- (water) Early vegetative periods are less sensitive to water deficit than tuber initiation and yield formation. [FAO_CROP_POTATO]
- (water) Initial crop coefficient 0.5. [FAO56]

**Vegetative development / Développement végétatif** – Leaves, stems and side shoots develop until the rows close. Stolons start to form below ground.

- (water) About 70 % of the water is taken from the top 0.3 m of soil: keep that layer supplied. [FAO_CROP_POTATO]
- (water) Crop coefficient rises from 0.5 to 1.15 as the canopy closes. [FAO56]

**Tuber initiation / Initiation et tubérisation** – Stolon tips swell into the first tubers. It happens underground: it cannot be seen on a photo of the leaves and is checked by digging one plant.

- (water) Stolonization and tuber initiation are among the periods where water deficit has the greatest effect on yield. [FAO_CROP_POTATO]
- (temperature) Tuber initiation needs night temperatures below 15 °C; optimum mean daily temperature 18–20 °C. [FAO_CROP_POTATO]

**Tuber bulking / Grossissement des tubercules** – Tubers fill and gain most of the yield. Also underground: progress is checked by weighing a sample of tubers.

- (water) Yield formation is the other most sensitive period; keep depletion within 30–50 % of the available water. [FAO_CROP_POTATO]
- (water) Frequent irrigation reduces tuber malformation. [FAO_CROP_POTATO]
- (temperature) Tuber growth is sharply inhibited below 10 °C and above 30 °C; optimal soil temperature 15–18 °C. [FAO_CROP_POTATO]

**Maturation and senescence / Maturation et sénescence** – Leaves yellow and vines die back while the tuber skin sets. Water use falls.

- (water) Ripening is less sensitive to water deficit; correct timing can save irrigations, including the last one before harvest. [FAO_CROP_POTATO]
- (water) End-of-season crop coefficient 0.75. [FAO56]

Camera classes → compatible stages: `emergence` → sprouting_emergence; `leafy_canopy` → vegetative, tuber_initiation, tuber_bulking; `flowering` → tuber_initiation, tuber_bulking; `senescence` → maturation

Assumptions:
- FAO-56 lengths for Jan/Nov planting, (semi) arid climate: 25/30/45/30 days (mid-season 45 d option).
- The FAO mid-season (45 d) is split into tuber initiation 15 d + tuber bulking 30 d (assumption).
- Tuber initiation and bulking happen underground: a photo can only give a proxy (canopy, flower buds, flowering). The link between above- and below-ground development is described for well-watered crops and depends on the variety; the stage is confirmed by digging a plant.
- Degree-days: base 7 °C as in the potato growing-degree-day study [POTATO_GDD_FL], horizontal cutoff at 30 °C, above which FAO reports tuber growth sharply inhibited [FAO_CROP_POTATO]. Day length also affects tuber initiation and is NOT modelled.

## Tomato  (FAO56, BBCH_SOLANACEOUS, BBCH_MONOGRAPH)

Degree-days: no documented thresholds yet → calendar only.

| # | Stage (EN) | Stade (FR) | BBCH | FAO-56 period | Days | Visible on a photo | Water-deficit sensitivity (FAO) | Field check |
|---|---|---|---|---|---|---|---|---|
| 1 | Germination and plant establishment | Germination et établissement du plant | BBCH 00–1x | initial | 30 | yes | not documented yet | – |
| 2 | Vegetative growth | Croissance végétative | BBCH 1x–2x | development | 40 | yes | not documented yet | – |
| 3 | Flowering (first trusses) | Floraison (premiers bouquets) | BBCH 5x–6x | mid_season | 15 | yes | not documented yet | – |
| 4 | Fruit set and fruit development | Nouaison et développement des fruits | BBCH 7x | mid_season | 30 | yes | not documented yet | – |
| 5 | Ripening and harvest | Maturation et récolte | BBCH 8x | late_season | 30 | yes | not documented yet | – |

Camera classes → compatible stages: `seedling` → establishment; `leafy_no_flowers` → vegetative; `flowers` → flowering; `green_fruits` → fruit_set; `coloured_fruits` → ripening

Assumptions:
- FAO-56 lengths for April/May planting, Mediterranean: 30/40/45/30 days.
- Mid-season split: flowering 15 d + fruit set 30 d (assumption).
- Stage needs and degree-day thresholds not documented yet in AGRIWISE: calendar only.

## Eggplant  (FAO56, BBCH_SOLANACEOUS, BBCH_MONOGRAPH)

Degree-days: no documented thresholds yet → calendar only.

| # | Stage (EN) | Stade (FR) | BBCH | FAO-56 period | Days | Visible on a photo | Water-deficit sensitivity (FAO) | Field check |
|---|---|---|---|---|---|---|---|---|
| 1 | Germination and plant establishment | Germination et établissement du plant | BBCH 00–1x | initial | 30 | yes | not documented yet | – |
| 2 | Vegetative growth | Croissance végétative | BBCH 1x–2x | development | 45 | yes | not documented yet | – |
| 3 | Flowering | Floraison | BBCH 5x–6x | mid_season | 15 | yes | not documented yet | – |
| 4 | Fruit set and fruit development | Nouaison et développement des fruits | BBCH 7x | mid_season | 25 | yes | not documented yet | – |
| 5 | Ripening and harvest | Maturation et récolte | BBCH 8x | late_season | 25 | yes | not documented yet | – |

Camera classes → compatible stages: `seedling` → establishment; `leafy_no_flowers` → vegetative; `flowers` → flowering; `green_fruits` → fruit_set; `coloured_fruits` → ripening

Assumptions:
- FAO-56 lengths for May/June planting, Mediterranean: 30/45/40/25 days.
- Mid-season split: flowering 15 d + fruit set 25 d (assumption).
- Stage needs and degree-day thresholds not documented yet in AGRIWISE: calendar only.
