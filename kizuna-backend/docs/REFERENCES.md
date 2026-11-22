# Agronomic references used by AGRIWISE

Keys in brackets are used in the code (`sources` fields) and in [CROP_CYCLES.md](CROP_CYCLES.md).
Every value taken from a reference is used as published; values that are our own choices are
listed as **assumptions** next to the data that uses them.

## Water requirements
- **[FAO56]** Allen, R.G., Pereira, L.S., Raes, D., Smith, M. (1998). *Crop evapotranspiration –
  Guidelines for computing crop water requirements.* FAO Irrigation and Drainage Paper 56.
  https://www.fao.org/4/x0490e/x0490e00.htm
  Used: Eq. 6 (Penman-Monteith ET0), Eq. 7–39 (radiation, vapour pressure), Eq. 47 (wind at 2 m),
  Eq. 52 (Hargreaves), Eq. 56 (ETc = Kc × ET0), Eq. 62/65/66 (Kc adjustment and curve),
  Eq. 82–86 (TAW, RAW, Ks, root-zone water balance), Table 11 (stage lengths), Table 12 (Kc, crop
  height), Table 19 (soil water characteristics), Table 22 (rooting depth Zr, depletion fraction p),
  the note that daily rain below about 0.2 ET0 is normally evaporated. Checked by unit tests against
  FAO-56 Examples 8 and 18.
- **[OPEN_METEO]** Open-Meteo forecast API, variable `et0_fao_evapotranspiration` (FAO-56
  Penman-Monteith). https://open-meteo.com/en/docs

## Crop water needs per stage
- **[FAO_CROP_PEPPER]** FAO Land & Water, *Crop information – Pepper* (water relations, from FAO
  Irrigation and Drainage Paper 33, Doorenbos & Kassam 1979).
  https://www.fao.org/land-water/databases-and-software/crop-information/pepper/en/
  Used: start of flowering most sensitive (depletion ≤ 25 % of available water); deficit during yield
  formation → shrivelled/malformed fruit; heavy rain at flowering → flower shedding, at ripening →
  fruit rot; waterlogging → leaf shedding; depletion ≤ 30–40 % of TAW for optimum yield; seedlings
  more salt-sensitive; day 18–27 °C / night 15–18 °C; germination soil 20–24 °C; warmer nights →
  earlier flowering; priority to irrigation up to the first picking when water is limited.
- **[FAO_CROP_POTATO]** FAO Land & Water, *Crop information – Potato*.
  https://www.fao.org/land-water/databases-and-software/crop-information/potato/en/
  Used: stolonization/tuber initiation and yield formation most sensitive, early vegetative and
  ripening less sensitive; depletion 30–50 % of available water; ~70 % of uptake from the top 0.3 m;
  tuber initiation needs nights below 15 °C, optimum mean 18–20 °C; tuber growth sharply inhibited
  below 10 °C and above 30 °C, optimum soil 15–18 °C; frequent irrigation reduces malformation;
  timing can save irrigations including the last before harvest.

## Thermal time (degree-days)
- **[OSU_CROPTIME]** Oregon State University Extension, *Vegetable degree-day models: an introduction
  for farmers and gardeners* (EM 9305) and the Croptime sweet pepper models.
  https://extension.oregonstate.edu/catalog/em-9305-vegetable-degree-day-models-introduction-farmers-gardeners
  Used: sweet pepper lower threshold 52 °F (11.1 °C), upper threshold 100 °F (37.8 °C), single sine
  method with horizontal cutoff. The Croptime stage requirements themselves are NOT used: they are
  calibrated per variety in Oregon.
- **[POTATO_GDD_FL]** *Accumulated growing degree days as a model to determine key developmental
  stages and evaluate yield and quality* (potato, base 7 °C), Proc. Fla. State Hort. Soc. 118: 98–101
  (2005). https://journals.flvc.org/fshs/article/download/85974/82890/107838
  Used: base temperature 7 °C for potato. Its stage values (emergence ≈ 213, full flower ≈ 804 GDD for
  two Florida chipping varieties) are NOT used as requirements, for the same reason.
- **[BASKERVILLE_EMIN]** Baskerville, G.L., Emin, P. (1969). Rapid estimation of heat accumulation from
  maximum and minimum temperatures. *Ecology* 50: 514–517 – single sine method (checked in the tests
  against numerical integration).
- **[OPEN_METEO_ARCHIVE]** Open-Meteo historical weather API (ERA5 reanalysis), daily
  `temperature_2m_max` / `temperature_2m_min`, used for the local climate normals.
  https://open-meteo.com/en/docs/historical-weather-api

**Assumption (documented):** the FAO-56 stage lengths describe the crop planted in its FAO reference
season (pepper: April/June planting → mid-May; potato: Jan/Nov planting in a (semi) arid climate →
mid-January). The degree-days of each stage are measured over that reference season in the local
climate normals; the actual season (sensor, forecast or reanalysis temperatures) is compared with
them. A pepper planted in winter therefore develops more slowly than the FAO calendar; a greenhouse
is seen through its own air-temperature sensor. Day length (photoperiod, important for potato tuber
initiation) is not modelled.

## Crop development stages
- **[BBCH_MONOGRAPH]** Meier, U. (ed.) (2001). *Growth stages of mono- and dicotyledonous plants.*
  BBCH Monograph, 2nd edition. Federal Biological Research Centre for Agriculture and Forestry.
  Licensed CC BY 4.0. https://www.openagrar.de (BBCH Monograph) – general principal stages and the
  potato section.
- **[BBCH_SOLANACEOUS]** Feller, C. et al. (1995). *Phenological growth stages of vegetable crops.
  II. Fruit vegetables and pulses* (BBCH codes for tomato, aubergine, sweet pepper).
  https://www.openagrar.de/servlets/MCRFileNodeServlet/openagrar_derivate_00035701/95.046.pdf
- **[BBCH_POTATO]** BBCH scale for potato (in [BBCH_MONOGRAPH]): principal growth stage 4 "tuber
  formation"; stage 40 "tuber initiation: swelling of first stolon tips to twice the diameter of
  subtending stolon"; 41–48 tuber mass increasing; 48 maximum tuber mass, skin set not complete.
- **[POTATO_STAGES_APS]** Dwelle, R.B., Love, S.L. *Potato growth and development*, adapted from
  *Potato Health Management* (R.C. Rowe, ed., 1993, APS Press): stages sprout development, plant
  establishment (vegetative growth), tuber initiation, tuber bulking, tuber maturation.
- **[POTATO_ABOVE_BELOW]** University of Hohenheim publication on potato phenology under water deficit
  (hohPublica repository): the literature links above- and below-ground potato development (bud
  formation with tuber initiation, flowering with tuber filling) for well-watered crops. AGRIWISE
  therefore uses flowering only as a *proxy* and asks for a field check.
  https://hohpublica.uni-hohenheim.de

## Plant protection (used from step 4)
- **[EPPO_GD]** EPPO Global Database – pest status by country (e.g. *Tuta absoluta* in Tunisia).
  https://gd.eppo.int
- Further phytosanitary references for pepper and potato pests and diseases will be added in step 4,
  with prevention and integrated pest management guidance only (no product doses).

## Datasets (for transfer learning, licences to check before use)
- PlantVillage (leaf images, lab conditions): pepper bacterial spot / healthy; potato early blight,
  late blight, healthy.
- PlantDoc (field images, smaller).
- Public Roboflow datasets for insects (Colorado potato beetle, aphids, whiteflies, thrips).
No model has been trained on these yet: see kizuna-vision/training.
