# Cursor Approval Alert

**Never miss a paused Cursor agent again.** Cursor Approval Alert is a lightweight screen watcher for the [Cursor](https://cursor.com) AI code editor. It plays a repeating audio alert for as long as Cursor's Agent is waiting on an **Allow** or **Run** approval button.

![Python](https://img.shields.io/badge/python-3.9%2B-blue)
![Platform](https://img.shields.io/badge/platform-Windows-0078D6)
![Works with Cursor](https://img.shields.io/badge/works%20with-Cursor-black)
![License: MIT](https://img.shields.io/badge/license-MIT-green)

<p>
  <img src="templates/allow_button.png" alt="Cursor Allow button template" height="32">
  <img src="templates/run_button.png" alt="Cursor Run button template" height="32">
  <img src="templates/run_button_enter.png" alt="Cursor Run (Enter) button template" height="32">
</p>

---

## The problem

Cursor's Agent mode stops and waits for you to approve terminal commands, tool calls and file edits. If you've moved to another window, another monitor or another task, the agent can sit idle for minutes or hours before you notice.

## The solution

`approval_alert.py` watches **every monitor** for Cursor's approval buttons. Whenever one shows up, it plays a soft chime and **repeats it every 2 seconds until the button is gone**, so the notification persists until you respond.

- **Persistent alerts:** the chime keeps repeating until you approve or reject the prompt.
- **Multi-monitor:** scans the whole virtual desktop on every cycle, so Cursor can be on any screen.
- **Never clicks:** detection only. It never moves the mouse or presses a button for you, so you stay in control of what the agent runs.
- **No OpenCV:** template matching is written in pure NumPy (FFT-based normalized cross-correlation), so it also runs on **Windows on ARM**.
- **DPI- and scale-tolerant:** handles display scaling (125%, 150%, …) and small size differences between the captured button and the one on screen.
- **Ready out of the box:** templates for Cursor's **Allow**, **Run** and **Run ⏎** buttons are included.

## Quick start

```bash
git clone https://github.com/KenThorburn/cursor-approval-alert.git
cd cursor-approval-alert
pip install -r requirements.txt
python approval_alert.py
```

The included templates in [`templates/`](templates/) match Cursor's default dark theme. If your theme, font or zoom level looks different, capture your own (see below).

Press **Ctrl+C** to stop.

## Capturing your own Cursor buttons

If the included templates don't match your setup, or you want to watch for other Cursor buttons, capture a new template. Make sure the button is visible in Cursor, then run:

```bash
python approval_alert.py --capture allow_button
```

1. You get a 5-second countdown. Put your mouse on the button's **top-left corner**.
2. You get a second 5-second countdown. Put your mouse on the button's **bottom-right corner**.
3. The crop is saved as `templates/allow_button.png`.

Every PNG in `templates/` is loaded automatically, so you can capture as many buttons as you want (`run_button`, `accept_all`, `continue`, …).

## Command-line options

| Option | Default | Description |
| --- | --- | --- |
| `--capture NAME` | n/a | Capture a new button template to `templates/NAME.png`. |
| `--template PATH` | n/a | Watch for an extra template PNG stored outside `templates/`. Can be given more than once. |
| `--interval SECONDS` | `1.0` | Time between screen scans. |
| `--threshold 0-1` | `0.88` | Match confidence required to count as a hit. Lower it if real buttons are missed; raise it if you get false alerts. |

Examples:

```bash
python approval_alert.py --interval 2 --threshold 0.86
python approval_alert.py --template C:\path\to\accept_button.png
```

## How it works

1. **Capture:** [`mss`](https://github.com/BoboTiG/python-mss) grabs the full virtual desktop (all monitors) and converts it to grayscale.
2. **Coarse search:** the screen and each template are downscaled, and a zero-mean normalized cross-correlation (ZNCC, the same measure as OpenCV's `TM_CCOEFF_NORMED`) is computed with an FFT. Several template scales are tried to cope with display scaling.
3. **Fine verification:** each candidate is re-checked at full resolution on a small patch, which filters out things that only look similar.
4. **Alert:** while any button is found, a quiet C-major arpeggio is synthesized in memory and played every 2 seconds. When the button disappears, the alert stops.

All sums use float64 integral images, so large multi-monitor desktops don't produce false positives from precision loss.

## Requirements

- **Cursor** (Agent mode)
- **Windows 10/11** (x64 or ARM64). Audio uses the built-in `winsound` module.
- **Python 3.9+**
- `numpy`, `pillow`, `mss`, `pyautogui` (installed from `requirements.txt`)

## Troubleshooting

| Symptom | Fix |
| --- | --- |
| A Cursor button is on screen but there is no alert | Recapture the template at your current Cursor theme and zoom level, or lower `--threshold` (for example to `0.82`). |
| Alerts fire when no button is visible | Raise `--threshold`, or recapture a tighter crop that contains only the button. |
| High CPU usage | Increase `--interval` (for example to `2`). |
| `warn:` messages in the console | Usually a transient capture error (locked screen, monitor asleep). The watcher keeps running. |

## FAQ

**Does it auto-approve or auto-click anything in Cursor?**
No. It only observes the screen and plays a sound. Approving agent actions is left to you on purpose.

**Does it send any data anywhere?**
No. Screenshots are processed in memory and are never saved or sent anywhere.

**Does it work with other editors or AI agents?**
Not officially yet. This project currently targets Cursor only. Support for other IDEs is planned; see the roadmap below.

## Roadmap

Planned, roughly in priority order:

- [ ] **More IDEs and agents:** VS Code (GitHub Copilot agent mode), Windsurf, Claude Code, Cline and others, each with built-in button templates
- [ ] **macOS and Linux support:** cross-platform audio in place of `winsound`
- [ ] **Desktop toast notifications** as well as the sound
- [ ] **Light-theme templates** for Cursor

### Request an IDE

Want support for your editor or agent? [Open an issue](https://github.com/KenThorburn/cursor-approval-alert/issues/new) titled **"IDE request: \<name\>"** and, if you can, attach a screenshot of its approval button. Requests with the most 👍 reactions are prioritized.

## Contributing

Issues and pull requests are welcome, especially items from the roadmap and template improvements for different Cursor themes.

## License

[MIT](LICENSE)

---

<sub>Keywords: Cursor AI notification, Cursor agent waiting for approval, Cursor Allow button alert, Cursor Run command approval sound, Cursor agent mode paused, Cursor IDE productivity, human-in-the-loop notifications, screen watcher, template matching in Python.</sub>
