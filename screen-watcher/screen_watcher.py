"""
screen_watcher.py — watches every monitor for Cursor's approval buttons
("Allow", "Run", ...) and plays a repeating alert for as long as one is on
screen, so a paused agent never sits waiting unnoticed. It never clicks.
OpenCV-free build: pure numpy template matching (works on Windows-on-ARM).

Platforms:
- Windows: works out of the box.
- macOS: requires Screen Recording permission for the app you run this from
  (System Settings > Privacy & Security > Screen Recording). If the bundled
  templates don't match, capture your own on the Mac. For a method that
  needs no permissions, see ../cursor-hooks/cursor_hooks.py.

Setup (one time):
    pip install -r requirements.txt

Step 1 — capture each button (once, while it's visible on screen):
    python screen_watcher.py --capture allow_button
    python screen_watcher.py --capture run_button
    Each gives a 5s countdown to put your mouse on the TOP-LEFT corner of the
    button, then 5s for the BOTTOM-RIGHT corner. Saves <name>.png in the
    templates/ folder. Capture as many different buttons as you want.

Step 2 — run the watcher (auto-loads every PNG in templates/, plus anything
passed via --template):
    python screen_watcher.py
    python screen_watcher.py --interval 2 --threshold 0.86
    python screen_watcher.py --template extra_button.png

Notes:
- Scans the ENTIRE virtual desktop (all monitors) for ALL buttons each cycle
  and repeats the alert every two seconds while any are visible.
- Never clicks or moves the mouse.
- Finds buttons at 0.5x to 2x their template size (display scaling, Retina,
  templates from another machine). The size each template is found at is
  learned and saved to templates/.learned_scales.json, so later scans only
  search around it, with a full sweep every few scans.
- Matching is coarse-to-fine with full-resolution verification, and all sum
  math runs in float64 (float32 precision loss on big desktops causes false
  positives).

Emergency stop: Ctrl+C.
"""

import argparse
import ctypes
import io
import json
import math
import struct
import subprocess
import sys
import tempfile
import time
import wave
from pathlib import Path

import numpy as np
import pyautogui
try:
    from mss import MSS as mss  # mss >= 10.2
except ImportError:
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


def require_screen_permission():
    """On macOS, exit with instructions if Screen Recording isn't allowed.
    Without it, screenshots contain only the wallpaper, so nothing ever matches."""
    if sys.platform != "darwin":
        return
    try:
        import Quartz  # installed with pyautogui on macOS
    except ImportError:
        return
    if Quartz.CGPreflightScreenCaptureAccess():
        return
    Quartz.CGRequestScreenCaptureAccess()  # adds the app to the settings list
    sys.exit(
        "Screen Recording permission is required.\n"
        "Open System Settings > Privacy & Security > Screen Recording, turn on\n"
        "the app you're running this from (Terminal, iTerm, Cursor, VS Code...),\n"
        "then quit and reopen that app and run this again."
    )


def grab_virtual_screen(sct):
    """Screenshot the full virtual desktop (all monitors).
    Returns (grayscale float32 image, x_offset, y_offset)."""
    mon = sct.monitors[0]  # index 0 = bounding box of ALL monitors
    raw = np.asarray(sct.grab(mon), dtype=np.float32)  # H x W x 4, BGRA
    gray = 0.114 * raw[..., 0] + 0.587 * raw[..., 1] + 0.299 * raw[..., 2]
    return gray, mon["left"], mon["top"]


# ------------------------------------------------- numpy template matching --

# Template sizes tried, relative to the captured template: 0.5x to 2x in ~15%
# steps. Covers Windows display scaling, Retina vs. non-Retina screens, and
# templates captured on a different machine.
SCALES = tuple(sorted({round(1.15 ** i, 3) for i in range(-5, 6)}))
FINE_STEPS = (1.0, 0.96, 1.04)  # full-res refinement between SCALES steps
COARSE_HEIGHT = 10              # template height (px) during the coarse pass
MAX_CANDIDATES = 10             # coarse hits verified per template and scale
FULL_SWEEP_EVERY = 5            # once a scale is learned, try all scales this often
LEARNED_SCALES_FILE = TEMPLATE_DIR / ".learned_scales.json"


def _integral(a):
    """Integral image in float64 — float32 cumsums lose precision on large
    screens and corrupt the scores."""
    return np.pad(a.astype(np.float64), ((1, 0), (1, 0))).cumsum(axis=0).cumsum(axis=1)


def _window_sums(ii, h, w):
    """Sum of every h x w window, given integral image ii."""
    return ii[h:, w:] - ii[:-h, w:] - ii[h:, :-w] + ii[:-h, :-w]


def _fast_len(n):
    """Smallest 2^a * 3^b * 5^c >= n. FFTs of such sizes are far faster than
    of sizes with large prime factors (screen sizes often have them)."""
    best = 2 * n
    p5 = 1
    while p5 < best:
        p35 = p5
        while p35 < best:
            p = p35
            while p < n:
                p *= 2
            best = min(best, p)
            p35 *= 3
        p5 *= 5
    return best


