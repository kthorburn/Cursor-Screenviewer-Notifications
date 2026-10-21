"""
cursor_hooks.py — persistent approval alerts for Cursor's Agent, driven by
Cursor Hooks instead of screen capture. Needs no Screen Recording (or any
other) permission and no third-party packages. Works on macOS and Windows.

How it works:
- beforeShellExecution / beforeMCPExecution: the agent is about to run a
  terminal command or MCP tool. If it hasn't run within a few seconds (i.e.
  it's waiting for you), a background process starts chiming every 2s and,
  on macOS, shows a notification.
- afterShellExecution / afterMCPExecution / postToolUseFailure / stop /
  sessionEnd: the command ran, was rejected, or the agent stopped, so the
  chime stops.

Install (adds entries to ~/.cursor/hooks.json, keeping any existing hooks,
and backs the old file up to hooks.json.bak):
    python3 cursor_hooks.py --install
Remove:
    python3 cursor_hooks.py --uninstall
Hear the alert without Cursor:
    python3 cursor_hooks.py --test

Safety: Cursor requires before-hooks to answer with a permission. This script
always answers "ask", the most cautious option, so it can never auto-approve
a command. Depending on your Cursor version, that may make Cursor ask before
commands it would otherwise run automatically.

Limitation: Cursor has no "user clicked Run" event, only before/after. If you
approve a long-running command, the chime continues until it finishes.
"""

import argparse
import io
import json
import math
import re
import struct
import subprocess
import sys
import tempfile
import time
import uuid
import wave
from pathlib import Path

ALERT_DELAY_SECONDS = 3.0         # commands that finish sooner never chime
ALERT_REPEAT_SECONDS = 2.0
ALERT_TIMEOUT_SECONDS = 15 * 60   # give up if no "after" hook ever arrives

START_EVENTS = ("beforeShellExecution", "beforeMCPExecution")
STOP_EVENTS = ("afterShellExecution", "afterMCPExecution",
               "postToolUseFailure", "stop", "sessionEnd")

SCRIPT = Path(__file__).resolve()
HOOKS_FILE = Path.home() / ".cursor" / "hooks.json"
STATE_DIR = Path(tempfile.gettempdir()) / "cursor-approval-alert"

# ------------------------------------------------------------------- sound --

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
            STATE_DIR.mkdir(exist_ok=True)
            _alert_wav_path = STATE_DIR / "alert.wav"
            _alert_wav_path.write_bytes(make_alert_sound())
        subprocess.run(["afplay", str(_alert_wav_path)], check=False)
    else:
        print("\a", end="", flush=True)  # unsupported platform: terminal bell


