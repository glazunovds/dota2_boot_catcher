"""
Vision for the Dota 2 Boot Breaker bot.

All detection is colour-based (HSV) so it needs no bundled textures:
  * the play panel is located by its bright-gold ornate frame,
  * the cart by its red body along the bottom,
  * the bricks by their gold colour in the upper area,
  * the "LOCK CART POSITION" / "THROW BOOT" prompt by its near-white text.

Coordinates returned by find_cart_x / find_brick_centroid_x are in *panel*
pixels (relative to the cropped play panel), which is all the controller needs
since it steers with keyboard A/D, not the mouse.
"""

import os
import cv2
import numpy as np

IMAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "images")


# --------------------------------------------------------------------------- #
# HSV colour masks (OpenCV ranges: H 0-179, S 0-255, V 0-255)
# --------------------------------------------------------------------------- #

def _hsv(bgr):
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2HSV)


def gold_mask(hsv, cfg):
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    return ((h >= cfg.gold_h_lo) & (h <= cfg.gold_h_hi) &
            (s >= cfg.gold_s_min) & (v >= cfg.gold_v_min))


def red_mask(hsv, cfg):
    h, s, v = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    return (((h <= 10) | (h >= 160)) & (s >= cfg.red_s_min) & (v >= cfg.red_v_min))


def _side_slice(width, margin_f):
    m = int(width * margin_f)
    return m, width - m


# --------------------------------------------------------------------------- #
# Panel location (auto): bounding box of the gold frame in the screen centre.
# --------------------------------------------------------------------------- #

def locate_modal(screen_bgr, cfg):
    """Bounding box (l, t, w, h) of the whole minigame modal via its gold frame,
    or None. Returns None on the pause overlay (frame is darkened)."""
    H, W = screen_bgr.shape[:2]
    sx0, sx1 = int(W * cfg.panel_search_x0), int(W * cfg.panel_search_x1)
    sy0, sy1 = int(H * cfg.panel_search_y0), int(H * cfg.panel_search_y1)
    roi = screen_bgr[sy0:sy1, sx0:sx1]
    mask = gold_mask(_hsv(roi), cfg).astype(np.uint8) * 255
    mask = cv2.morphologyEx(mask, cv2.MORPH_CLOSE, np.ones((7, 7), np.uint8))
    ys, xs = np.where(mask > 0)
    if len(xs) < cfg.panel_min_gold:
        return None
    # Percentile bounds shrug off stray gold pixels from the HUD/curtains.
    x_lo, x_hi = np.percentile(xs, [0.3, 99.7])
    y_lo, y_hi = np.percentile(ys, [0.3, 99.7])
    left, top = sx0 + int(x_lo), sy0 + int(y_lo)
    w, h = int(x_hi - x_lo), int(y_hi - y_lo)
    if w < cfg.panel_min_w or h < cfg.panel_min_h:
        return None
    # The real modal is ~square. Reject wide/tall bboxes that come from gold UI
    # scattered across a monitor that doesn't actually show the minigame.
    ar = w / max(1, h)
    if not (cfg.modal_aspect_lo <= ar <= cfg.modal_aspect_hi):
        return None
    # Reject a bbox that spans almost the whole search window (gold everywhere).
    if w > 0.92 * (sx1 - sx0) or h > 0.97 * (sy1 - sy0):
        return None
    return (left, top, w, h)


def locate_field(screen_bgr, cfg):
    """Play-field region (l, t, w, h) - the dark area between the gold poles -
    by insetting the modal by the measured field fractions. None if no modal."""
    m = locate_modal(screen_bgr, cfg)
    if m is None:
        return None
    l, t, w, h = m
    fl = int(l + w * cfg.field_x0)
    ft = int(t + h * cfg.field_y0)
    fw = int(w * (cfg.field_x1 - cfg.field_x0))
    fh = int(h * (cfg.field_y1 - cfg.field_y0))
    return (fl, ft, fw, fh)


def scene_saturation(field_bgr):
    """Mean HSV saturation of the field centre. High (~230) while playing,
    very low (~5) during the between-level loading dip."""
    h, w = field_bgr.shape[:2]
    c = field_bgr[int(h * 0.20):int(h * 0.85), int(w * 0.20):int(w * 0.80)]
    return float(cv2.cvtColor(c, cv2.COLOR_BGR2HSV)[:, :, 1].mean())


# --------------------------------------------------------------------------- #
# In-panel detections
# --------------------------------------------------------------------------- #

def _band(panel_bgr, top_f, bot_f):
    h = panel_bgr.shape[0]
    return panel_bgr[int(h * top_f):int(h * bot_f)]


