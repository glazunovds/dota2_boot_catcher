"""
Dota 2 Boot Breaker bot.

Auto-plays the Dark Carnival "Boot Breaker" minigame (position the cart under
the bricks, lock, aim, throw the boot, repeat). mss capture + OpenCV detection +
global keyboard input. Multi-monitor aware (auto-scans monitors for the game).

Usage
-----
  python main.py                 interactive: press 's' to start, hold 'q' to stop
  python main.py --grab           save one screenshot per monitor (debug), then exit
  python main.py --dry-run        print detections + save debug frames, no keys
  python main.py --calibrate      pin the field by pointing at the gold frame
  python main.py --monitor N      force capture of monitor N (1 = first)
  python main.py --snapshot NAME  save one field screenshot to snapshots/
  python main.py --debug          save annotated frames while playing

How to play
-----------
1. Open the Boot Breaker minigame (the intro PLAY screen or a level is fine).
2. Run `python main.py`, then press 's'. It auto-clicks PLAY and plays on.
   Hold 'q' to stop, Ctrl+C to quit.

Keyboard input goes to whatever window is focused; the auto-PLAY click focuses
Dota for you. Detection is colour-based - verify with `--dry-run` first and tune
config.json if needed (see README). The `keyboard` library usually needs the
terminal run as Administrator to reach Dota.
"""

import argparse
import ctypes
from ctypes import wintypes
import os
import sys
import threading
import time

import cv2
import numpy as np
import mss
import keyboard

import detect
from config import Config, load_config, save_config

running = False       # True while a game is being played
stop_flag = True      # keeps the 'q' listener thread alive
_held = None          # currently held movement key ('a'/'d') or None
_dbg_dir = None       # set to a folder path to dump annotated frames
_manual = False       # --manual: never click / grab foreground; user owns focus
_dbg_n = 0
_sct = None           # lazily-opened mss capture handle
_log_f = None         # optional log file handle
_t0 = time.time()


def log(msg):
    line = f"[{time.time() - _t0:7.2f}s] {msg}"
    print(line, flush=True)
    if _log_f is not None:
        _log_f.write(line + "\n")
        _log_f.flush()


def set_dpi_aware():
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)   # per-monitor v1
    except Exception:
        try:
            ctypes.windll.user32.SetProcessDPIAware()
        except Exception:
            pass


# --------------------------------------------------------------------------- #
# Capture (mss; multi-monitor aware, uses absolute virtual-desktop coords)
# --------------------------------------------------------------------------- #

def _sct_handle():
    global _sct
    if _sct is None:
        _sct = (getattr(mss, "MSS", None) or mss.mss)()
    return _sct


def grab_region(region):
    """Grab an absolute virtual-desktop region -> BGR array. region=(l,t,w,h)."""
    l, t, w, h = (int(v) for v in region)
    shot = _sct_handle().grab({"left": l, "top": t, "width": w, "height": h})
    return np.ascontiguousarray(np.asarray(shot)[:, :, :3])   # BGRA -> BGR


def monitors():
    # mss.monitors[0] is the whole virtual desktop; [1:] are the real monitors.
    return _sct_handle().monitors


def find_game_region(cfg):
    """Absolute (l, t, w, h) of the play field, scanning monitors if needed."""
    if cfg.region:
        return tuple(int(v) for v in cfg.region)
    mons = monitors()
    search = [mons[cfg.monitor]] if cfg.monitor and cfg.monitor < len(mons) else mons[1:]
    best = None                                  # (region, score)
    for mon in search:
        img = grab_region((mon["left"], mon["top"], mon["width"], mon["height"]))
        r = detect.locate_field(img, cfg)
        if r is None:
            continue
        l, t, w, h = r
        field = img[t:t + h, l:l + w]
        sat = detect.scene_saturation(field)
        region = (mon["left"] + l, mon["top"] + t, w, h)
        # Several monitors can hold gold that passes the frame test. DON'T just
        # take the first-scanned one - a stray, dim gold region on the (first-
        # scanned) second monitor was shadowing the real game on the ultrawide.
        # Prefer the match that looks like the LIVE minigame: brightly saturated
        # AND large. (sat ~230 while playing; a false/dim match ~10-20.)
        score = sat * w * h
        if best is None or score > best[1]:
            best = (region, score)
    return best[0] if best else None


# Backwards-friendly alias used by the modes.
def get_panel_region(cfg, screen=None):
    return find_game_region(cfg)


# --------------------------------------------------------------------------- #
# Mouse (ctypes; works across monitors, unlike pyautogui)
# --------------------------------------------------------------------------- #

class _POINT(ctypes.Structure):
    _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]


# --- SendInput plumbing: absolute virtual-desktop mouse. A synthetic click only
# gives a game keyboard focus if it lands on the exact pixel and reads as a real
# click; the old mouse_event (no ABSOLUTE flag, 30ms) left Dota active-looking
# but focus-less, so the user had to click manually. SendInput with
# ABSOLUTE|VIRTUALDESK coords + a real hold fixes that.
_user32 = ctypes.windll.user32
ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong
INPUT_MOUSE = 0
MOUSEEVENTF_MOVE = 0x0001
MOUSEEVENTF_LEFTDOWN = 0x0002
MOUSEEVENTF_LEFTUP = 0x0004
MOUSEEVENTF_ABSOLUTE = 0x8000
MOUSEEVENTF_VIRTUALDESK = 0x4000
SM_XVIRTUALSCREEN, SM_YVIRTUALSCREEN = 76, 77
SM_CXVIRTUALSCREEN, SM_CYVIRTUALSCREEN = 78, 79


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUT(ctypes.Structure):
    class _U(ctypes.Union):
        _fields_ = [("mi", _MOUSEINPUT)]
    _anonymous_ = ("u",)
    _fields_ = [("type", wintypes.DWORD), ("u", _U)]


_user32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(_INPUT), ctypes.c_int]
_user32.SendInput.restype = wintypes.UINT
_user32.GetSystemMetrics.argtypes = [ctypes.c_int]
_user32.GetSystemMetrics.restype = ctypes.c_int
_user32.GetCursorPos.argtypes = [ctypes.POINTER(_POINT)]


def cursor_pos():
    p = _POINT()
    _user32.GetCursorPos(ctypes.byref(p))
    return (p.x, p.y)


