"""Tunable settings for the Boot Breaker bot (persisted to config.json).

Defaults were measured from real 1920x1080 screenshots of the minigame. Bands
are fractions of the detected PLAY FIELD (the dark area between the gold poles),
not the whole modal.
"""

import json
import os
from dataclasses import dataclass, asdict, field

CONFIG_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


@dataclass
class Config:
    # Optional fixed field region [left, top, w, h] set by --calibrate (absolute
    # virtual-desktop coords). If null the bot auto-detects it from the modal.
    region: list | None = None

    # Which mss monitor to capture: 0 = auto-scan all monitors, 1 = first, etc.
    monitor: int = 0

    # --- Modal auto-detect: bounding box of the gold frame (fractions of screen) ---
    panel_search_x0: float = 0.15
    panel_search_x1: float = 0.85
    panel_search_y0: float = 0.02
    panel_search_y1: float = 0.99
    panel_min_gold: int = 2000
    panel_min_w: int = 350
    panel_min_h: int = 350
    modal_aspect_lo: float = 0.80   # modal is ~square; reject wide UI-noise bboxes
    modal_aspect_hi: float = 1.30

    # --- Play field inside the modal (fractions of the modal; measured) ---
    # The field is the dark play area between the two vertical gold poles.
    field_x0: float = 0.165
    field_x1: float = 0.835
    field_y0: float = 0.11
    field_y1: float = 1.00

    # --- Detection bands (fractions of the play field) ---
    brick_top: float = 0.00
    brick_bottom: float = 0.60
    brick_min_pixels: int = 60
    cart_top: float = 0.86
    cart_bottom: float = 1.00
    side_margin: float = 0.03
    prompt_top: float = 0.60      # only used to draw the prompt band in debug overlays
    prompt_bottom: float = 0.66

    # --- HSV thresholds (OpenCV: H 0-179, S/V 0-255) ---
    gold_h_lo: int = 10
    gold_h_hi: int = 34
    gold_s_min: int = 90
    gold_v_min: int = 110
    red_s_min: int = 90
    red_v_min: int = 60
    cart_min_area: int = 40        # smallest red blob accepted as the cart

    # --- Boot detection during the catch phase ---
    boot_motion_thresh: int = 22    # per-pixel gray change to count as motion
    boot_h_lo: int = 8              # boot body: orange/brown (>=8 avoids the red cart)
    boot_h_hi: int = 36
    boot_s_min: int = 35
    boot_v_min: int = 40
    boot_search_bottom: float = 0.84   # ignore the cart band (cart trim moves too)
    boot_side_margin: float = 0.07     # ignore the animated claw/jester at edges
    boot_min_area: int = 15
    # Shape/size gate (from a level-clearing boot's envelope, 336 frames: area
    # p99=1105 max=1451, width p99=70, aspect max 2.67). Rejects NON-boot blobs
    # that "largest blob" used to grab: brick clusters (~1696px, 66x32) and the
    # aim-trajectory / UI lines (258x3, aspect ~80). The spinning boot stays
    # compact so it passes, and rejecting the big/wide blobs stops the lock-ups.
    boot_max_area: int = 1300
    boot_max_aspect: float = 2.8
    boot_max_w: int = 90

    # --- Boot TRACKER (ROI search + outlier gate, ported from the Rust bot) ---
    # Once locked, search only a box around the boot's predicted position; this
    # is what stops stray orange motion being grabbed as a false, far boot.
    boot_roi_pad: float = 55.0        # base ROI radius (px) around the prediction
    boot_roi_speed: float = 0.05      # + this * boot speed(px/s): faster = bigger box
    boot_roi_lost_grow: float = 22.0  # + this * lost-frames: widen while searching
    boot_gate_base: float = 170.0     # reject a det this many px from the prediction
    boot_gate_lost: float = 55.0      # + this * lost-frames (looser while unsure)
    boot_lost_keep: int = 12          # keep the lock this many lost frames, then re-acquire

    # --- Catch phase (follow the boot with the cart after the throw) ---
    catch_lead: float = 2.0        # frames of boot velocity to aim ahead of it
    catch_smooth: float = 0.45     # low-pass on the target (lower = tracks tighter)
    catch_cart_smooth: float = 0.5 # low-pass on the (noisy) cart reading
    catch_deadzone_frac: float = 0.045  # start moving only past this (>per-tick travel)
    catch_coast_frac: float = 0.025     # cart coasts ~this frac after release; brake early by it
    catch_period: float = 0.005    # control tick (fast; the grab already paces it)
    catch_line: float = 0.82       # boot is "caught" when it reaches this y-frac (cart top)
    catch_predict_top: float = 0.45  # only track once the boot is below this (stable)
    catch_near: float = 0.72       # boot below this -> stay under it
    catch_commit_y: float = 0.80   # boot below this AND cart close -> stop (commit the catch)
    catch_commit_tol: float = 50   # "close" = cart within this many px of the boot
    boot_wall_left: float = 0.02   # boot bounces off these field-x fractions (the poles)
    boot_wall_right: float = 0.98
    catch_min_vy: float = 0.8      # px/tick downward to treat the boot as descending
    boot_lost_secs: float = 1.4    # boot unseen this long => catch phase ends
    catch_timeout: float = 90.0    # hard cap on a single catch phase

    # --- Scene-state sync (mean saturation of the field centre) ---
    # Playing ~= 228, loading/paused ~= 4, so any split in between is safe.
    sat_ready: float = 120.0     # field this saturated AND cart seen => ready
    sat_ended: float = 60.0      # field drops below this => level ended/loading

    # --- Control ---
    # The cart moves ~1120 px/s (measured), so deadzones are a FRACTION of the
    # field width to stay bigger than the per-tick travel (else it oscillates).
    move_deadzone_frac: float = 0.03
    move_pulse: float = 0.02
    move_timeout: float = 1.5      # give up positioning quickly (launch pos isn't critical)
    move_probe_steps: int = 4      # if cart hasn't moved after this many pulses, it's not the LOCK screen
    position_cart: bool = False    # position cart under bricks before locking (risks aim on THROW screen)
    space_hold: float = 0.10    # how long to hold Space (too short = Dota ignores it)
    aim_taps: int = 0           # D-taps (negative = A) to rotate aim before throw
    aim_tap_hold: float = 0.05

    # --- Timing / sync ---
    poll_hz: float = 20.0
    ready_timeout: float = 20.0      # max wait for a level to become ready
    level_end_timeout: float = 15.0  # max wait for the level-end loading dip
    settle_delay: float = 0.7        # pause after a level renders before acting
    post_lock_delay: float = 0.5     # pause between successive Space presses
    throw_max_presses: int = 6       # press Space up to this many times to launch
    launch_wait: float = 1.1         # after a Space, watch this long for a launched boot
    launch_min_y: float = 0.56       # boot must rise ABOVE this y-frac to count.
                                     # The aim-preview dots top out ~0.60 (y~676),
                                     # so 0.56 (y~632) rejects them; but the boot
                                     # only reaches ~0.46 (it hits the bricks), so
                                     # going higher (0.50) made launch confirm flaky
                                     # -> 3 presses -> catch started 4s late.
    launch_min_hits: int = 1         # ...seen this many frames. 1 is enough now
                                     # that launch_min_y rejects the dots by height;
                                     # 2 made confirm slow -> 3 presses -> late catch
    launch_min_area: int = 250       # ...and be a real boot-SIZED blob. A launched
                                     # boot is ~400-700px; small brick/dot fragments
                                     # (<250) were false-triggering the launch (then
                                     # the catch ran on the aim screen and rotated it)
    unpause_f9: bool = False         # tap F9 if the field vanishes (pause popup)
    pause_none_secs: float = 2.0     # field missing this long => assume paused

    # --- Auto-PLAY (click the PLAY button on the intro / end-of-run screen) ---
    auto_play: bool = True
    play_button_yf: float = 0.84     # PLAY button, as a fraction of field height
    intro_secs: float = 1.0          # intro must persist this long before clicking

    # --- Keyboard focus ---
    # Dota's minigame only takes KEYBOARD focus from a REAL mouse click (injected
    # clicks can't). At startup the bot waits this long for the user to click once
    # inside the minigame window; keys can't reach Dota until they do.
    focus_click_wait: float = 8.0


def load_config(path: str = CONFIG_PATH) -> Config:
    cfg = Config()
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for k, v in data.items():
            if hasattr(cfg, k):
                setattr(cfg, k, v)
    return cfg


def save_config(cfg: Config, path: str = CONFIG_PATH) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(asdict(cfg), f, indent=2)
    print(f"Saved config -> {path}")