class PreparedImage:
    """An image plus the parts every match against it reuses (its FFT and
    integral images), so they're computed once per scan, not per template."""

    def __init__(self, image):
        self.image = image.astype(np.float64)
        self.shape = self.image.shape
        # zero-padding to a fast size doesn't change the valid score region
        self.fft_shape = tuple(_fast_len(n) for n in self.shape)
        self.fft = np.fft.rfft2(self.image, s=self.fft_shape)
        self.ii = _integral(self.image)
        self.ii2 = _integral(self.image * self.image)


def _zero_mean(template):
    return template.astype(np.float64) - template.mean(dtype=np.float64)


def match_zncc(image, template, template_fft=None):
    """Zero-mean normalized cross-correlation (equivalent to OpenCV's
    TM_CCOEFF_NORMED). image may be an array or a PreparedImage; template_fft
    is an optional cached rfft2 of the zero-meaned template at image.fft_shape.
    Returns score map of shape (H-h+1, W-w+1) in [-1, 1]."""
    if not isinstance(image, PreparedImage):
        image = PreparedImage(image)
    H, W = image.shape
    h, w = template.shape
    if h > H or w > W:
        return None

    t = _zero_mean(template)
    t_norm = np.sqrt((t * t).sum())
    if t_norm < 1e-6:
        return None  # flat template can't be matched

    # cross-correlation of image with zero-meaned template, via FFT
    if template_fft is None:
        template_fft = np.fft.rfft2(t, s=image.fft_shape)
    corr = np.fft.irfft2(image.fft * np.conj(template_fft), s=image.fft_shape)
    corr = corr[: H - h + 1, : W - w + 1]

    # per-window mean/variance of the image, via integral images
    n = h * w
    s1 = _window_sums(image.ii, h, w)
    s2 = _window_sums(image.ii2, h, w)
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


def _suppress(points, min_dist):
    """Non-max suppression: points are (x, y, score, ...) tuples; keeps the
    strongest of any that lie within min_dist of each other."""
    kept = []
    for p in sorted(points, key=lambda p: -p[2]):
        if all(abs(p[0] - k[0]) > min_dist or abs(p[1] - k[1]) > min_dist for k in kept):
            kept.append(p)
    return kept