def notify(message):
    """Show a desktop notification (macOS only; Windows gets the sound)."""
    if sys.platform == "darwin":
        # pass text as arguments, never spliced into the script, so command
        # text containing quotes can't break or inject AppleScript
        subprocess.run(
            ["osascript",
             "-e", "on run argv",
             "-e", "display notification (item 1 of argv) with title (item 2 of argv)",
             "-e", "end run",
             message, "Cursor is waiting for approval"],
            check=False, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


# ------------------------------------------------------------------- alert --
# A marker file per conversation holds a random token while an approval may
# be pending. The background alert keeps chiming only while the marker still
# holds its token; a stop hook deletes it, and a newer start overwrites it.

def marker_path(conversation_id):
    key = re.sub(r"[^A-Za-z0-9_-]", "_", conversation_id or "default")[:100]
    return STATE_DIR / f"{key}.waiting"


def start_alert(marker, message):
    STATE_DIR.mkdir(exist_ok=True)
    token = uuid.uuid4().hex
    marker.write_text(token)
    cmd = [sys.executable, str(SCRIPT), "--alert", str(marker), token, message]
    kwargs = dict(stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                  stderr=subprocess.DEVNULL, close_fds=True)
    if sys.platform == "win32":
        flags = subprocess.DETACHED_PROCESS | subprocess.CREATE_NEW_PROCESS_GROUP
        try:
            subprocess.Popen(cmd, creationflags=flags | subprocess.CREATE_BREAKAWAY_FROM_JOB,
                             **kwargs)
        except OSError:  # Cursor's job object may not allow breakaway
            subprocess.Popen(cmd, creationflags=flags, **kwargs)
    else:
        subprocess.Popen(cmd, start_new_session=True, **kwargs)


def run_alert(marker, token, message):
    """Background process: chime until the marker is cleared or replaced."""
    def still_waiting():
        try:
            return marker.read_text() == token
        except OSError:
            return False

    def wait(seconds):
        end = time.monotonic() + seconds
        while time.monotonic() < end:
            if not still_waiting():
                return False
            time.sleep(0.2)
        return still_waiting()

    if not wait(ALERT_DELAY_SECONDS):
        return  # finished or rejected quickly; nothing to announce
    notify(message)
    deadline = time.monotonic() + ALERT_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        play_alert()
        if not wait(ALERT_REPEAT_SECONDS):
            return
    if still_waiting():
        marker.unlink(missing_ok=True)


def describe(event):
    if event.get("tool_name"):
        text = f"{event.get('mcp_server_name', 'MCP')}: {event['tool_name']}"
    else:
        text = event.get("command") or "Agent action"
    text = " ".join(text.split())
    return text if len(text) <= 120 else text[:117] + "..."


def handle_hook(name):
    """Entry point when Cursor runs this as a hook (JSON event on stdin)."""
    try:
        event = json.load(sys.stdin)
    except ValueError:
        event = {}
    marker = marker_path(event.get("conversation_id"))

    if name in START_EVENTS:
        try:
            start_alert(marker, describe(event))
        except Exception:
            pass  # never let an alert problem get in the agent's way
        # "ask" is the most cautious answer: it can never auto-approve
        print(json.dumps({"permission": "ask"}))
    else:
        marker.unlink(missing_ok=True)


# ----------------------------------------------------------------- install --

def hook_command(name):
    return f'"{sys.executable}" "{SCRIPT}" --hook {name}'


def is_ours(entry):
    return SCRIPT.name in entry.get("command", "") and "--hook" in entry.get("command", "")


def load_hooks_config():
    if not HOOKS_FILE.exists():
        return {"version": 1, "hooks": {}}
    try:
        return json.loads(HOOKS_FILE.read_text())
    except ValueError as e:
        sys.exit(f"{HOOKS_FILE} isn't valid JSON ({e}); fix it and try again.")


def save_hooks_config(config):
    HOOKS_FILE.parent.mkdir(exist_ok=True)
    if HOOKS_FILE.exists():
        HOOKS_FILE.with_name("hooks.json.bak").write_text(HOOKS_FILE.read_text())
    HOOKS_FILE.write_text(json.dumps(config, indent=2) + "\n")


def install():
    config = load_hooks_config()
    hooks = config.setdefault("hooks", {})
    for name in START_EVENTS + STOP_EVENTS:
        entries = [e for e in hooks.get(name, []) if not is_ours(e)]
        entries.append({"command": hook_command(name), "timeout": 10})
        hooks[name] = entries
    save_hooks_config(config)
    print(f"Installed hooks in {HOOKS_FILE}")
    print("Restart Cursor (or reload the window) to activate them.")


def uninstall():
    if not HOOKS_FILE.exists():
        print("Nothing to remove.")
        return
    config = load_hooks_config()
    hooks = config.get("hooks", {})
    for name in list(hooks):
        hooks[name] = [e for e in hooks[name] if not is_ours(e)]
        if not hooks[name]:
            del hooks[name]
    save_hooks_config(config)
    for marker in STATE_DIR.glob("*.waiting"):
        marker.unlink(missing_ok=True)
    print(f"Removed hooks from {HOOKS_FILE}")


def test():
    print("Playing the alert 3 times (and a notification on macOS)...")
    notify("Test: npm install")
    for _ in range(3):
        play_alert()
        time.sleep(ALERT_REPEAT_SECONDS)


def main():
    p = argparse.ArgumentParser(
        description="Persistent Cursor approval alerts via Cursor Hooks.")
    g = p.add_mutually_exclusive_group(required=True)
    g.add_argument("--install", action="store_true", help="add hooks to ~/.cursor/hooks.json")
    g.add_argument("--uninstall", action="store_true", help="remove hooks from ~/.cursor/hooks.json")
    g.add_argument("--test", action="store_true", help="play the alert to check sound works")
    g.add_argument("--hook", metavar="EVENT", choices=START_EVENTS + STOP_EVENTS,
                   help=argparse.SUPPRESS)  # used by Cursor
    g.add_argument("--alert", nargs=3, metavar=("MARKER", "TOKEN", "MESSAGE"),
                   help=argparse.SUPPRESS)  # background chime process
    args = p.parse_args()

    if args.hook:
        handle_hook(args.hook)
    elif args.alert:
        marker, token, message = args.alert
        run_alert(Path(marker), token, message)
    elif args.install:
        install()
    elif args.uninstall:
        uninstall()
    else:
        test()


if __name__ == "__main__":
    main()