def find_cart_x(panel_bgr, cfg):
    """Horizontal centre of the red cart, in panel pixels, or None.

    Uses the largest red blob's centroid (the cart's wide red top bar). This is
    far steadier than a column peak, which drifted when the bar's red split
    around the cart's blue window."""
    band = _band(panel_bgr, cfg.cart_top, cfg.cart_bottom)
    red = red_mask(_hsv(band), cfg).astype(np.uint8) * 255
    x0, x1 = _side_slice(panel_bgr.shape[1], cfg.side_margin)
    red[:, :x0] = 0
    red[:, x1:] = 0
    red = cv2.morphologyEx(red, cv2.MORPH_CLOSE, np.ones((5, 5), np.uint8))
    n, _, stats, cent = cv2.connectedComponentsWithStats(red, 8)
    best = None
    for i in range(1, n):
        area = stats[i][4]
        if area < cfg.cart_min_area:
            continue
        if best is None or area > best[1]:
            best = (cent[i], area)
    if best is None:
        return None
    return int(round(best[0][0]))


def find_brick_centroid_x(panel_bgr, cfg):
    """Centre-of-mass X of the gold bricks, in panel pixels, or None."""
    band = _band(panel_bgr, cfg.brick_top, cfg.brick_bottom)
    hsv = _hsv(band)
    gold = gold_mask(hsv, cfg)
    x0, x1 = _side_slice(panel_bgr.shape[1], cfg.side_margin)
    cols = gold[:, x0:x1].sum(axis=0).astype(np.float32)
    total = cols.sum()
    if total < cfg.brick_min_pixels:
        return None
    centroid = (np.arange(len(cols)) * cols).sum() / total
    return int(round(x0 + centroid))


def to_gray(bgr):
    return cv2.cvtColor(bgr, cv2.COLOR_BGR2GRAY)


def find_boot(panel_bgr, prev_gray, cfg, roi=None, gray=None, min_area=None,
              exclude=None, cart_x=None):
    """Locate the in-flight boot. Returns ((x, y, area) or None, gray).

    The boot is found as *moving* pixels that are boot-orange (its body). Two
    things matter here:
      * motion excludes the static gold bricks and the static level-2 blue
        platforms;
      * using ONLY the orange body (not the cyan spin arc) excludes the RED cart
        and its cyan window - which move while catching and were being mistaken
        for the boot.
    The cart band is cropped out because the cart's own gold trim moves too.

    If `roi=(x0, y0, x1, y1)` (panel px) is given, ONLY that box is searched.
    Like the reference Rust bot, restricting the search to a box around the
    boot's predicted path is what keeps stray orange motion (UI, aim dots, the
    jester) from being picked up as a false, far-away "boot". The side margins
    stay ON even inside a ROI: replay showed that without them a ROI drifting
    to a field edge locks onto the animated claw/jester there and never lets
    go (rally 3/7 tails). They do slice a pole-hugging boot to a sliver
    (~0.3% of frames) - the relaxed aspect cap plus the 12-fresh-frame lock
    budget ride that out instead.

    `gray` may pass a precomputed grayscale of panel_bgr; `min_area` raises the
    minimum blob size (used for full-field re-acquisition, where tiny UI trim /
    falling pickups would otherwise be grabbed while the gate is off).
    `exclude` is a list of (x, y, radius) dead spots - candidates centred there
    are skipped (used to blacklist shimmer blocks that stole the lock).
    `cart_x` (panel px): when the cart position is known, the search extends
    down to boot_search_bottom_ext with only a cart-sized rectangle masked,
    instead of cutting the whole band - the final ~100px of a descent is where
    fast boots deflect, and the whole-band cut made the endgame blind.
    """
    if gray is None:
        gray = to_gray(panel_bgr)
    if prev_gray is None or prev_gray.shape != gray.shape:
        return None, gray
    h, w = gray.shape
    motion = cv2.absdiff(gray, prev_gray) >= cfg.boot_motion_thresh
    motion = cv2.dilate(motion.astype(np.uint8), np.ones((5, 5), np.uint8))
    hsv = _hsv(panel_bgr)
    hh, ss, vv = hsv[:, :, 0], hsv[:, :, 1], hsv[:, :, 2]
    orange = ((hh >= cfg.boot_h_lo) & (hh <= cfg.boot_h_hi) &
              (ss >= cfg.boot_s_min) & (vv >= cfg.boot_v_min))
    boot = ((motion > 0) & orange).astype(np.uint8) * 255
    cut = int(h * cfg.boot_search_bottom)
    if cart_x is None:
        boot[cut:] = 0                                # ignore the whole cart band
    else:                                             # deep search, cart masked
        boot[int(h * cfg.boot_search_bottom_ext):] = 0
        cx0 = max(0, int(cart_x - cfg.cart_mask_halfw))
        cx1 = min(w, int(cart_x + cfg.cart_mask_halfw))
        boot[cut:, cx0:cx1] = 0
    m = int(w * cfg.boot_side_margin)                 # ignore the animated
    boot[:, :m] = 0                                    # claw/jester/pole at the
    boot[:, w - m:] = 0                                # field edges
    if roi is not None:                               # restrict to the ROI box
        x0, y0, x1, y1 = roi
        x0 = max(0, int(x0)); y0 = max(0, int(y0))
        x1 = min(w, int(x1)); y1 = min(h, int(y1))
        keep = np.zeros_like(boot)
        if x1 > x0 and y1 > y0:
            keep[y0:y1, x0:x1] = boot[y0:y1, x0:x1]
        boot = keep
    boot = cv2.morphologyEx(boot, cv2.MORPH_OPEN, np.ones((3, 3), np.uint8))
    n, _, stats, cent = cv2.connectedComponentsWithStats(boot, 8)
    area_lo = max(cfg.boot_min_area, min_area or 0)
    passing = []
    for i in range(1, n):
        _x, _y, bw, bh, area = stats[i]
        if area < area_lo or area > cfg.boot_max_area:
            continue                          # too small = noise, too big = brick cluster
        asp = max(bw, bh) / max(1, min(bw, bh))
        if asp > cfg.boot_max_aspect or bw > cfg.boot_max_w:
            continue                          # wide/thin = aim-trajectory or UI line
        if exclude and any((cent[i][0] - ex) ** 2 + (cent[i][1] - ey) ** 2 < er * er
                           for ex, ey, er in exclude):
            continue                          # blacklisted dead spot (shimmer block)
        passing.append((cent[i], int(area), stats[i]))
    if not passing:
        return None, gray
    # IDENTITY: the flying boot spins and always carries a BRIGHT cyan arc that
    # moves with it; no gold junk (coins, popups, sparkles, debris, shimmer,
    # trim) has one, and the pale static cyan block on levels 2+ fails the
    # saturation + motion test. Keep only arc-bearing candidates; if NO
    # candidate has an arc, there is no boot in view. (Validated on 531
    # labeled blobs across 8 recorded runs: 34/34 junk rejected, ~100% of true
    # boot detections kept - the rare arc-less "boot" dets were themselves
    # wrong-blob picks that this filter re-selects correctly.)
    arc = ((motion > 0) & (hh >= cfg.boot_arc_h_lo) & (hh <= cfg.boot_arc_h_hi) &
           (ss >= cfg.boot_arc_s_min) & (vv >= cfg.boot_arc_v_min))
    pad = cfg.boot_arc_pad
    with_arc = []
    for c, area, st in passing:
        x0 = max(0, st[0] - pad)
        y0 = max(0, st[1] - pad)
        x1 = min(w, st[0] + st[2] + pad)
        y1 = min(h, st[1] + st[3] + pad)
        if int(arc[y0:y1, x0:x1].sum()) >= cfg.boot_arc_min:
            with_arc.append((c, area))
    if not with_arc:
        return None, gray
    # Among arc-bearing candidates pick the LARGEST blob (nearest-to-prediction
    # selection was tried and reverted; the identity filter now does the work
    # that size/kinematic heuristics approximated).
    best = max(with_arc, key=lambda c: c[1])
    return (int(best[0][0]), int(best[0][1]), best[1]), gray


