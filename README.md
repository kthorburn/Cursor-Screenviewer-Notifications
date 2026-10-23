# Cursor Approval Alert

**Never miss a paused Cursor agent again.** Cursor Approval Alert gives you a persistent, repeating audio alert whenever [Cursor](https://cursor.com)'s Agent is waiting for you to approve an **Allow** or **Run** prompt. The chime keeps going until you respond.

It's built for **corporate and enterprise environments where manual run approval is required** and auto-run is turned off.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows%20%7C%20macOS-0078D6)
![Works with Cursor](https://img.shields.io/badge/works%20with-Cursor-black)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

<p>
  <img src="screen-watcher/templates/allow_button.png" alt="Cursor Allow button template" height="32">
  <img src="screen-watcher/templates/run_button.png" alt="Cursor Run button template" height="32">
  <img src="screen-watcher/templates/run_button_enter.png" alt="Cursor Run (Enter) button template" height="32">
</p>

---

## The problem

Cursor's Agent mode stops and waits for you to approve terminal commands and tool calls. If you've moved to another window, another monitor or another task, the agent can sit idle for minutes or hours before you notice.

## Built for environments that require manual approval

Many companies don't allow Cursor's auto-run mode. Security, compliance or IT policy says a person must review and click **Run** or **Allow** before the agent executes anything. That keeps a human in the loop, but it also means watching the chat constantly, or coming back to find the agent has been stalled for an hour.

Cursor Approval Alert removes the babysitting without weakening that policy:

- **It never approves anything.** Neither method clicks, presses keys or auto-accepts. Every command still needs your manual approval.
- **The hooks method can only make Cursor more cautious.** Its only permission answer is `ask`, so it can't override an "ask before running" policy.
- **Nothing leaves your machine.** No network calls, telemetry or external services. Screenshots are processed in memory and never saved.
- **Small and auditable.** Each method is a single Python file that a security team can review in minutes. The hooks method uses only Python's standard library.
- **Works on locked-down machines.** The hooks method needs no admin rights or OS permissions. It installs per user in `~/.cursor/hooks.json` and doesn't touch any enterprise-managed hooks file. On managed Macs where IT blocks Screen Recording, use the hooks method.

You keep the oversight your organization requires and lose the idle time.

## Two ways to get alerted

This repo has two independent methods. Pick the one that fits your machine.

| | [Screen watcher](#method-1-screen-watcher) | [Cursor hooks](#method-2-cursor-hooks) |
| --- | --- | --- |
| Folder | [`screen-watcher/`](screen-watcher/) | [`cursor-hooks/`](cursor-hooks/) |
| How it detects a prompt | Looks for the Allow/Run button on screen | Cursor tells it a command is about to run |
| **Windows** | ✅ Works out of the box | ✅ Works |
| **macOS** | ⚠️ Needs Screen Recording permission | ✅ Works, no permissions |
| Extra packages | `numpy`, `pillow`, `mss`, `pyautogui` | None (Python standard library only) |
| Needs to stay running | Yes, in a terminal | No, Cursor runs it when needed |
| Accuracy | Alerts only while the button is visible | Alerts while a command is pending (see [limitations](#limitations)) |

**Recommendation:** on Windows, use the screen watcher. On a Mac, use Cursor hooks unless you're happy to grant Screen Recording permission.

---

## Method 1: Screen watcher

[`screen-watcher/screen_watcher.py`](screen-watcher/screen_watcher.py) scans **every monitor** about once a second. While one of Cursor's approval buttons is visible, it plays a soft chime every 2 seconds. The chime stops as soon as the button goes away.

- **Never clicks:** it only looks at the screen. It never moves the mouse or presses anything.
- **Multi-monitor:** scans the whole virtual desktop, so Cursor can be on any screen.
- **No OpenCV:** template matching is pure NumPy (FFT-based normalized cross-correlation), so it runs on **Windows on ARM** too.
- **Automatic size matching:** finds buttons from **0.5× to 2×** the size of the template, so the same templates work across Windows display scaling (100%–200%), Retina and non-Retina screens, and different machines. Once a button is found, the watcher remembers its on-screen size (in `templates/.learned_scales.json`) and searches around that size first, which keeps later scans fast.

### Windows

```bash
git clone https://github.com/kthorburn/Cursor-Screenviewer-Notifications.git
cd Cursor-Screenviewer-Notifications/screen-watcher
pip install -r requirements.txt
python screen_watcher.py
```

The included templates match Cursor's default dark theme on Windows. Press **Ctrl+C** to stop.

### macOS

> [!IMPORTANT]
> **macOS requires Screen Recording permission for the screen watcher.** Without it, macOS only gives the script your wallpaper, so it can never see Cursor's buttons. The script checks for this at startup and exits with instructions if it's missing. If you'd rather not grant this permission, use [Cursor hooks](#method-2-cursor-hooks) instead.

1. Open **System Settings → Privacy & Security → Screen Recording** and turn on the app you'll run the script from (Terminal, iTerm, Cursor or VS Code).
2. **Quit and reopen that app.** The permission doesn't apply until you do.
3. Install and run. The bundled templates come from Windows, and size differences are handled automatically. If a button still isn't detected (for example, because of a different theme or font rendering), capture your own on the Mac.

```bash
cd Cursor-Screenviewer-Notifications/screen-watcher
pip3 install -r requirements.txt
python3 screen_watcher.py

# only if the bundled templates aren't detected:
python3 screen_watcher.py --capture allow_button   # with an Allow button visible in Cursor
python3 screen_watcher.py --capture run_button     # with a Run button visible in Cursor
```

Screen Recording lets that app see everything on your screen. Only grant it to apps you trust.

### Capturing button templates

```bash
python screen_watcher.py --capture allow_button
```

1. You get a 5-second countdown. Put your mouse on the button's **top-left corner**.
2. You get a second 5-second countdown. Put your mouse on the button's **bottom-right corner**.
3. The crop is saved as `screen-watcher/templates/allow_button.png`.

Every PNG in `templates/` is loaded automatically, so you can capture as many buttons as you want.

### Options

| Option | Default | Description |
| --- | --- | --- |
| `--capture NAME` | n/a | Capture a new button template to `templates/NAME.png`. |
| `--template PATH` | n/a | Watch for an extra template PNG stored elsewhere. Can be given more than once. |
| `--interval SECONDS` | `1.0` | Time between screen scans. |
| `--threshold 0-1` | `0.88` | Match confidence required. Lower it if buttons are missed; raise it if you get false alerts. |

### Troubleshooting

| Symptom | Fix |
| --- | --- |
| A button is on screen but there is no alert | Recapture the template at your current Cursor theme and zoom, or lower `--threshold` (for example to `0.82`). |
| "Screen Recording permission is required" (macOS) | Grant the permission as described above, then **quit and reopen** the app you run the script from. |
| Alerts fire when no button is visible | Raise `--threshold`, or recapture a tighter crop. |
| High CPU usage | Increase `--interval` (for example to `2`). Scans are heavier until the first button has been seen, because every size is searched. After that, the learned size keeps them light. |

---

## Method 2: Cursor hooks

[`cursor-hooks/cursor_hooks.py`](cursor-hooks/cursor_hooks.py) uses [Cursor Hooks](https://cursor.com/docs/agent/hooks), scripts that Cursor itself runs at points in the agent's work. It never reads your screen, so it needs **no Screen Recording or other permissions**, and it only uses Python's standard library.

### Install

```bash
git clone https://github.com/kthorburn/Cursor-Screenviewer-Notifications.git
cd Cursor-Screenviewer-Notifications/cursor-hooks
python3 cursor_hooks.py --test      # hear the alert (and see a notification on macOS)
python3 cursor_hooks.py --install   # add the hooks to ~/.cursor/hooks.json
```

Then restart Cursor. Use `python` instead of `python3` on Windows.

`--install` keeps any hooks you already have and saves a backup to `~/.cursor/hooks.json.bak`. To remove the hooks again, run:

```bash
python3 cursor_hooks.py --uninstall
```

The hooks point at the script where it is now, so if you move the folder, run `--install` again.

### How it works

1. **Before** the agent runs a terminal command or MCP tool, Cursor calls the hook. If the command still hasn't run 3 seconds later, it's waiting for you, so the alert starts. You hear a chime every 2 seconds and, on macOS, see a notification showing the command.
2. **After** the command runs, is rejected or fails, or when the agent stops, Cursor calls the hook again and the chime stops.
3. As a safety net, the chime gives up after 15 minutes.

To keep the macOS notification on screen until you dismiss it, open **System Settings → Notifications → Script Editor** and set the alert style to **Persistent**.

### Safety

Cursor requires the "before" hooks to answer with a permission. This script always answers **`ask`**, the most cautious option, so **it can never auto-approve a command**. Depending on your Cursor version, this may make Cursor ask before commands it would otherwise run automatically.

### Limitations

- Cursor has no "you clicked Run" event, only events before and after a command. If you approve a long-running command, the chime continues until that command finishes.
- Commands that take longer than 3 seconds to run may chime even if Cursor didn't need your approval.
- It covers terminal commands and MCP tools, the actions Cursor's approval prompts are for.

---

## FAQ

**Does either method auto-approve or click anything?**
No. Approving agent actions is always left to you.

**Does it send any data anywhere?**
No. Everything runs locally. The screen watcher processes screenshots in memory and never saves them.

**Does it work with other editors or AI agents?**
Not yet. This project currently targets Cursor only. See the roadmap below.

## Roadmap

- [x] Windows screen watcher
- [x] macOS support
- [x] Cursor hooks method (no screen capture)
- [ ] **More IDEs and agents:** VS Code (GitHub Copilot agent mode), Windsurf, Claude Code, Cline and others
- [ ] Windows toast notifications for the hooks method
- [ ] Linux support
- [ ] Light-theme and macOS templates for the screen watcher

### Request an IDE

Want support for your editor or agent? [Open an issue](https://github.com/kthorburn/Cursor-Screenviewer-Notifications/issues/new) titled **"IDE request: \<name\>"** and, if you can, attach a screenshot of its approval button. Requests with the most 👍 reactions are prioritized.

## Contributing

Issues and pull requests are welcome, especially items from the roadmap.

## License

[MIT](LICENSE)

---

<sub>Keywords: Cursor enterprise, Cursor manual approval required, Cursor auto-run disabled, corporate AI coding policy, human-in-the-loop compliance, Cursor AI notification, Cursor agent waiting for approval, Cursor Allow button alert, Cursor Run command approval sound, Cursor agent mode paused, Cursor hooks, Cursor IDE productivity, macOS Cursor notifications, human-in-the-loop notifications, screen watcher, template matching in Python.</sub>
