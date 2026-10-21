"""
approval_alert.py — watches every monitor for a coding agent's approval
buttons ("Allow", "Run", ...) and plays a repeating alert for as long as one
is on screen, so a paused agent never sits waiting unnoticed. It never clicks.
OpenCV-free build: pure numpy template matching (works on Windows-on-ARM).

Setup (one time):
    pip install -r requirements.txt

Step 1 — capture each button (once, while it's visible on screen):
    python approval_alert.py --capture allow_button
    python approval_alert.py --capture run_button
    Each gives a 5s countdown to put your mouse on the TOP-LEFT corner of the
    button, then 5s for the BOTTOM-RIGHT corner. Saves <name>.png in the
    templates/ folder. Capture as many different buttons as you want.

Step 2 — run the watcher (auto-loads every PNG in templates/, plus anything
passed via --template):
    python approval_alert.py
    python approval_alert.py --interval 2 --threshold 0.86
    python approval_alert.py --template extra_button.png

Notes:
- Scans the ENTIRE virtual desktop (all monitors) for ALL buttons each cycle
  and repeats the alert every two seconds while any are visible.
- Never clicks or moves the mouse.
- Matching is coarse-to-fine with full-resolution verification, and all sum
  math runs in float64 (float32 precision loss on big desktops causes false
  positives).

Emergency stop: Ctrl+C.
"""

import argparse
import ctypes
import io
import math
import struct
import sys
import time
import winsound
import wave
from pathlib import Path

import numpy as np
import pyautogui
from mss import mss
from PIL import Image

pyautogui.FAILSAFE = False
pyautogui.PAUSE = 0.02

TEMPLATE_DIR = Path(__file__).parent / "templates"
ALERT_REPEAT_SECONDS = 2.0

# ---------------------------------------------------------------- platform --

def set_dpi_aware():
    """Make coordinates consistent on Windows with display scaling (125%/150% etc.)."""
    if sys.platform == "win32":
        try:
            ctypes.windll.shcore.SetProcessDpiAwareness(2)  # per-monitor DPI aware
        except Exception:
            try:
                ctypes.windll.user32.SetProcessDPIAware()
            except Exception:
                pass


def grab_virtual_screen(sct):
    """Screenshot the full virtual desktop (all monitors).
    Returns (grayscale float32 image, x_offset, y_offset)."""
    mon = sct.monitors[0]  # index 0 = bounding box of ALL monitors
    raw = np.asarray(sct.grab(mon), dtype=np.float32)  # H x W x 4, BGRA
    gray = 0.114 * raw[..., 0] + 0.587 * raw[..., 1] + 0.299 * raw[..., 2]
    return gray, mon["left"], mon["top"]


# ------------------------------------------------- numpy template matching --

def _window_sums(a, h, w):
    """Sum of every h x w window of 2-D array a, via integral image (float64 —
    float32 cumsums lose precision on large screens and corrupt the scores)."""
    ii = np.pad(a, ((1, 0), (1, 0))).astype(np.float64).cumsum(axis=0).cumsum(axis=1)
    return ii[h:, w:] - ii[:-h, w:] - ii[h:, :-w] + ii[:-h, :-w]


def match_zncc(image, template):
    """Zero-mean normalized cross-correlation (equivalent to OpenCV's
    TM_CCOEFF_NORMED). Returns score map of shape (H-h+1, W-w+1) in [-1, 1]."""
    H, W = image.shape
    h, w = template.shape
    if h > H or w > W:
        return None

    image = image.astype(np.float64, copy=False)
    t = template.astype(np.float64) - template.mean(dtype=np.float64)
    t_norm = np.sqrt((t * t).sum())
    if t_norm < 1e-6:
        return None  # flat template can't be matched

    # cross-correlation of image with zero-meaned template, via FFT
    F = np.fft.rfft2(image)
    G = np.fft.rfft2(t, s=image.shape)
    corr = np.fft.irfft2(F * np.conj(G), s=image.shape)[: H - h + 1, : W - w + 1]

    # per-window mean/variance of the image, via integral images
    n = h * w
    s1 = _window_sums(image, h, w)
    s2 = _window_sums(image * image, h, w)
    var = s2 - (s1 * s1) / n
    np.maximum(var, 0.0, out=var)

    denom = np.sqrt(var) * t_norm
    score = corr / np.maximum(denom, 1e-6)
    # a window must have meaningful contrast to count — kills divide-by-noise
    # hits on flat regions (solid panels, empty desktop, etc.)
    score[var < n * 4.0] = 0.0
    return np.clip(score, -1.0, 1.0)


def _resize_template(template, scale):
    h, w = template.shape
    nh, nw = max(1, int(round(h * scale))), max(1, int(round(w * scale)))
    img = Image.fromarray(template.astype(np.float32))
    img = img.resize((nw, nh), Image.LANCZOS)
    return np.asarray(img, dtype=np.float32)


