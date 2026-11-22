"""Classic computer vision (no AI model needed): canopy cover, sticky-trap insect count, motion.

These run on any image and give measured values the backend can rely on.
"""
from __future__ import annotations

import cv2
import numpy as np


# ------------------------------------------------------------------ canopy cover
def canopy_cover(img_bgr: np.ndarray) -> dict:
    """Fraction of the image covered by green vegetation.

    Excess Green index on chromatic coordinates (ExG = 2g - r - b, Woebbecke et al. 1995),
    thresholded with Otsu's method; a floor of 0.05 avoids calling grey/brown soil "green"
    when an image has almost no vegetation.
    """
    img = img_bgr.astype(np.float32)
    s = img.sum(axis=2) + 1e-6
    b, g, r = img[..., 0] / s, img[..., 1] / s, img[..., 2] / s
    exg = 2 * g - r - b                                  # -1 .. 2
    exg8 = np.clip((exg + 1) / 3 * 255, 0, 255).astype(np.uint8)
    otsu, _ = cv2.threshold(exg8, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)
    floor = int((0.05 + 1) / 3 * 255)
    mask = exg8 > max(otsu, floor)
    mask = cv2.morphologyEx(mask.astype(np.uint8), cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    pct = float(mask.mean() * 100)
    return {"canopy_cover_pct": round(pct, 1), "kind": "measured",
            "method": "Excess Green index + Otsu threshold"}


# ------------------------------------------------------------------ yellow sticky trap
def count_trap_insects(img_bgr: np.ndarray, min_area_frac: float = 2e-5, max_area_frac: float = 5e-3) -> dict:
    """Count insects caught on a yellow sticky trap.

    1. find the largest yellow region (the trap), 2. inside it, count dark blobs whose
    size is plausible for an insect. Put the trap in front of the camera, flat, in daylight.
    """
    hsv = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2HSV)
    yellow = cv2.inRange(hsv, (22, 120, 150), (38, 255, 255))   # bright, saturated trap yellow
    yellow = cv2.morphologyEx(yellow, cv2.MORPH_CLOSE, np.ones((15, 15), np.uint8))
    contours, _ = cv2.findContours(yellow, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    h, w = yellow.shape
    if not contours:
        return {"trap_found": False, "insect_count": None, "kind": "measured"}
    trap = max(contours, key=cv2.contourArea)
    trap_area = cv2.contourArea(trap)
    if trap_area < 0.05 * h * w:
        return {"trap_found": False, "insect_count": None, "kind": "measured"}
    trap_mask = np.zeros_like(yellow)
    cv2.drawContours(trap_mask, [trap], -1, 255, thickness=cv2.FILLED)
    trap_mask = cv2.erode(trap_mask, np.ones((9, 9), np.uint8))       # ignore the trap's edge
    dark = cv2.inRange(hsv, (0, 0, 0), (180, 255, 110))
    insects = cv2.bitwise_and(dark, trap_mask)
    insects = cv2.morphologyEx(insects, cv2.MORPH_OPEN, np.ones((2, 2), np.uint8))
    n, _, stats, _ = cv2.connectedComponentsWithStats(insects, connectivity=8)
    areas = stats[1:, cv2.CC_STAT_AREA]
    lo, hi = min_area_frac * trap_area, max_area_frac * trap_area
    count = int(((areas >= lo) & (areas <= hi)).sum())
    return {"trap_found": True, "insect_count": count, "trap_area_pct": round(trap_area / (h * w) * 100, 1),
            "kind": "measured", "method": "yellow trap segmentation + dark blob count"}


# ------------------------------------------------------------------ motion (animals at night, intruders)
def motion(prev_bgr: np.ndarray | None, img_bgr: np.ndarray, min_changed_pct: float = 2.0) -> dict:
    if prev_bgr is None or prev_bgr.shape != img_bgr.shape:
        return {"compared": False, "intrusion": False}
    a = cv2.GaussianBlur(cv2.cvtColor(prev_bgr, cv2.COLOR_BGR2GRAY), (21, 21), 0)
    b = cv2.GaussianBlur(cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY), (21, 21), 0)
    # compensate a global brightness change (clouds, dusk) before differencing
    offset = int(np.median(a.astype(int) - b.astype(int)))
    b = np.clip(b.astype(int) + offset, 0, 255).astype(np.uint8)
    diff = cv2.threshold(cv2.absdiff(a, b), 30, 255, cv2.THRESH_BINARY)[1]
    diff = cv2.dilate(diff, np.ones((5, 5), np.uint8), iterations=2)
    pct = float((diff > 0).mean() * 100)
    contours, _ = cv2.findContours(diff, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)
    boxes = [list(cv2.boundingRect(c)) for c in sorted(contours, key=cv2.contourArea, reverse=True)[:5]]
    return {"compared": True, "changed_pct": round(pct, 2), "intrusion": pct >= min_changed_pct,
            "boxes": boxes, "kind": "measured"}
