# codex-usage-overlay

A tiny, dependency-free desktop overlay for local Codex usage signals.

`codex-usage-overlay` reads Codex's local session JSONL files and local telemetry
database to show reported remaining rate-limit percentages, reset countdowns, a
manual token counter, and an optional API-equivalent cost estimate. It is built
as a single Python/Tkinter `.pyw` app so it can run quietly on Windows without a
console window.

This is an independent utility and is not an official OpenAI or Codex project.

## Features

- Always-on-top, draggable, translucent Tkinter overlay.
- Shows Codex-reported 5-hour and 7-day remaining percentages.
- Uses the freshest local source available: `logs_2.sqlite` rate-limit websocket
  events first, session JSONL rate events as fallback.
- Optional reset countdowns in the overlay.
- Optional manual token counter with input, cached input, output, reasoning, and
  total token details.
- Optional API-equivalent cost estimate using local token counts and documented
  OpenAI API pricing constants.
- Right-click menu for visibility modes, display toggles, layout, refresh, reset
  position, token-counter reset, and status details.
- Package-aware Windows visibility for both legacy `codex.exe` and the unified
  `ChatGPT.exe` host shipped in the `OpenAI.Codex` package.
- No third-party packages, no network calls, and no `auth.json` access.

## Requirements

- Windows is the primary supported platform.
- Python 3.11 or newer with Tkinter available.
- Codex CLI or the Codex-capable ChatGPT desktop app installed and writing local
  session/log data under `~/.codex`.

Limited non-Windows behavior is supported for always-visible mode and local log
reading, but foreground/window visibility detection is Windows-only.

## Run

From this folder:

```powershell
python codex_usage_overlay.pyw
```

To launch without a console window on Windows:

```powershell
pythonw codex_usage_overlay.pyw
```

Useful command-line checks:

```powershell
python codex_usage_overlay.pyw --print-status
python codex_usage_overlay.pyw --version
python codex_usage_overlay.pyw --help
```

## Start On Windows Login

Create a shortcut in your Startup folder that points to `codex_usage_overlay.pyw`
or runs `pythonw` with the script path as its argument.

Open the Startup folder:

```powershell
explorer shell:startup
```

Then create a shortcut with:

- Target: `pythonw`
- Arguments: full path to `codex_usage_overlay.pyw`
- Start in: the folder containing `codex_usage_overlay.pyw`

## Settings

Settings are stored beside the script in:

```text
codex_usage_overlay.settings.json
```

This file is intentionally ignored by git because it may contain local screen
coordinates and personal preferences. Use
`codex_usage_overlay.settings.example.json` as a safe reference.

Supported settings include:

- `visibility_mode`: `always`, `process`, `foreground`, or `visible_window`
- `display_windows`: `primary`, `secondary`, or both
- `layout_mode`: `horizontal`, `vertical`, or `grid_2x2`
- `position`: saved overlay coordinates
- `opacity`: `0.2` through `1.0`
- `show_resets`: show reset countdowns
- `show_token_counter`: show manual token counter
- `show_api_cost_estimate`: show API-equivalent cost estimate

## Privacy And Data

The overlay reads local files only:

- `~/.codex/sessions/**/*.jsonl`
- `~/.codex/logs_2.sqlite`
- `~/.codex/config.toml` for model detection fallback

It does not call OpenAI APIs, upload data, read `auth.json`, or require an API
key. The runtime state file is written to the user temp directory and deleted on
normal quit.

## Limitations

- Codex local log formats are unofficial implementation details and may change.
- Displayed rate limits are only as fresh as the local Codex logs.
- The API cost estimate is approximate and is not actual Codex subscription
  billing.
- Preview-only Codex models without published API pricing use a clearly labeled
  GPT-5.5 proxy. Unknown, custom, and future models remain unpriced.
- If models change during a manual token-counter window, reset the counter for a
  cleaner cost estimate.

## Troubleshooting

### I launched it but do not see it

The overlay saves its last screen position in `codex_usage_overlay.settings.json`.
If your monitor layout changed, delete the `position` value or set it to `null`.
The app also clamps saved positions to the visible screen area on launch.

For the unified Windows desktop app, use version `0.1.7` or newer so the
`foreground` and `visible_window` modes recognize the packaged `ChatGPT.exe`
host. Other ChatGPT installations are intentionally ignored.

### Dragging or right-click menu feels unreliable

Update to `0.1.5` or newer, then check for old processes and restart the app.
The right-click menu is a custom owned Tk window rather than a native Tk popup,
which avoids first clicks falling through to Codex behind the overlay. The
overlay also prevents duplicate instances so overlapping windows do not fight
for clicks.

```powershell
Get-CimInstance Win32_Process | Where-Object { $_.CommandLine -like '*codex_usage_overlay.pyw*' }
```

Only one instance should be running. New launches exit cleanly if another
overlay instance is already active.

## Development

Run the tests:

```powershell
python -m unittest -v test_codex_usage_overlay.py
python -m py_compile codex_usage_overlay.pyw test_codex_usage_overlay.py
```

The project intentionally uses only the Python standard library.