# --------------------------------------------------------------------------- #
# Debug overlay
# --------------------------------------------------------------------------- #

def annotate(panel_bgr, cfg, cart_x, target_x, metric, state, boot=None):
    out = panel_bgr.copy()
    h, w = out.shape[:2]

    def hline(frac, color):
        y = int(h * frac)
        cv2.line(out, (0, y), (w, y), color, 1)

    for f in (cfg.brick_top, cfg.brick_bottom):
        hline(f, (0, 120, 0))
    for f in (cfg.cart_top, cfg.cart_bottom):
        hline(f, (0, 0, 160))
    for f in (cfg.prompt_top, cfg.prompt_bottom):
        hline(f, (160, 160, 0))
    x0, x1 = _side_slice(w, cfg.side_margin)
    cv2.line(out, (x0, 0), (x0, h), (90, 90, 90), 1)
    cv2.line(out, (x1, 0), (x1, h), (90, 90, 90), 1)
    if cart_x is not None:
        cv2.line(out, (cart_x, int(h * cfg.cart_top)), (cart_x, h), (0, 0, 255), 2)
    if target_x is not None:
        cv2.line(out, (target_x, 0), (target_x, int(h * cfg.brick_bottom)), (0, 255, 0), 2)
    if boot is not None:
        cv2.circle(out, (int(boot[0]), int(boot[1])), 14, (255, 255, 0), 2)
    txt = f"{state}  metric={metric}  cart={cart_x}  target={target_x}"
    cv2.putText(out, txt, (6, 20), cv2.FONT_HERSHEY_SIMPLEX, 0.55, (255, 255, 255), 1, cv2.LINE_AA)
    return out