def _abs_coords(x, y):
    """Map a virtual-desktop pixel to 0..65535 normalized coords (what
    MOUSEEVENTF_ABSOLUTE|VIRTUALDESK expects, spanning ALL monitors)."""
    gsm = _user32.GetSystemMetrics
    vx, vy = gsm(SM_XVIRTUALSCREEN), gsm(SM_YVIRTUALSCREEN)
    vw, vh = max(1, gsm(SM_CXVIRTUALSCREEN)), max(1, gsm(SM_CYVIRTUALSCREEN))
    nx = max(0, min(65535, int(((x - vx) * 65535 + (vw - 1)) // vw)))
    ny = max(0, min(65535, int(((y - vy) * 65535 + (vh - 1)) // vh)))
    return nx, ny


def _mk_mouse(flags, nx=0, ny=0):
    inp = _INPUT()
    inp.type = INPUT_MOUSE
    inp.mi = _MOUSEINPUT(nx, ny, 0, flags, 0, 0)
    return inp


def _send(*inputs):
    arr = (_INPUT * len(inputs))(*inputs)
    return _user32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT)) == len(inputs)


def move_mouse(x, y):
    nx, ny = _abs_coords(x, y)
    if not _send(_mk_mouse(MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny)):
        _user32.SetCursorPos(int(x), int(y))


def click_at(x, y):
    """Move to (x, y) and click via SendInput absolute virtual-desktop coords, so
    the click lands on the exact pixel and Dota routes WM_MOUSEACTIVATE (real
    keyboard focus). Caller re-asserts foreground ONCE afterward, so it doesn't
    race/fight the click's own activation."""
    nx, ny = _abs_coords(x, y)
    move = MOUSEEVENTF_MOVE | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK
    _send(_mk_mouse(move, nx, ny))
    time.sleep(0.08)
    _send(_mk_mouse(MOUSEEVENTF_LEFTDOWN | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))
    time.sleep(0.06)
    _send(_mk_mouse(MOUSEEVENTF_LEFTUP | MOUSEEVENTF_ABSOLUTE | MOUSEEVENTF_VIRTUALDESK, nx, ny))


def park_cursor():
    move_mouse(10, 10)   # top-left of the primary screen, off the game


# --------------------------------------------------------------------------- #
# Window focus (keyboard input only reaches the FOREGROUND window)
# --------------------------------------------------------------------------- #

GA_ROOT = 2
_hwnd = 0
_child_hwnd = 0        # deepest window under the field centre (may be the CEF/
                       # Panorama input window that actually needs keyboard focus)
_win32_ready = False


def _init_win32():
    global _win32_ready
    if _win32_ready:
        return
    u = ctypes.windll.user32
    # Default ctypes restype is c_int (32-bit) which truncates HWNDs on 64-bit.
    u.WindowFromPoint.restype = ctypes.c_void_p
    u.WindowFromPoint.argtypes = [_POINT]
    u.GetAncestor.restype = ctypes.c_void_p
    u.GetAncestor.argtypes = [ctypes.c_void_p, ctypes.c_uint]
    u.GetForegroundWindow.restype = ctypes.c_void_p
    u.GetForegroundWindow.argtypes = []
    u.SetForegroundWindow.restype = wintypes.BOOL
    u.SetForegroundWindow.argtypes = [ctypes.c_void_p]
    u.BringWindowToTop.restype = wintypes.BOOL
    u.BringWindowToTop.argtypes = [ctypes.c_void_p]
    u.AttachThreadInput.restype = wintypes.BOOL
    u.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
    u.GetWindowThreadProcessId.restype = wintypes.DWORD
    u.GetWindowThreadProcessId.argtypes = [ctypes.c_void_p, ctypes.POINTER(wintypes.DWORD)]
    u.SetActiveWindow.restype = ctypes.c_void_p
    u.SetActiveWindow.argtypes = [ctypes.c_void_p]
    u.SetFocus.restype = ctypes.c_void_p
    u.SetFocus.argtypes = [ctypes.c_void_p]
    u.keybd_event.argtypes = [ctypes.c_ubyte, ctypes.c_ubyte, wintypes.DWORD, ULONG_PTR]
    u.GetGUIThreadInfo.restype = wintypes.BOOL
    u.GetGUIThreadInfo.argtypes = [wintypes.DWORD, ctypes.POINTER(_GUITHREADINFO)]
    ctypes.windll.kernel32.GetCurrentThreadId.restype = wintypes.DWORD
    _win32_ready = True


class _GUITHREADINFO(ctypes.Structure):
    _fields_ = [("cbSize", wintypes.DWORD), ("flags", wintypes.DWORD),
                ("hwndActive", wintypes.HWND), ("hwndFocus", wintypes.HWND),
                ("hwndCapture", wintypes.HWND), ("hwndMenuOwner", wintypes.HWND),
                ("hwndMoveSize", wintypes.HWND), ("hwndCaret", wintypes.HWND),
                ("rcCaret", wintypes.RECT)]


def keyboard_focus_state():
    """(hwndFocus, hwndActive) in Dota's GUI thread - the REAL keyboard-focus
    window (not just foreground). This is what tells us if SetFocus actually
    worked, and whether input goes to _hwnd or to a child (CEF/Panorama) window."""
    if not _hwnd:
        return (None, None)
    u = ctypes.windll.user32
    tid = u.GetWindowThreadProcessId(_hwnd, None)
    gti = _GUITHREADINFO()
    gti.cbSize = ctypes.sizeof(_GUITHREADINFO)
    if not u.GetGUIThreadInfo(tid, ctypes.byref(gti)):
        return (None, None)
    return (gti.hwndFocus, gti.hwndActive)


def dota_has_kb_focus():
    """True iff the REAL keyboard-focus window belongs to Dota (its root, the
    input child, or any descendant of the root). This is the honest check that
    injected keys will actually land - being 'foreground' is not enough."""
    foc, _act = keyboard_focus_state()
    if not foc:
        return False
    if foc == _hwnd or foc == _child_hwnd:
        return True
    root = ctypes.windll.user32.GetAncestor(foc, GA_ROOT)
    return bool(root) and root == _hwnd


def refresh_hwnd(region):
    """Find the top-level game window under the play field's centre (and the
    deepest child there, which may be the real keyboard-input window)."""
    global _hwnd, _child_hwnd
    _init_win32()
    u = ctypes.windll.user32
    cx = int(region[0] + region[2] // 2)
    cy = int(region[1] + region[3] // 2)
    hwnd = u.WindowFromPoint(_POINT(cx, cy))
    _child_hwnd = hwnd or 0
    if hwnd:
        root = u.GetAncestor(hwnd, GA_ROOT)
        _hwnd = root or hwnd
    return _hwnd


def focus_game(tries=4):
    """Give Dota real KEYBOARD focus (not just foreground). Being the foreground
    window is NOT enough for a game - injected keys only land once the window
    has keyboard focus (what a real click grants). So we ALWAYS run the full
    dance: an Alt-key tap (satisfies the foreground-lock rule), then under
    AttachThreadInput to Dota's GUI thread call SetForegroundWindow +
    SetActiveWindow + SetFocus. The old code returned early when Dota was already
    foreground and so never called SetFocus - that was the 'foreground=Dota but
    cart won't move' bug. Returns True if Dota ends up foreground."""
    if not _hwnd:
        return False
    u = ctypes.windll.user32
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    for _ in range(tries):
        tgt = u.GetWindowThreadProcessId(_hwnd, None)
        fg = u.GetForegroundWindow()
        fgt = u.GetWindowThreadProcessId(fg, None) if fg else 0
        att_fg = att_tgt = False
        if fgt and fgt != cur:
            att_fg = bool(u.AttachThreadInput(cur, fgt, True))
        if tgt and tgt != cur:
            att_tgt = bool(u.AttachThreadInput(cur, tgt, True))
        try:
            u.BringWindowToTop(_hwnd)
            u.SetForegroundWindow(_hwnd)
            u.SetActiveWindow(_hwnd)
            u.SetFocus(_hwnd)                 # <-- the missing keyboard-focus call
            if _child_hwnd and _child_hwnd != _hwnd:
                u.SetFocus(_child_hwnd)       # the CEF/Panorama input child, if any
        finally:
            if att_tgt:
                u.AttachThreadInput(cur, tgt, False)
            if att_fg:
                u.AttachThreadInput(cur, fgt, False)
        if u.GetForegroundWindow() == _hwnd:
            return True
        time.sleep(0.06)
    return u.GetForegroundWindow() == _hwnd


def focus_click(region):
    """Reliably focus Dota by clicking a harmless spot on its window - Windows
    blocks background apps from SetForegroundWindow, but a real click always
    focuses the window under it. Clicks the HUD strip above the play field
    (BOOTS/LEVEL/SCORE - not a button), then parks the cursor off-screen."""
    l, t, w, h = region
    x = int(l + w * 0.5)
    y = int(t + h * 0.30)      # INSIDE the play field (harmless during menu/level)
    click_at(x, y)             # (unused now; focus is set by the user's real click)
    time.sleep(0.15)
    park_cursor()


# --------------------------------------------------------------------------- #
# Keyboard control (global; goes to the focused Dota window)
# --------------------------------------------------------------------------- #

# --- Key send. PostMessage delivers WM_KEYDOWN/UP straight to the Dota window's
# message queue, which does NOT require keyboard focus - if the minigame handles
# window key-messages this sidesteps the whole focus fight (no click needed). The
# `keyboard` library (SendInput) is the fallback; it needs the window focused.
_use_post = True
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
_VK = {'a': 0x41, 'd': 0x44, 'space': 0x20, 'f9': 0x78}
_SC = {'a': 0x1E, 'd': 0x20, 'space': 0x39, 'f9': 0x43}
_post_ready = False


def _post_key(name, up):
    """Send a key to Dota by posting WM_KEYDOWN/WM_KEYUP to its window."""
    global _post_ready
    u = ctypes.windll.user32
    if not _post_ready:
        u.PostMessageW.argtypes = [ctypes.c_void_p, ctypes.c_uint, ctypes.c_size_t, ctypes.c_ssize_t]
        u.PostMessageW.restype = ctypes.c_int
        _post_ready = True
    tgt = _child_hwnd or _hwnd
    if not tgt:
        return
    vk, sc = _VK.get(name, 0), _SC.get(name, 0)
    if up:
        lparam = 1 | (sc << 16) | (1 << 30) | (1 << 31)   # prev-down + key-up transition
        u.PostMessageW(tgt, WM_KEYUP, vk, lparam)
    else:
        lparam = 1 | (sc << 16)                            # repeat 1, scancode
        u.PostMessageW(tgt, WM_KEYDOWN, vk, lparam)


def _key_down(name):
    _post_key(name, False) if _use_post else keyboard.press(name)


def _key_up(name):
    _post_key(name, True) if _use_post else keyboard.release(name)


def hold(key):
    global _held
    if _held == key:
        return
    release_held()
    _key_down(key)
    _held = key


def release_held():
    global _held
    if _held is not None:
        _key_up(_held)
        _held = None


def tap(key, hold=0.10):
    """Press a key and hold it briefly. A zero-length tap is often ignored by
    Dota, so we hold for `hold` seconds (default 100 ms)."""
    _key_down(key)
    time.sleep(hold)
    _key_up(key)


# --------------------------------------------------------------------------- #
# Debug frame dump
# --------------------------------------------------------------------------- #

def snap(region, cfg, label, cart_x=None, target_x=None, boot=None):
    """Grab the field and (if --debug) save an annotated frame. Returns
    (sat, cart_x) so callers can log the scene state cheaply."""
    global _dbg_n
    panel = grab_region(region)
    sat = detect.scene_saturation(panel)
    if cart_x is None:
        cart_x = detect.find_cart_x(panel, cfg)
    if _dbg_dir:
        img = detect.annotate(panel, cfg, cart_x, target_x, int(sat), label, boot)
        cv2.imwrite(os.path.join(_dbg_dir, f"f{_dbg_n:04d}_{label}.png"), img)
        _dbg_n += 1
    return sat, cart_x


# --------------------------------------------------------------------------- #
# Control loop
# --------------------------------------------------------------------------- #

def move_cart(region, target_x, cfg):
    """Position the cart on the LOCK screen. It PROBES first: the cart is very
    fast, so if a few A/D pulses don't move it, we're not on the LOCK screen
    (A/D is rotating the aim, or the cart is locked) - stop immediately so we
    don't wreck the launch aim."""
    dead = cfg.move_deadzone_frac * int(region[2])
    deadline = time.time() + cfg.move_timeout
    last_cart = None
    start_cart = None
    steps = 0
    while running and time.time() < deadline:
        panel = grab_region(region)
        cart = detect.find_cart_x(panel, cfg)
        if cart is None:
            log(f"    move: cart not seen (target={target_x}) - stopping")
            break
        if start_cart is None:
            start_cart = cart
        last_cart = cart
        err = target_x - cart
        if abs(err) <= dead:
            break
        # Probe: after a few pulses, a LOCK-screen cart has clearly moved
        # (~200px). If it hasn't, we're on the aim/throw screen -> stop now.
        if steps >= cfg.move_probe_steps and abs(cart - start_cart) < 8:
            log("    not the LOCK screen (cart didn't move) - skipping positioning")
            break
        hold('d' if err > 0 else 'a')
        steps += 1
        time.sleep(cfg.move_pulse)
    release_held()
    log(f"    moved cart -> {last_cart} (target {target_x}, {steps} pulses)")


def do_aim(cfg):
    taps = cfg.aim_taps
    if not taps:
        return
    key = 'd' if taps > 0 else 'a'
    for _ in range(abs(taps)):
        if not running:
            break
        keyboard.press(key)
        time.sleep(cfg.aim_tap_hold)
        keyboard.release(key)
        time.sleep(0.04)


def click_play(region, cfg):
    """Click the PLAY button (intro / end-of-run screen) to start a game.
    Does NOT call focus_game(): its SetForegroundWindow/SetFocus dance can't give
    real keyboard focus but CAN knock the user's manual focus loose (cursor jumps
    away, keys stop landing). The synthetic click still presses the button; the
    user's keyboard focus is left untouched."""
    l, t, w, h = region
    x, y = int(l + w * 0.5), int(t + h * cfg.play_button_yf)
    click_at(x, y)
    time.sleep(0.15)
    park_cursor()
    foc, _act = keyboard_focus_state()
    log(f"  clicked PLAY at ({x},{y}); kbFocus={foc}")


def wait_ready(region, cfg):
    """Wait until the field is saturated (rendered) AND a cart is visible,
    i.e. a level is ready for input. Clicks PLAY if it sees the intro/menu.
    Returns True, or False on timeout/stop."""
    period = 1.0 / max(1.0, cfg.poll_hz)
    deadline = time.time() + cfg.ready_timeout
    intro_since = None
    last_click = 0.0
    prev_gray = detect.to_gray(grab_region(region))   # prime motion baseline
    while running and time.time() < deadline:
        field = grab_region(region)
        sat = detect.scene_saturation(field)
        cart = detect.find_cart_x(field, cfg)
        boot, prev_gray = detect.find_boot(field, prev_gray, cfg)
        # Ready to lock only if a level is rendered, the cart is visible, and no
        # boot is still in flight (otherwise we'd lock/throw mid-bounce).
        if sat >= cfg.sat_ready and cart is not None and boot is None:
            time.sleep(cfg.settle_delay)
            snap(region, cfg, "ready")
            log(f"  READY (sat={sat:.0f} cart={cart})")
            return True
        # Intro / menu (a PLAY button): mid saturation, no cart -> press PLAY.
        now = time.time()
        if cfg.auto_play and cart is None and cfg.sat_ended <= sat < cfg.sat_ready:
            intro_since = intro_since or now
            if now - intro_since >= cfg.intro_secs and now - last_click >= 3.0:
                click_play(region, cfg)
                last_click = now
                intro_since = None
        else:
            intro_since = None
        time.sleep(period)
    return False


def play_level(region, cfg, verbose):
    if not _manual:      # manual mode: the user owns focus/foreground entirely
        bring_foreground()   # PostMessage keys only work when Dota is active
    # 1) optionally position the cart under the bricks. OFF by default: A/D only
    # moves the cart on the LOCK screen; on the THROW screen it rotates the aim,
    # and the two screens can't be told apart reliably, so positioning risks
    # wrecking the (good) default up-ish launch. The launch spot barely matters
    # since the boot bounces everywhere anyway.
    if cfg.position_cart:
        panel = grab_region(region)
        target_x = detect.find_brick_centroid_x(panel, cfg) or int(region[2]) // 2
        log(f"  positioning cart -> {target_x}")
        move_cart(region, target_x, cfg)
    s, c = snap(region, cfg, "pre_lock")
    if s < cfg.sat_ready:
        # The level ended/transitioned while we were positioning - bail so we
        # don't lock/throw into a loading screen.
        log(f"  aborting boot: scene changed (sat={s:.0f})")
        return
    # 1.5) If a boot is ALREADY airborne (the game auto-served, or one carried
    # over from a just-cleared level), pressing Space mis-fires the throw - skip
    # straight to catching it. (This is the 'Space while boot midair' case.)
    if boot_in_flight(region, cfg):
        log("  boot already airborne - catching without a throw")
        catch_phase(region, cfg)
        return
    # 2) lock + throw: exactly TWO Space presses (lock, then throw), then watch.
    # NO launch confirmation: run_20260704c proved the confirm loop is worse
    # than useless - the real throw happens on press 1-2 and the boot flies
    # UNWATCHED while boot_launched polls; the animated "+50" score popups from
    # that very flight (gold, up to 1248px, at y~560 - above the launch_min_y
    # line) then false-confirm the launch on press 4-5, and catch_phase starts
    # on an EMPTY field, chasing popups. If a press gets eaten (level fade-in),
    # the catch phase just sees nothing for boot_lost_secs (~1.4s) and the main
    # loop comes straight back here for another pair - a ~4s recovery beats an
    # unwatched flight every time.
    snap(region, cfg, "space")
    log("  Space (lock)")
    tap('space', cfg.space_hold)
    time.sleep(cfg.post_lock_delay)
    if not running:
        return
    snap(region, cfg, "space")
    log("  Space (throw) - catching")
    tap('space', cfg.space_hold)

    # 3) catch phase: follow the boot with the cart until it's gone
    catch_phase(region, cfg)


def boot_in_flight(region, cfg, window=0.4):
    """True if a REAL boot is already airborne and MOVING - so the bot should NOT
    press Space to throw. Requires a gated, boot-sized (>=launch_min_area) blob
    that actually TRAVELS over the window. The on-cart boot is static (no motion
    -> undetected) and the aim dots are gated/too-small, so neither trips this."""
    prev = detect.to_gray(grab_region(region))
    cut = int(region[3]) * cfg.boot_search_bottom
    seen = []
    end = time.time() + window
    while running and time.time() < end:
        panel = grab_region(region)
        boot, prev = detect.find_boot(panel, prev, cfg)
        if boot is not None and boot[2] >= cfg.launch_min_area and boot[1] < cut:
            seen.append(boot)
        time.sleep(0.03)
    if len(seen) < 3:
        return False
    xs = [p[0] for p in seen]
    ys = [p[1] for p in seen]
    return (max(xs) - min(xs) > 25) or (max(ys) - min(ys) > 25)


def bring_foreground():
    """Best-effort: make Dota the ACTIVE/foreground window. PostMessage'd keys are
    only processed by the minigame when its window is active (they were dropped in
    a run where the terminal had focus). Keyboard focus is NOT needed - PostMessage
    doesn't use it - so this can't hurt like the old focus dance did. From a
    background process SetForegroundWindow is subject to the foreground-lock, so
    it's best-effort; if keys still don't land, keep Dota in front manually."""
    if not _hwnd:
        return
    u = ctypes.windll.user32
    if u.GetForegroundWindow() == _hwnd:
        return
    cur = ctypes.windll.kernel32.GetCurrentThreadId()
    fg = u.GetForegroundWindow()
    fgt = u.GetWindowThreadProcessId(fg, None) if fg else 0
    att = bool(u.AttachThreadInput(cur, fgt, True)) if (fgt and fgt != cur) else False
    u.BringWindowToTop(_hwnd)
    u.SetForegroundWindow(_hwnd)
    if att:
        u.AttachThreadInput(cur, fgt, False)


def boot_launched(region, cfg, timeout):
    """Confirm a throw by watching for a launched boot risen HIGH into the field,
    boot-sized, seen over frames. (Reverted from a movement-based check that made
    it stricter.) Requiring height + area rejects the animating aim-preview dots;
    if the boot isn't confirmed the loop just presses Space again."""
    limit = int(region[3]) * cfg.launch_min_y
    prev = detect.to_gray(grab_region(region))
    deadline = time.time() + timeout
    hits = 0
    while running and time.time() < deadline:
        panel = grab_region(region)
        boot, prev = detect.find_boot(panel, prev, cfg)
        if boot is not None and boot[1] < limit and boot[2] >= cfg.launch_min_area:
            hits += 1
            if hits >= cfg.launch_min_hits:
                return True
        time.sleep(0.02)
    return False


def steer(err, cfg, field_w):
    """Move the cart toward an x-error. Two measured facts drive this:
      * the cart travels ~27-55px per control tick, so it only STARTS moving when
        the error exceeds a deadzone (a fraction of the field) - else it jitters;
      * after the key is released the cart COASTS ~21px, so it releases that far
        BEFORE the target and lets momentum carry it precisely onto the boot,
        instead of overshooting and reversing.
    Hysteresis (start at `dead`, stop at `coast`) keeps it from chattering."""
    dead = cfg.catch_deadzone_frac * field_w
    coast = cfg.catch_coast_frac * field_w
    switch = dead * 1.5                 # must exceed this to reverse direction
    if _held == 'd':
        if err < -switch:
            hold('a')
        elif err < coast:               # momentum will carry it the rest -> release
            release_held()
    elif _held == 'a':
        if err > switch:
            hold('d')
        elif err > -coast:
            release_held()
    else:
        if err > dead:
            hold('d')
        elif err < -dead:
            hold('a')


def predict_landing(bx, by, vx, vy, left, right, catch_y):
    """Where the boot will cross the cart line, bouncing off the side walls.
    Straight-line (arcade) physics, per-frame velocity units. None if ascending."""
    if vy <= 0:
        return None
    x, y, vxx = float(bx), float(by), float(vx)
    guard = 0
    while y < catch_y and guard < 2000:
        x += vxx
        y += vy
        if x <= left:
            x = left + (left - x)
            vxx = -vxx
        elif x >= right:
            x = right - (x - right)
            vxx = -vxx
        guard += 1
    return max(left, min(right, x))


def catch_phase(region, cfg):
    """Follow the boot with the cart. Ported from the reference Rust catcher,
    which is reliable because of its TRACKER, not clever steering:
      * search for the boot only inside a small ROI around its predicted path
        (so stray orange motion - UI, aim dots, the jester - can't be grabbed
        as a far-away false boot);
      * GATE detections: reject anything implausibly far from the prediction and
        coast on the estimate instead;
      * then simply steer the cart toward the boot's LAST SEEN x (pure pursuit,
        no velocity lead / landing projection - those chased noisy per-tick
        predictions and overshot the wrong way at contact).
    """
    global _dbg_n
    w, h = int(region[2]), int(region[3])
    prev_gray = None
    _tel = []                        # per-tick telemetry -> debug/catch_tel.csv
    bx = by = None                   # tracked boot position (px), None = no lock
    bvx = bvy = 0.0                  # tracked boot velocity (px/s)
    last_t = None
    det_prev = None                  # (t, x, y) of the last ACCEPTED detection -
                                     # velocity is measured between accepted dets,
                                     # NOT against the coasted state (dividing a
                                     # relock residual by one 36ms tick injected
                                     # up to 5956 px/s and ran the coast off-field)
    lost = 0                         # consecutive FRESH frames without an accepted det
    seen = 0                         # consecutive accepted dets (tracker age)
    ever_seen = False
    breakthrough = False             # boot exited the TOP = level complete
    det_hist = []                    # consecutive accepted dets (t, x, y) for the
                                     # impossible-track breakers
    blacklist = []                   # (x, y, t_until) dead spots find_boot must skip
    small_run = 0                    # consecutive accepted dets under the boot-size
                                     # floor (coin chains must not sustain a lock)
    target_x = None                  # boot's last SEEN x = the steer target
    cx = None                        # smoothed cart x
    cx_prev = None                   # for cart-velocity (overshoot damping)
    cart_vx = 0.0                    # cart velocity, px/s
    t_cx = time.time()
    start = time.time()
    last_seen_wall = time.time()
    n = 0
    while running and time.time() - start < cfg.catch_timeout:
        panel = grab_region(region)
        now = time.time()
        sat = detect.scene_saturation(panel)
        if sat < cfg.sat_ended:                       # level ended (loading dip)
            log(f"  catch: level ended (loading dip){' after breakthrough' if breakthrough else ''}")
            break

        # 0) Stale-capture check: the bot polls ~28/s but the game renders at
        # 15-20fps, so ~1/3 of grabs are pixel-identical to the previous render.
        # Those ticks carry NO information - running detection is a guaranteed
        # miss (motion mask empty) and counting them as "lost" burned the
        # 12-frame lock budget in 0.43s of wall time. Skip them entirely: no
        # detection, no lost++, no coasting (the boot didn't move either).
        gray = detect.to_gray(panel)
        stale = (prev_gray is not None and prev_gray.shape == gray.shape and
                 float(cv2.absdiff(gray, prev_gray).mean()) < cfg.frame_stale_eps)
        det = None
        cart = None
        if not stale:
            cart = detect.find_cart_x(panel, cfg)
            if cart is not None:                      # smooth the noisy cart read
                new_cx = cart if cx is None else cfg.catch_cart_smooth * cx + (1 - cfg.catch_cart_smooth) * cart
                if cx_prev is not None and now > t_cx:
                    cart_vx = 0.5 * cart_vx + 0.5 * (new_cx - cx_prev) / (now - t_cx)
                cx = new_cx
                cx_prev = new_cx
                t_cx = now

            # 1) Predict where the boot should be now, and (if locked) search only
            #    a box around it. No lock yet -> search the whole field, with a
            #    boot-sized area floor (the gate is off while seen<2, and tiny UI
            #    trim / falling gold pickups seeded every phantom lock on record).
            tracking = bx is not None and lost <= cfg.boot_lost_keep
            pred_x = pred_y = None
            roi = None
            if tracking and last_t is not None:
                dt = now - last_t
                pred_x = bx + bvx * dt
                pred_y = by + bvy * dt
                speed = (bvx * bvx + bvy * bvy) ** 0.5
                radius = cfg.boot_roi_pad + speed * cfg.boot_roi_speed + lost * cfg.boot_roi_lost_grow
                roi = (pred_x - radius, pred_y - radius, pred_x + radius, pred_y + radius)
            blacklist = [b for b in blacklist if now < b[2]]
            # Size floor with hysteresis. Every lock THIEF on record is small
            # (coins 104-238px, debris 81-199, trim 15-63) while the flying boot
            # is 450-714 - so acquiring a lock (or keeping one that's been blind
            # a while) demands a boot-sized blob. But a HEALTHY lock (lost<=2,
            # fresh prediction) may feed on smaller fragments: a fast boot
            # splinters below 250 and a flat floor blinded the tracker at the
            # decisive moment twice in run_20260704e. Steals need the lost>=3
            # window, which keeps the big floor.
            # ...and small dets may only BRIDGE a lock, not sustain it: a chain
            # of small accepts keeps lost at 0, so falling coins rode a
            # "healthy" lock for 17 straight dets once. After small_keep_max
            # consecutive smalls the floor snaps back to boot-sized until a
            # real >=250px det re-anchors the track.
            min_a = cfg.track_keep_min_area if (
                tracking and lost <= 2 and small_run < cfg.small_keep_max) \
                else cfg.reacquire_min_area
            det, prev_gray = detect.find_boot(
                panel, prev_gray, cfg, roi, gray=gray, min_area=min_a,
                exclude=[(bx_, by_, cfg.blacklist_r) for bx_, by_, _ in blacklist],
                cart_x=cart if cart is not None else cx)

            # 2) Gate: reject a detection implausibly far from the prediction.
            if det is not None and pred_x is not None and seen >= 2 and lost <= 6:
                dist = ((det[0] - pred_x) ** 2 + (det[1] - pred_y) ** 2) ** 0.5
                speed = (bvx * bvx + bvy * bvy) ** 0.5
                gate = cfg.boot_gate_base + speed * 0.12 + lost * cfg.boot_gate_lost
                if dist > gate:
                    det = None

            # 3) Update the tracker (smoothed velocity) or coast the estimate.
            if det is not None:
                dx, dy = det[0], det[1]
                if det_prev is not None and now > det_prev[0]:
                    ddt = now - det_prev[0]           # time since last ACCEPTED det
                    bvx = 0.55 * bvx + 0.45 * (dx - det_prev[1]) / ddt
                    bvy = 0.55 * bvy + 0.45 * (dy - det_prev[2]) / ddt
                    sp = (bvx * bvx + bvy * bvy) ** 0.5
                    if sp > cfg.boot_vmax:            # physical clamp: real boot
                        k = cfg.boot_vmax / sp        # tops out ~1300 px/s
                        bvx *= k
                        bvy *= k
                else:
                    bvx = bvy = 0.0
                bx, by = float(dx), float(dy)
                det_prev = (now, float(dx), float(dy))
                last_t = now
                lost = 0
                seen += 1
                ever_seen = True
                target_x = dx
                last_seen_wall = now
                small_run = small_run + 1 if det[2] < cfg.reacquire_min_area else 0
                # Impossible-track breakers: some gold blocks have an ANIMATED
                # shine, so they pass motion+color and can steal the lock while
                # the boot flies on. Two motions a real boot never makes:
                det_hist.append((now, float(dx), float(dy), int(det[2])))
                if len(det_hist) > 16:
                    det_hist.pop(0)
                broke = None
                if len(det_hist) >= cfg.brk_static_dets:
                    dw = det_hist[-cfg.brk_static_dets:]
                    xs = [p[1] for p in dw]
                    ys = [p[2] for p in dw]
                    if (max(xs) - min(xs) < cfg.brk_static_box
                            and max(ys) - min(ys) < cfg.brk_static_box
                            and dw[-1][0] - dw[0][0] >= cfg.brk_static_secs):
                        broke = "static lock (sparkle/shine)"
                if (cfg.brk_junk_kinematics and broke is None
                        and len(det_hist) >= cfg.brk_column_dets):
                    dw = det_hist[-cfg.brk_column_dets:]
                    xs = [p[1] for p in dw]
                    ys = [p[2] for p in dw]
                    if (max(xs) - min(xs) < cfg.brk_column_xrange
                            and ys[-1] - ys[0] > cfg.brk_column_descent
                            and cfg.brk_column_edge <= dx <= w - cfg.brk_column_edge):
                        broke = "column walk (shine wave)"
                if (cfg.brk_junk_kinematics and broke is None
                        and len(det_hist) >= cfg.brk_coin_dets):
                    dw = det_hist[-cfg.brk_coin_dets:]
                    med = sorted(p[3] for p in dw)[len(dw) // 2]
                    if (med < cfg.brk_coin_med
                            and dw[-1][2] - dw[0][2] > cfg.brk_coin_descent):
                        broke = "coin ride (small median, descending)"
                if broke:
                    log(f"  catch: broke {broke} at ({dx},{int(dy)}) - blacklisted, re-acquiring")
                    blacklist.append((float(dx), float(dy), now + cfg.blacklist_secs))
                    det_hist = []
                    small_run = 0
                    bx = by = None
                    bvx = bvy = 0.0
                    seen = 0
                    det_prev = None
                    target_x = None
            else:
                lost += 1
                if bx is not None and last_t is not None:
                    if lost <= cfg.boot_freeze_lost:  # coast briefly, then FREEZE:
                        dt = now - last_t             # a long extrapolation with a
                        bx += bvx * dt                # noisy velocity walked the
                        by += bvy * dt                # ROI clean off the field
                    last_t = now
                # Breakthrough: the boot punched through the bricks and left
                # through the TOP of the field - that's how a level is WON (the
                # game fades to the next layout ~1.3s later). Don't re-acquire
                # garbage in the meantime; declare success and end the phase.
                if (det_prev is not None and det_prev[2] < cfg.exit_top_y
                        and bvy < cfg.exit_top_vy and lost >= cfg.exit_top_lost):
                    breakthrough = True
                if lost > cfg.boot_lost_keep:         # drop the lock, re-acquire
                    if det_prev is not None and len(det_hist) >= 3:
                        xs = [p[1] for p in det_hist[-3:]]
                        ys = [p[2] for p in det_hist[-3:]]
                        if max(xs) - min(xs) < 25 and max(ys) - min(ys) < 25:
                            # the lock died going NOWHERE = a dead spot (shimmer
                            # block / sparkle), not a moving boot that got
                            # occluded - keep re-acquisition off it for a while
                            blacklist.append((det_prev[1], det_prev[2],
                                              now + cfg.blacklist_secs))
                    det_hist = []
                    small_run = 0
                    bx = by = None
                    bvx = bvy = 0.0
                    seen = 0
                    det_prev = None
                    target_x = None

        # 4) Steer: pure pursuit of the boot's last SEEN x, with hysteresis.
        if target_x is not None and cx is not None and lost <= cfg.boot_lost_keep:
            # Steer toward where the cart WILL be given its momentum, not where it
            # is now - damps the overshoot that let a straight-descending boot get
            # chased past (cart hit 748 while the boot sat at 602, then it bounced
            # away). cart_vx*lookahead is the anticipated travel.
            steer(target_x - (cx + cart_vx * cfg.catch_cart_lookahead), cfg, w)
        else:
            release_held()

        if _dbg_dir and n % 4 == 0:                   # save clean + annotated
            cv2.imwrite(os.path.join(_dbg_dir, f"f{_dbg_n:04d}_catch_raw.png"), panel)
            boot_draw = (int(bx), int(by)) if bx is not None else None
            ann = detect.annotate(panel, cfg, int(cx) if cx is not None else cart,
                                  int(target_x) if target_x is not None else None,
                                  int(sat), "catch", boot_draw)
            cv2.imwrite(os.path.join(_dbg_dir, f"f{_dbg_n:04d}_catch.png"), ann)
            _dbg_n += 1
        n += 1
        _tel.append((round(now - start, 3),
                     int(bx) if bx is not None else "",
                     int(by) if by is not None else "",
                     det[0] if det is not None else "",
                     det[2] if det is not None else "",   # detected blob area
                     cart if cart is not None else "",
                     round(cx, 1) if cx is not None else "",
                     round(target_x, 1) if target_x is not None else "",
                     round(bvx, 1), round(bvy, 1), lost,
                     1 if stale else 0))
        if breakthrough:
            log("  catch: BREAKTHROUGH - boot exited the top, level complete!")
            break
        # End the phase once the boot has been gone a while (caught or dropped).
        if time.time() - last_seen_wall > cfg.boot_lost_secs:
            log(f"  catch: boot gone ({'caught/lost' if ever_seen else 'never seen'})")
            break
        time.sleep(cfg.catch_period)
    release_held()
    if _dbg_dir and _tel:                       # dump this rally's telemetry
        p = os.path.join(_dbg_dir, "catch_tel.csv")
        header = not os.path.exists(p)
        with open(p, "a", encoding="utf-8") as f:
            if header:
                f.write("t,bx,by,det_x,det_area,cart,cx,target,vx,vy,lost,stale\n")
            f.write("# --- boot ---\n")
            for r in _tel:
                f.write(",".join(str(x) for x in r) + "\n")
    return breakthrough


def main_loop(cfg, verbose):
    global running
    park_cursor()                            # keep cursor off the game
    region = find_game_region(cfg)
    if region is None and cfg.unpause_f9:
        # No modal at all usually means the pause overlay - try to resume.
        log("No modal found; tapping F9 in case it's paused...")
        tap('f9', cfg.space_hold)
        time.sleep(0.7)
        region = get_panel_region(cfg)
    if region is None:
        log("! Could not locate the play field on any monitor. Make sure the\n"
            "  minigame is open, then try `--grab` to check what's captured,\n"
            "  `--monitor N` to force a monitor, or `--calibrate` to pin it.")
        running = False
        return
    log(f"Field region (absolute): {tuple(int(v) for v in region)}")
    refresh_hwnd(region)
    if _manual:
        # Manual mode: no clicks, no foreground grabbing - the user alt-tabs to
        # Dota and clicks the minigame themselves; the bot only sends Space/A/D.
        log("  MANUAL mode: focus the minigame yourself (alt-tab to Dota, click")
        log("  inside the game field once) and keep Dota in front. The bot will")
        log("  only press Space / A / D - it will never click or steal focus.")
    else:
        bring_foreground()
        # Keys go via PostMessage (no keyboard focus needed), BUT Dota only
        # processes them when its window is ACTIVE/foreground. The bot brings it
        # to front best-effort; if keys don't land, keep Dota in front (alt-tab
        # to it, don't switch to the terminal). No clicking required.
        log("  >>> Keep the Dota window ACTIVE/in front (don't switch to this terminal)")
        log("      - PostMessage keys only work when Dota is the foreground window. <<<")
    # If we're starting on a paused screen (desaturated), tap F9 to resume.
    s0 = detect.scene_saturation(grab_region(region))
    if s0 < cfg.sat_ended:
        log(f"  low saturation at start (sat={s0:.0f}) - tapping F9 to unpause")
        tap('f9', cfg.space_hold)
        time.sleep(0.8)

    shot = 0
    focus_prompted = False
    while running:
        if not wait_ready(region, cfg):
            if running:
                log("  no ready level detected (timeout) - re-locating...")
                new_region = get_panel_region(cfg)
                if new_region is not None:
                    region = new_region
            continue
        if not focus_prompted:
            if _use_post:
                # Keys go via PostMessage straight to the window - no focus/click
                # needed. If this works the cart moves with NO clicking at all.
                log("  input: PostMessage (focus-free). NO click needed - if the")
                log("  cart moves, we're done. If it doesn't move AT ALL, tell me")
                log("  and I'll switch to the click-to-focus method.")
            else:
                # keyboard-lib mode needs real focus: click the GAME panel (which
                # only exists now that a level is loaded; the intro was a different
                # panel). One click; focus then holds for the session.
                log("=" * 62)
                log(">>> The level is up. CLICK once inside the GAME FIELD now <<<")
                log("    (the dark play area with the cart - NOT the title/menu).")
                log(f"    Continuing in {cfg.focus_click_wait:.0f}s...")
                log("=" * 62)
                fend = time.time() + cfg.focus_click_wait
                while running and time.time() < fend:
                    time.sleep(0.5)
            focus_prompted = True
        shot += 1
        log(f"[boot {shot}] lock -> throw -> catch")
        play_level(region, cfg, verbose)
    release_held()
    log("Loop stopped.")


# --------------------------------------------------------------------------- #
# Hotkey plumbing (mirrors the minesweeper bot)
# --------------------------------------------------------------------------- #

def start_game(cfg, verbose):
    global running
    if not running:
        running = True
        log("=== Starting Boot Breaker bot ===")
        try:
            main_loop(cfg, verbose)
        finally:
            release_held()


def listen_for_stop():
    global running
    while stop_flag:
        if keyboard.is_pressed('q') and running:
            running = False
            print("'q' pressed - stopping after current step...")
        time.sleep(0.01)


def run_interactive(cfg, verbose):
    global stop_flag
    t = threading.Thread(target=listen_for_stop, daemon=True)
    t.start()
    keyboard.add_hotkey('s', lambda: start_game(cfg, verbose))
    print("Ready. Open Boot Breaker (intro or a level), then press 's'.\n"
          "It will click PLAY if needed and play on. Hold 'q' to stop, Ctrl+C to quit.")
    try:
        keyboard.wait('ctrl+c')
    except KeyboardInterrupt:
        pass
    finally:
        stop_flag = False
        release_held()
        t.join(timeout=1.0)
        print("Bye.")


# --------------------------------------------------------------------------- #
# Utility modes
# --------------------------------------------------------------------------- #

def do_calibrate(cfg):
    def pick(label):
        print(f"{label}: hover the mouse and hold still for 3s...")
        base = cursor_pos()
        stable = time.time()
        while True:
            time.sleep(0.05)
            p = cursor_pos()
            if abs(p[0] - base[0]) <= 4 and abs(p[1] - base[1]) <= 4:
                if time.time() - stable >= 3.0:
                    print(f"  -> {p}")
                    return p
            else:
                base, stable = p, time.time()

    print("Point at the GOLD FRAME corners of the minigame window (the ornate\n"
          "border with the bolts), works on any monitor.")
    a = pick("Top-left frame corner")
    b = pick("Bottom-right frame corner")
    ml, mt = min(a[0], b[0]), min(a[1], b[1])
    mw, mh = abs(b[0] - a[0]), abs(b[1] - a[1])
    if mw < 250 or mh < 300:
        print(f"Frame {mw}x{mh} looks too small - aborting.")
        return
    # Inset the modal to the play field using the measured fractions.
    fl = int(ml + mw * cfg.field_x0)
    ft = int(mt + mh * cfg.field_y0)
    fw = int(mw * (cfg.field_x1 - cfg.field_x0))
    fh = int(mh * (cfg.field_y1 - cfg.field_y0))
    cfg.region = [fl, ft, fw, fh]
    print(f"Field region: {cfg.region}")
    save_config(cfg)


def do_snapshot(cfg, name):
    region = find_game_region(cfg)
    if region is None:
        print("Could not locate the field - try --grab instead.")
        return
    img = grab_region(region)
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    os.makedirs(out_dir, exist_ok=True)
    out = os.path.join(out_dir, f"{name}.png")
    cv2.imwrite(out, img)
    print(f"Saved field snapshot -> {out}  ({img.shape[1]}x{img.shape[0]})")


def do_grab(cfg):
    """Save one PNG per monitor so you can see exactly what the bot captures."""
    out_dir = os.path.join(os.path.dirname(os.path.abspath(__file__)), "snapshots")
    os.makedirs(out_dir, exist_ok=True)
    mons = monitors()
    print(f"{len(mons) - 1} monitor(s) detected.")
    for i, mon in enumerate(mons[1:], start=1):
        img = grab_region((mon["left"], mon["top"], mon["width"], mon["height"]))
        r = detect.locate_field(img, cfg)
        out = os.path.join(out_dir, f"monitor{i}.png")
        cv2.imwrite(out, img)
        found = f"field found at {r}" if r else "no field found"
        print(f"  monitor{i} {mon['width']}x{mon['height']} @({mon['left']},{mon['top']}) "
              f"-> {out}  [{found}]")


def do_dry_run(cfg, seconds, debug_dir):
    region = find_game_region(cfg)
    if region is None:
        print("! Could not locate the play field on any monitor. Try --grab, "
              "--monitor N, or --calibrate.")
        return
    print(f"Field region (absolute): {tuple(int(v) for v in region)}")
    if debug_dir:
        os.makedirs(debug_dir, exist_ok=True)
    print("DRY RUN - no keys pressed. Ctrl+C to stop.\n"
          "Check the red line sits on the cart and the green line on the middle "
          "of the bricks; sat should be high (~200+) while playing and drop near "
          "0 during the between-level loading. Then tune config.json.")
    period = 1.0 / max(1.0, cfg.poll_hz)
    end = time.time() + seconds
    n = 0
    try:
        while time.time() < end:
            panel = grab_region(region)
            cart = detect.find_cart_x(panel, cfg)
            target = detect.find_brick_centroid_x(panel, cfg)
            sat = detect.scene_saturation(panel)
            ready = sat >= cfg.sat_ready and cart is not None
            state = "READY" if ready else ("loading" if sat < cfg.sat_ended else "busy")
            print(f"sat={sat:6.1f} cart={cart!s:>5} target={target!s:>5} -> {state}")
            if debug_dir and n % 4 == 0:
                img = detect.annotate(panel, cfg, cart, target, int(sat), state)
                cv2.imwrite(os.path.join(debug_dir, f"dry{n:05d}.png"), img)
            n += 1
            time.sleep(period)
    except KeyboardInterrupt:
        print("Stopped.")


# --------------------------------------------------------------------------- #

def build_parser():
    p = argparse.ArgumentParser(description="Dota 2 Boot Breaker bot")
    p.add_argument("--calibrate", action="store_true", help="pin the field by pointing at the gold frame")
    p.add_argument("--dry-run", action="store_true", help="detect only, press no keys")
    p.add_argument("--dry-seconds", type=float, default=30.0)
    p.add_argument("--snapshot", metavar="NAME", help="save one field screenshot and exit")
    p.add_argument("--grab", action="store_true", help="save one screenshot per monitor and exit")
    p.add_argument("--monitor", type=int, help="force capture of mss monitor N (1=first)")
    p.add_argument("--no-auto-play", action="store_true", help="don't auto-click the PLAY button")
    p.add_argument("--manual", action="store_true",
                   help="no clicks / no foreground grabbing: YOU focus the minigame "
                        "and click PLAY; the bot only presses Space/A/D")
    p.add_argument("--no-debug", action="store_true", help="don't save annotated frames while playing")
    p.add_argument("--debug-dir", default="debug")
    p.add_argument("--verbose", "-v", action="store_true")
    return p


def main():
    global _dbg_dir, _log_f, _manual
    set_dpi_aware()
    args = build_parser().parse_args()
    cfg = load_config()
    if args.monitor is not None:
        cfg.monitor = args.monitor
    if args.no_auto_play:
        cfg.auto_play = False
    if args.manual:
        _manual = True
        cfg.auto_play = False

    if args.calibrate:
        do_calibrate(cfg)
        return
    if args.grab:
        do_grab(cfg)
        return
    if args.snapshot:
        do_snapshot(cfg, args.snapshot)
        return
    if args.dry_run:
        do_dry_run(cfg, args.dry_seconds, args.debug_dir)
        return

    # Interactive play: always write a log; save debug frames unless disabled.
    here = os.path.dirname(os.path.abspath(__file__))
    _log_f = open(os.path.join(here, "boot_breaker.log"), "w", encoding="utf-8")
    if not args.no_debug:
        _dbg_dir = os.path.join(here, args.debug_dir)
        os.makedirs(_dbg_dir, exist_ok=True)
        for old in os.listdir(_dbg_dir):                 # fresh frames each run
            if (old.startswith("f") and old.endswith(".png")) or old == "catch_tel.csv":
                try:
                    os.remove(os.path.join(_dbg_dir, old))
                except OSError:
                    pass
        log(f"Saving debug frames to {_dbg_dir}")
    run_interactive(cfg, args.verbose)


if __name__ == "__main__":
    if sys.platform != "win32":
        print("This bot is Windows-only.")
        sys.exit(1)
    main()
