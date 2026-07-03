# dota2_boot_catcher

Auto-player for the Dota 2 Dark Carnival **Boot Breaker** minigame (position the
cart, lock, aim, throw the boot up to break bricks — repeat each level). Same
approach as the minesweeper bot: full-screen capture + OpenCV detection +
global keyboard input.

## Requirements

- Windows, Dota 2 (1920x1080 works best, like the minesweeper bot).
- Python 3.10+.
- `pip install -r requirements.txt` (opencv-python, numpy, pyautogui, keyboard, pillow).

> The `keyboard` library may need the terminal to be run **as Administrator**
> for its global hotkeys/input to reach Dota.

## How it works

- Captures the screen with **mss** and **auto-scans every monitor** for the
  minigame, so it works with Dota on a second screen / borderless window (the
  common cause of "nothing happens"). Locates the play field by the modal's
  **gold frame**; falls back to a pinned region from `--calibrate`.
- Finds the **cart** (red) and **bricks** (gold) by colour, and tracks the
  level/loading state by the field's **colour saturation** (bright while a level
  is up, near-zero during the between-level loading).
- Per boot: move the cart under the centre of the bricks → Space (lock) →
  Space (throw) → **catch phase**: follow the bouncing boot with the cart so it
  doesn't fall off (miss = lose a boot), until it leaves play or the level ends.
- The boot is found by **motion + colour** (moving orange body / cyan spin arc).
  Requiring motion is deliberate: it ignores the static light-blue platforms in
  level 2+ and the static gold bricks.
- Play control is keyboard-only (A/D/Space); the only mouse use is clicking the
  **PLAY** button (auto-PLAY) to start a run, which also focuses Dota.

## Use

1. Open Boot Breaker (the intro **PLAY** screen or a level - either is fine).
2. In a terminal **run as Administrator** (the `keyboard` library needs it to
   send keys to Dota): `python main.py`
3. Press **`s`** to start. It auto-clicks PLAY if needed and plays on. Hold
   **`q`** to stop, **Ctrl+C** to quit.

> **Multi-monitor / borderless:** capture and clicks use absolute coordinates
> across all monitors, so this works out of the box. If auto-scan picks the
> wrong screen, force it with `--monitor N` (run `--grab` first to see the
> monitor list and which one the field was found on).

### Verify detection first (recommended)

Because detection is colour-based and every setup differs, check it before
trusting it — no keys are pressed:

```powershell
python main.py --dry-run --debug-dir debug
```

Watch the printed `sat=`, `cart=`, `target=` values and the annotated frames in
`debug/`:

- the **red** line should sit on the cart, the **green** line on the middle of
  the bricks;
- `sat` (mean colour saturation) should be **high (~200+) → `READY`** while a
  level is on screen and **drop near 0 → `loading`** during the between-level
  loading screen. That saturation swing is how the bot knows one level ended and
  the next began (so it throws exactly once per level).

If they're off, tune `config.json` (created on first run / `--calibrate`).

## Modes

- `python main.py` — interactive play (`s`/`q`/Ctrl+C).
- `python main.py --grab` — save one screenshot per monitor to `snapshots/` and
  print which monitor the field was found on. Run this first if it can't find
  the game.
- `python main.py --dry-run [--debug-dir DIR]` — detect only, no keys.
- `python main.py --monitor N` — force capture of monitor N (from `--grab`).
- `python main.py --debug` — play and also save annotated frames to `debug/`.
- `python main.py --snapshot NAME` — save one field screenshot to `snapshots/`.
- `python main.py --calibrate` — point at the two gold-frame corners (hold still
  3s each) to pin the field, if auto-detect misbehaves.
- `python main.py --no-auto-play` — don't auto-click PLAY (start it yourself).
- `-v` / `--verbose` — print what each level decides.

## Tuning (`config.json`)

Defaults were measured from real 1920x1080 screenshots, so they should be close.
Most likely to need adjusting if your setup differs:

- `sat_ready` / `sat_ended` — the "level is up" vs "loading" saturation split
  (playing ≈ 230, loading ≈ 0-5, so there's a lot of room). Read live `sat` in
  `--dry-run`.
- `gold_*` / `red_*` HSV ranges — brick and cart colour.
- `field_x0`/`field_x1`/`field_y0`/`field_y1` — where the play field sits inside
  the gold-framed modal (fractions of the modal). Change only if the field crop
  in `--dry-run` is off.
- `brick_top`/`brick_bottom`, `cart_top`/`cart_bottom`, `side_margin` — detection
  bands as fractions of the play field.
- `deadzone_px`, `move_pulse`, `move_timeout` — cart positioning.
- `aim_taps` — D-taps (negative = A) to rotate the launch angle before the
  throw. `0` = launch at the default angle (≈straight up, verified from the
  in-game aim preview).
- `ready_timeout`, `level_end_timeout`, `settle_delay`, `post_lock_delay` — sync
  timing.
- `region` — `[left, top, w, h]` to force a fixed field and skip auto-detect.
- `unpause_f9` — if the field can't be found at startup, tap F9 once (in case
  the game is paused) and retry.

> Geometry and thresholds are validated against real frames, but timings
> (`settle_delay`, `move_*`, `post_lock_delay`) were not tested against a live
> game — run `--dry-run` first and nudge if needed.

## Files

- `main.py` — capture, control loop, hotkeys, utility modes.
- `detect.py` — panel/cart/brick/prompt detection (OpenCV).
- `config.py` — settings dataclass (persisted to `config.json`).
