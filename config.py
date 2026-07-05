"""Tunable settings for the Boot Breaker bot (persisted to config.json).

Defaults were measured from real 1920x1080 screenshots of the minigame. Bands
are fractions of the detected PLAY FIELD (the dark area between the gold poles),
not the whole modal.
"""

import json
import os
import sys
from dataclasses import dataclass, asdict, field


def app_dir():
    """Directory config/logs/debug live in: next to the .exe when frozen
    (PyInstaller unpacks __file__ into a temp dir), else next to the source."""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


CONFIG_PATH = os.path.join(app_dir(), "config.json")


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
    # When the CART POSITION IS KNOWN (catch phase), search deeper and mask only
    # a rectangle around the cart instead of amputating the whole band: the boot
    # is visible down to ~0.93H, and those last ~100px are where fast descents
    # deflect (run_20260704b miss: the boot was at (440,979) while the masked
    # tracker steered to a stale 483 - the frames had the answer all along).
    boot_search_bottom_ext: float = 0.93
    cart_mask_halfw: float = 125.0     # half-width of the cart mask rectangle
    boot_side_margin: float = 0.07     # ignore the animated claw/jester at edges
    boot_min_area: int = 35         # Rust-bot parity; the GO!-arrow trim (15px)
                                    # was accepted as a "boot" below this
    # Shape/size gate (from a level-clearing boot's envelope, 336 frames: area
    # p99=1105 max=1451, width p99=70, aspect max 2.67). Rejects NON-boot blobs
    # that "largest blob" used to grab: brick clusters (~1696px, 66x32) and the
    # aim-trajectory / UI lines (258x3, aspect ~80). The spinning boot stays
    # compact so it passes, and rejecting the big/wide blobs stops the lock-ups.
    # Ceiling 1300 (validated): raising it to 1600 to cover the measured
    # true-boot max (1451) let a 1258px shimmer-block/brick blob steal the lock
    # at a rally's end - the clipped boot tail is the cheaper loss.
    boot_max_area: int = 1300
    boot_max_aspect: float = 3.2    # Rust parity; wall-clipped boots hit ~3.0-3.3
    boot_max_w: int = 90
    # --- Boot IDENTITY: the cyan spin arc (validated on 531 labeled blobs from
    # 8 recorded runs). The flying boot ALWAYS spins and carries a thin BRIGHT
    # cyan arc (H 85-110, S>=120, V>=150) that moves with it; no gold junk
    # (coins/popups/sparkles/debris/shimmer/trim) has one - 34/34 junk rejected,
    # ~100% boot kept. The indestructible cyan BLOCK on levels 2+ is PALE
    # (S~75) and static, so the saturation bound + motion coupling exclude it.
    boot_arc_h_lo: int = 85
    boot_arc_h_hi: int = 110
    boot_arc_s_min: int = 120
    boot_arc_v_min: int = 150
    boot_arc_min: int = 8           # min moving-arc pixels near a candidate; a
                                    # candidate set with NO arc anywhere = no boot
    boot_arc_pad: int = 10          # count arc pixels in the blob bbox + this
    # Full-field RE-ACQUISITION needs a bigger floor than in-ROI tracking: with
    # no lock the gate is off, and the run_20260703 forensics showed the only
    # wrong grabs were the GO!-arrow trim (15-63px) and falling gold pickups
    # (90-243px) - a real boot in open flight is 374-731px.
    reacquire_min_area: int = 250
    # ...but a HEALTHY lock (lost<=2, fresh prediction) may keep itself alive on
    # smaller blobs: a fast boot fragments below 250 and a flat floor blinded
    # the tracker at decisive moments (run_20260704e rallies 0/12 died in such
    # blind windows). Coins/debris steals need the lost>=3 window, which keeps
    # the big floor.
    track_keep_min_area: int = 120
    # ...for at most this many CONSECUTIVE small dets: a chain of small accepts
    # keeps lost at 0, so a coin-fall can sustain a "healthy" lock forever
    # (run_20260704f boot 9: 17 straight 154-199px dets rode coins from y=297
    # to 759 while the boot flew elsewhere). Small dets may BRIDGE a lock, not
    # live on it - the boot re-anchors with a >=250px det within 1-3 frames,
    # coins never can.
    small_keep_max: int = 4

    # --- Boot TRACKER (ROI search + outlier gate, ported from the Rust bot) ---
    # Once locked, search only a box around the boot's predicted position; this
    # is what stops stray orange motion being grabbed as a false, far boot.
    boot_roi_pad: float = 55.0        # base ROI radius (px) around the prediction
    boot_roi_speed: float = 0.05      # + this * boot speed(px/s): faster = bigger box
    boot_roi_lost_grow: float = 50.0  # + this * lost-frames: widen while searching
                                      # (22 was too slow for a 60fps boot at
                                      # ~2500px/s - the ROI trailed it by up to
                                      # 875px during launch/bounce reversals)
    boot_gate_base: float = 170.0     # reject a det this many px from the prediction
    boot_gate_lost: float = 55.0      # + this * lost-frames (looser while unsure)
    boot_lost_keep: int = 12          # keep the lock this many lost frames, then re-acquire
                                      # (FRESH frames only - stale captures don't count)
    # Velocity/coast sanity (from run_20260703: real boot tops out ~1300 px/s,
    # but dividing a relock residual by one 36ms tick injected up to 5956 px/s
    # and coasted the track to y=3439 - clamping + freezing removed every
    # out-of-field runaway offline with zero good relocks rejected).
    boot_vmax: float = 4000.0         # clamp smoothed |velocity| (px/s). Sized
                                      # for 60fps play: game speed == render fps,
                                      # so the boot runs ~3x its 20fps speeds
                                      # (measured max ~1300 at 15-20fps). The old
                                      # 1500 clamp SATURATED at 45-60fps and made
                                      # the prediction trail the boot (0.5s blind
                                      # gaps). Garbage velocities can't happen
                                      # anymore - only arc-verified boot dets
                                      # feed the tracker - so this is just an
                                      # insanity guard now.
    boot_freeze_lost: int = 4         # stop coasting (hold position) after this
                                      # many fresh lost frames
    frame_stale_eps: float = 0.05     # mean gray absdiff below this = the game
                                      # hasn't rendered a new frame; skip the tick
    # --- Impossible-track breakers (run_20260704 failure: some gold blocks have
    # an ANIMATED shine, so they pass motion+color and can steal the lock; the
    # boot then flies on while the tracker rides a static block / walks down a
    # shimmering column). Both rules validated on the recorded telemetry:
    # the static rule fired 4x in 11 old rallies (all at confuser spots, never
    # at catch-line contacts or bounce apexes), the column rule fired ONLY on
    # the actual gold-block column rides in each run.
    brk_static_dets: int = 5          # this many consecutive dets ...
    brk_static_box: float = 14.0      # ... inside a box this size ...
    brk_static_secs: float = 0.25     # ... spanning at least this long = not a boot
    brk_column_dets: int = 12         # this many consecutive dets ...
    brk_column_xrange: float = 20.0   # ... in a column this narrow ...
    brk_column_descent: float = 250.0 # ... descending this far = shine wave, not boot
    brk_column_edge: float = 80.0     # column rule only for interior x (pole-hugging
                                      # boot descents are real and near-vertical)
    # Coin-ride breaker: coin falls produce dets of MIXED size (122-440px, the
    # 250-440 clusters defeat any per-det floor) but their rolling MEDIAN stays
    # ~200 while real boot flight sits ~550 - and they DESCEND steadily, which
    # separates them from wall-pocket sliver hovering (validated on 3 runs:
    # catches every known fatal ride, fires in winning rallies mostly on real
    # transient rides that got lucky).
    brk_coin_dets: int = 5            # rolling window (consecutive accepted dets)
    brk_coin_med: float = 300.0       # median area below this ...
    brk_coin_descent: float = 60.0    # ... while net descent exceeds this = coins
    # The cyan-arc identity filter keeps junk OUT of the detection stream, so
    # the kinematic junk rules (coin-ride median, column walk) can now only
    # fire on the REAL boot (a fragmented fast descent matches their profile).
    # They flip from protection to hazard - off by default. The static-lock
    # breaker stays on (a mid-air boot is never static; it guards against any
    # unforeseen arc-bearing confuser).
    brk_junk_kinematics: bool = False
    blacklist_r: float = 45.0         # after a break/lock-drop, ignore detections
    blacklist_secs: float = 1.5       # this close to the dead spot for this long

    # --- Catch phase (follow the boot with the cart after the throw) ---
    catch_lead: float = 2.0        # frames of boot velocity to aim ahead of it
    catch_smooth: float = 0.45     # low-pass on the target (lower = tracks tighter)
    catch_cart_smooth: float = 0.5 # low-pass on the (noisy) cart reading
    # Steering constants retuned for 60fps play (46Hz loop, per-tick cart travel
    # ~13-26px vs the 27-55px they were originally sized for). Simulation over
    # 573 recorded final descents: this set halves marginal landings (crossings
    # ending >55px from the boot: 90 -> 32) with negligible added chatter.
    catch_cart_lookahead: float = 0.01  # damp overshoot by steering to where the
                                        # cart will be after this much of its own
                                        # momentum. MUST stay small: reversal needs
                                        # cart_vel > deadzone*1.5/lookahead, far
                                        # above cart max, so it can only RELEASE
                                        # early, never reverse the wrong way. 0.03
                                        # over-led the boot at 60fps (biggest
                                        # single gap contributor); 0.0 worsens the
                                        # p95 tail. 0.01 is the sweet spot.
    catch_deadzone_frac: float = 0.032  # start moving only past this (>per-tick travel)
    catch_coast_frac: float = 0.020     # cart coasts ~this frac after release; brake early by it
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
    catch_timeout: float = 600.0   # safety cap only. 90s truncated HEALTHY rallies
                                   # mid-air (the phase has reliable end signals:
                                   # sat fade, boot-gone 1.4s, top-exit breakthrough)
    # Breakthrough = level complete: the boot punches through the bricks and
    # exits the TOP of the field (the game fades 1.3-1.4s later). Recognize it
    # instead of booking it as "boot lost".
    exit_top_y: float = 60.0       # last det above this y (px) ...
    exit_top_vy: float = -150.0    # ... while ascending faster than this ...
    exit_top_lost: int = 6         # ... unseen this many fresh frames => exited

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
