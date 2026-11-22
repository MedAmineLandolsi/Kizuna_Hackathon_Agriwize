# Photo collection guide (what makes the models reliable)

The public datasets get you started; **your own field photos** make the model work on
Tunisian fields, with your camera, your light and your crop varieties.

## What to photograph
| Model | Classes | Start with |
|---|---|---|
| Crop + stage classifier | `tomato-initial`, `tomato-development`, `tomato-mid_season`, `tomato-late_season`, same for `pepper`, `potato`, `eggplant` and any other crop, plus `other-none` (weeds, bare soil, pipes) | about 100–200 photos per class |
| Pest / damage detector | `tuta_mine` (leaf blotches/tunnels), `tuta_larva`, `whitefly`, `aphid`, `leaf_spot` | about 100–200 labelled examples per class |
| Sticky trap | nothing to train: the insect count is classic image processing | – |

More photos, and more *varied* photos, always help. Check accuracy on the test set
rather than trusting a number of photos.

## How to take them
- **Same camera and distance as in use**: mast photos for the mast option, close-ups
  (20–40 cm) for the carriage and phone options. A model trained on close-ups will not
  work from 2.5 m.
- **Vary conditions**: morning / noon / evening, sun and cloud, wet and dry leaves,
  different plants and plots.
- **Healthy plants too**: the detector must learn what is *not* a pest.
- **Split by plant, not by photo**: all photos of one plant go either to `train` or to
  `test`, never both, or the accuracy will look better than it is.

## Crops that look alike
Tomato, pepper, potato and eggplant belong to the same family and look similar from far.
Take **leaf close-ups** of each (potato: compound leaves of oval leaflets; pepper: smooth,
glossy, single oval leaves; tomato: hairy, serrated compound leaves; eggplant: large fuzzy
lobed leaves), keep the classes balanced, and read the confusion matrix after training.

## Labelling (detector)
Roboflow (free plan) or CVAT: draw a tight box around each mine, larva or insect.
Export in **YOLOv8** format. Ask an agronomist or the regional CRDA to check a sample of
labels: wrong labels teach the model wrong answers.

## Sticky traps
Yellow sticky trap with the *Tuta absoluta* pheromone lure, placed flat in front of a
camera, photographed once a day in daylight. Replace the trap when it is full and send
the next photo with `trap_replaced=true` so the count restarts.

## What to report in the pitch
The measured accuracy from the notebook (mAP50 per class for the detector, top-1
accuracy for the classifier) on photos the model never saw. Do not quote accuracy
figures from papers as if they were yours.