class Matcher:
    """Multi-scale template search that learns the size each template appears
    at on this screen. Until a template has been seen, every scan tries all
    SCALES. After that, scans try only the learned scale and its neighbours,
    plus a full sweep every FULL_SWEEP_EVERY scans in case a window moves to
    a display with different scaling. Templates not seen yet borrow the
    scale learned for the others, since buttons on one screen share a size.
    Learned scales persist across runs."""

    def __init__(self, templates, threshold, learned_file=LEARNED_SCALES_FILE):
        self.templates = templates
        self.threshold = threshold
        self.coarse_threshold = max(0.5, threshold - 0.12)  # downscaling blurs; be lenient
        self.learned_file = learned_file
        self.variants = {name: self._build_variants(t) for name, t in templates.items()}
        self.learned = self._load_learned()
        self.scans = 0
        self._fft_cache = {}     # (name, scale) -> template FFT at the coarse screen size
        self._fft_shape = None
        self._fine_cache = {}    # (name, scale) -> resized full-res template

    @staticmethod
    def _build_variants(template):
        """Precompute each scaled copy of a template, downscaled so it's about
        COARSE_HEIGHT px tall for the coarse pass."""
        variants = []
        for scale in SCALES:
            full = template if scale == 1.0 else _resize_template(template, scale)
            ds = max(1, round(full.shape[0] / COARSE_HEIGHT))
            small = _downscale(full, ds)
            if min(small.shape) >= 5:
                variants.append({"scale": scale, "full": full, "ds": ds, "small": small})
        return variants

    # ---- learned scales

    def _load_learned(self):
        if self.learned_file is None or not self.learned_file.exists():
            return {}
        try:
            data = json.loads(self.learned_file.read_text())
        except (OSError, ValueError):
            return {}
        # ignore entries for templates that were recaptured since
        return {name: entry["scale"] for name, entry in data.items()
                if name in self.templates and entry.get("scale") in SCALES
                and entry.get("shape") == list(self.templates[name].shape)}

    def _learn(self, name, scale):
        if self.learned.get(name) == scale:
            return
        self.learned[name] = scale
        if self.learned_file is None:
            return
        data = {n: {"scale": s, "shape": list(self.templates[n].shape)}
                for n, s in self.learned.items()}
        try:
            self.learned_file.write_text(json.dumps(data, indent=2) + "\n")
        except OSError:
            pass  # read-only folder: just relearn next run

    def _search_order(self, name):
        known = self.learned.get(name)
        if known is None and self.learned:
            known = sorted(self.learned.values())[len(self.learned) // 2]  # median
        center = known or 1.0
        ordered = sorted(self.variants[name], key=lambda v: abs(math.log(v["scale"] / center)))
        if known and self.scans % FULL_SWEEP_EVERY:
            return ordered[:3]  # learned scale and its two neighbours
        return ordered

    # ---- search

    def find_all(self, screen):
        """Search for every template in one screen grab.
        Returns list of (name, center_x, center_y, score)."""
        self.scans += 1
        if screen.shape != self._fft_shape:  # display layout changed
            self._fft_cache.clear()
            self._fft_shape = screen.shape
        coarse_screens = {}  # ds -> PreparedImage, shared by all templates this scan
        results = []
        for name in self.variants:
            for v in self._search_order(name):
                hits = self._find_at_scale(name, v, screen, coarse_screens)
                if hits:
                    self._learn(name, v["scale"])
                    results += [(name, x, y, s) for x, y, s in hits]
                    break
        return results

    def _find_at_scale(self, name, v, screen, coarse_screens):
        """Coarse-to-fine search for one scaled template. Returns list of
        (center_x, center_y, score) in full-resolution screen coords."""
        ds = v["ds"]
        if ds not in coarse_screens:
            coarse_screens[ds] = PreparedImage(_downscale(screen, ds))
        small_screen = coarse_screens[ds]

        # ---- coarse pass on the downscaled screen
        key = (name, v["scale"])
        if key not in self._fft_cache:
            self._fft_cache[key] = np.fft.rfft2(_zero_mean(v["small"]), s=small_screen.fft_shape)
        score = match_zncc(small_screen, v["small"], self._fft_cache[key])
        if score is None:
            return []
        ys, xs = np.nonzero(score >= self.coarse_threshold)
        if len(xs) == 0:
            return []
        candidates = _suppress(list(zip(xs, ys, score[ys, xs])), max(v["small"].shape))
        candidates = candidates[:MAX_CANDIDATES]

        # ---- fine pass: verify each candidate at FULL resolution on a small
        # patch, trying sizes just either side of this scale. This is what
        # prevents alerting on things that merely resemble the button.
        hits = []
        H, W = screen.shape
        th, tw = v["full"].shape
        pad = max(8, 2 * ds)
        for cx, cy, _ in candidates:
            cx, cy = int(cx) * ds, int(cy) * ds
            x0, y0 = max(0, cx - pad), max(0, cy - pad)
            x1, y1 = min(W, cx + int(tw * 1.05) + pad), min(H, cy + int(th * 1.05) + pad)
            patch = PreparedImage(screen[y0:y1, x0:x1])
            for step in FINE_STEPS:
                tmpl = self._fine_template(name, v, step)
                fine = match_zncc(patch, tmpl)
                if fine is None or fine.size == 0:
                    continue
                py, px = np.unravel_index(np.argmax(fine), fine.shape)
                s = float(fine[py, px])
                if s >= self.threshold:
                    hits.append((x0 + int(px) + tmpl.shape[1] // 2,
                                 y0 + int(py) + tmpl.shape[0] // 2, s))
                    break

        # collapse overlapping detections of the same button
        return _suppress(hits, max(th, tw))

    def _fine_template(self, name, v, step):
        if step == 1.0:
            return v["full"]
        key = (name, round(v["scale"] * step, 4))
        if key not in self._fine_cache:
            self._fine_cache[key] = _resize_template(self.templates[name], key[1])
        return self._fine_cache[key]


# ------------------------------------------------------------------ capture --

def capture_template(name: str):
    set_dpi_aware()
    require_screen_permission()
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

    # On Retina displays the grab is in physical pixels while mouse positions
    # are in points, so convert before cropping.
    sx = raw.shape[1] / mon["width"]
    sy = raw.shape[0] / mon["height"]
    crop = raw[round((y1 - oy) * sy) : round((y2 - oy) * sy),
               round((x1 - ox) * sx) : round((x2 - ox) * sx)]
    if crop.size == 0:
        sys.exit("Empty crop — coordinates were outside the captured screen. Try again.")

    rgb = crop[..., [2, 1, 0]]  # BGRA -> RGB
    Image.fromarray(rgb).save(path)
    print(f"Template saved to {path}  ({crop.shape[1]}x{crop.shape[0]} px)")
    print("Now run:  python screen_watcher.py")


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
            "  python screen_watcher.py --capture allow_button\n"
            "  python screen_watcher.py --capture run_button"
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


_alert_wav_path = None


def play_alert():
    """Play a quiet melodic alert instead of a system sound."""
    global _alert_wav_path
    if sys.platform == "win32":
        import winsound
        winsound.PlaySound(make_alert_sound(), winsound.SND_MEMORY)
    elif sys.platform == "darwin":
        # afplay can't read from memory, so write the WAV once and reuse it
        if _alert_wav_path is None:
            path = Path(tempfile.gettempdir()) / "cursor_approval_alert.wav"
            path.write_bytes(make_alert_sound())
            _alert_wav_path = path
        subprocess.run(["afplay", str(_alert_wav_path)], check=False)
    else:
        print("\a", end="", flush=True)  # unsupported platform: terminal bell


def run_watcher(extra_templates, interval: float, threshold: float):
    set_dpi_aware()
    require_screen_permission()
    templates = load_all_templates(extra_templates)
    matcher = Matcher(templates, threshold)
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
                matches = matcher.find_all(screen)

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