def _downscale(img, ds):
    """Fast integer block-average downscale."""
    if ds == 1:
        return img
    H, W = img.shape
    H2, W2 = (H // ds) * ds, (W // ds) * ds
    return img[:H2, :W2].reshape(H2 // ds, ds, W2 // ds, ds).mean(axis=(1, 3))


def find_matches(screen, template, threshold, small_screen_cache):
    """Coarse-to-fine search for one template. Returns list of
    (center_x, center_y, score) in full-resolution image coords.
    small_screen_cache: dict reused across templates within one scan so the
    downscaled screen is computed once per downscale factor."""
    th0, tw0 = template.shape

    # ---- coarse pass on a downscaled screen (template ends up ~10px tall)
    ds = max(1, th0 // 10)
    if ds not in small_screen_cache:
        small_screen_cache[ds] = _downscale(screen, ds)
    small_screen = small_screen_cache[ds]
    coarse_thr = max(0.5, threshold - 0.12)  # downscaling blurs; be lenient here

    candidates = []  # (x, y) full-res top-left estimates, with the scale that hit
    for scale in (1.0, 0.9, 1.1, 0.8, 1.25):
        tmpl = template if scale == 1.0 else _resize_template(template, scale)
        small_tmpl = _downscale(tmpl, ds)
        if min(small_tmpl.shape) < 5:
            continue
        score = match_zncc(small_screen, small_tmpl)
        if score is None:
            continue
        ys, xs = np.where(score >= coarse_thr)
        for x, y in zip(xs, ys):
            candidates.append((int(x) * ds, int(y) * ds, scale))
        if candidates:
            break  # found at this scale; no need to keep rescaling

    if not candidates:
        return []

    # ---- fine pass: verify each candidate at FULL resolution on a small patch.
    # This is what prevents alerting on things that merely resemble the button.
    hits = []
    H, W = screen.shape
    for cx, cy, scale in candidates:
        tmpl = template if scale == 1.0 else _resize_template(template, scale)
        th, tw = tmpl.shape
        pad = max(8, 2 * ds)
        x0, y0 = max(0, cx - pad), max(0, cy - pad)
        x1, y1 = min(W, cx + tw + pad), min(H, cy + th + pad)
        patch = screen[y0:y1, x0:x1]
        score = match_zncc(patch, tmpl)
        if score is None or score.size == 0:
            continue
        py, px = np.unravel_index(np.argmax(score), score.shape)
        s = float(score[py, px])
        if s >= threshold:
            hits.append((x0 + int(px) + tw // 2, y0 + int(py) + th // 2, s))

    # non-max suppression: collapse overlapping detections of the same button
    hits.sort(key=lambda h: -h[2])
    kept = []
    min_dist = max(tw0, th0)
    for x, y, s in hits:
        if all(abs(x - kx) > min_dist or abs(y - ky) > min_dist for kx, ky, _ in kept):
            kept.append((x, y, s))
    return kept


def find_all_buttons(screen, templates, threshold):
    """Search for every template in one screen grab.
    templates: dict name -> 2-D float32 array.
    Returns list of (name, center_x, center_y, score)."""
    small_screen_cache = {}
    results = []
    for name, tmpl in templates.items():
        for (x, y, s) in find_matches(screen, tmpl, threshold, small_screen_cache):
            results.append((name, x, y, s))
    return results


# ------------------------------------------------------------------ capture --

def capture_template(name: str):
    set_dpi_aware()
    TEMPLATE_DIR.mkdir(exist_ok=True)
    path = TEMPLATE_DIR / (name if name.endswith(".png") else f"{name}.png")
    print(f"Capturing template '{path.name}'. Make sure the button is visible on screen.")
    print("\nMove mouse to the TOP-LEFT corner of the button...")
    for i in range(5, 0, -1):
        print(f"  {i}...", end="\r", flush=True)
        time.sleep(1)
    x1, y1 = pyautogui.position()
    print(f"\n  got ({x1}, {y1})")

    print("Now move mouse to the BOTTOM-RIGHT corner of the button...")
    for i in range(5, 0, -1):
        print(f"  {i}...", end="\r", flush=True)
        time.sleep(1)
    x2, y2 = pyautogui.position()
    print(f"\n  got ({x2}, {y2})")

    if x2 <= x1 or y2 <= y1:
        sys.exit("Bottom-right must be below/right of top-left. Try again.")

    with mss() as sct:
        mon = sct.monitors[0]
        raw = np.asarray(sct.grab(mon))  # H x W x 4, BGRA
        ox, oy = mon["left"], mon["top"]

    crop = raw[y1 - oy : y2 - oy, x1 - ox : x2 - ox]
    if crop.size == 0:
        sys.exit("Empty crop — coordinates were outside the captured screen. Try again.")

    rgb = crop[..., [2, 1, 0]]  # BGRA -> RGB
    Image.fromarray(rgb).save(path)
    print(f"Template saved to {path}  ({crop.shape[1]}x{crop.shape[0]} px)")
    print("Now run:  python approval_alert.py")


def load_template(path: Path):
    img = Image.open(path).convert("RGB")
    arr = np.asarray(img, dtype=np.float32)
    # same gray weights as grab_virtual_screen, so scores stay consistent
    return 0.299 * arr[..., 0] + 0.587 * arr[..., 1] + 0.114 * arr[..., 2]


def load_all_templates(extra_paths):
    """Load every PNG in templates/ plus any extras. Returns dict name -> array."""
    templates = {}
    paths = sorted(TEMPLATE_DIR.glob("*.png")) if TEMPLATE_DIR.is_dir() else []
    paths += [Path(p) for p in (extra_paths or [])]
    for p in paths:
        if not p.exists():
            sys.exit(f"Template not found: {p}")
        templates[p.stem] = load_template(p)
    if not templates:
        sys.exit(
            "No templates found. Capture them first:\n"
            "  python approval_alert.py --capture allow_button\n"
            "  python approval_alert.py --capture run_button"
        )
    return templates


# ------------------------------------------------------------------ watcher --

def make_alert_sound():
    """Create a quiet, smoothly enveloped C-major arpeggio as a WAV in memory."""
    sample_rate = 22050
    note_duration = 0.11
    gap_duration = 0.025
    notes = (523.25, 659.25, 783.99, 659.25)
    pcm = bytearray()

    for note in notes:
        frame_count = int(sample_rate * note_duration)
        fade_frames = int(sample_rate * 0.015)
        for frame in range(frame_count):
            envelope = min(1.0, frame / fade_frames,
                           (frame_count - frame - 1) / fade_frames)
            sample = int(32767 * 0.10 * max(0.0, envelope) *
                         math.sin(2.0 * math.pi * note * frame / sample_rate))
            pcm.extend(struct.pack("<h", sample))
        pcm.extend(b"\0\0" * int(sample_rate * gap_duration))

    output = io.BytesIO()
    with wave.open(output, "wb") as wav:
        wav.setnchannels(1)
        wav.setsampwidth(2)
        wav.setframerate(sample_rate)
        wav.writeframes(pcm)
    return output.getvalue()


def play_alert():
    """Play a quiet melodic alert instead of a Windows system sound."""
    winsound.PlaySound(make_alert_sound(), winsound.SND_MEMORY)


def run_watcher(extra_templates, interval: float, threshold: float):
    set_dpi_aware()
    templates = load_all_templates(extra_templates)
    print(
        f"Watching all monitors every {interval}s for: {', '.join(templates)} "
        f"(threshold={threshold}). Alerts every {ALERT_REPEAT_SECONDS:g}s while "
        "detected; no clicking. "
        "Ctrl+C to stop."
    )

    total_pings = 0
    next_ping_at = 0.0
    with mss() as sct:
        while True:
            try:
                screen, ox, oy = grab_virtual_screen(sct)
                matches = find_all_buttons(screen, templates, threshold)

                if matches:
                    print(f"[{time.strftime('%H:%M:%S')}] {len(matches)} button(s) found")
                    now = time.monotonic()
                    if now >= next_ping_at:
                        play_alert()
                        total_pings += 1
                        next_ping_at = now + ALERT_REPEAT_SECONDS
                        print(f"  ping [total {total_pings}]")
                else:
                    next_ping_at = 0.0

                time.sleep(interval)

            except KeyboardInterrupt:
                print(f"\nStopped. Total pings: {total_pings}")
                return
            except Exception as e:
                # don't die on transient capture errors (monitor sleep, lock screen, etc.)
                print(f"  warn: {e}")
                time.sleep(interval)


def main():
    p = argparse.ArgumentParser(
        description="Play a repeating alert while a coding agent's approval "
                    "button (Allow / Run) is visible on screen.")
    p.add_argument("--capture", metavar="NAME", nargs="?", const="allow_button",
                   default=None,
                   help="capture a button template, e.g. --capture run_button")
    p.add_argument("--template", action="append", metavar="PATH",
                   help="extra template PNG(s) to watch for (repeatable)")
    p.add_argument("--interval", type=float, default=1.0, help="seconds between scans (default 1)")
    p.add_argument("--threshold", type=float, default=0.88, help="match confidence 0-1 (default 0.88)")
    args = p.parse_args()

    if args.capture:
        capture_template(args.capture)
    else:
        run_watcher(args.template, args.interval, args.threshold)


if __name__ == "__main__":
    main()