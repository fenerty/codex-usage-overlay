# codex-usage-overlay

A tiny, dependency-free desktop overlay for local Codex usage signals.

`codex-usage-overlay` reads Codex's local session JSONL files and local telemetry
database to show the main Codex allowance's reported remaining rate-limit
percentages, reset countdowns, a manual token counter, and an optional
API-equivalent cost estimate. It is built as a single Python/Tkinter `.pyw` app
so it can run quietly on Windows without a console window.

This is an independent utility and is not an official OpenAI or Codex project.

## Features

- Always-on-top, draggable, translucent Tkinter overlay.
- Shows whichever rate-limit windows are present for the main `codex` allowance.
  One effective window renders as a compact percentage such as `33%`; legacy
  5-hour plus weekly telemetry still renders both windows with their labels.
- Ignores separate model-specific allowance buckets such as
  GPT-5.3-Codex-Spark, so they cannot replace the main Codex percentage.
- Uses the freshest local source available: `logs_2.sqlite` rate-limit websocket
  events first, session JSONL rate events as fallback.
- Optional reset countdowns in the overlay.
- Optional manual token counter with input, cached input, output, reasoning, and
  total token details.
- Optional API-equivalent cost estimate using local token counts and documented
  OpenAI Standard API pricing, including per-request long-context premiums.
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
- `display_windows`: `primary`, `secondary`, or both. Only slots present in the
  latest telemetry are offered or rendered; a stale missing-slot selection falls
  back to the available window. A single effective selection omits its window
  label when a remaining percentage is available.
- `layout_mode`: `horizontal` or `vertical`. Legacy `grid_2x2` values migrate to
  `horizontal` the next time settings are saved.
- `position`: saved overlay coordinates
- `opacity`: `0.2` through `1.0`
- `show_resets`: show reset countdowns
- `show_token_counter`: show manual token counter
- `show_api_cost_estimate`: show API-equivalent cost estimate

## Performance And Polling

Version `0.1.10` keeps the existing 500 ms refresh cadence while the overlay is
shown. When a visibility mode withdraws it, the app checks whether it should
wake every 1 second and ingests new log data every 5 seconds. It performs an
immediate log catch-up before showing the overlay again.

SQLite reading and session-file discovery are incremental during steady-state
polling, and the runtime-state heartbeat is written every 2 seconds. These
changes add no dependencies and do not change the UI, settings, data sources,
or displayed calculations.

Database, WAL, and shared-memory metadata plus a small binary fingerprint wake
the SQLite reader, with a 5-second maximum-ID probe as a race-safe fallback.
Transient SQLite errors force a retry on the next poll even if signatures
collide. The newest ten session files and their active date directories are
checked incrementally, with a full recursive discovery every 30 seconds.
Unchanged labels and visibility state are reused, and monitor topology is
normally enumerated only after native display notifications or during a
5-second fallback check.

## Privacy And Data

The overlay reads local files only:

- `~/.codex/sessions/**/*.jsonl`
- `~/.codex/logs_2.sqlite`
- `~/.codex/config.toml` for model detection fallback

It does not call OpenAI APIs, upload data, read `auth.json`, or require an API
key. The runtime state file is written to the user temp directory and deleted on
normal quit. Its additive diagnostics contain only overlay/menu/drag state, sanitized
error text, visibility mode, and the detected packaged desktop build; executable
paths and raw transcript content are not written.

## Limitations

- Codex local log formats are unofficial implementation details and may change.
- Displayed rate limits are only as fresh as the local Codex logs.
- The rate display intentionally tracks only the main `codex` allowance. Token
  counting remains model-independent.
- The API cost estimate is approximate and is not actual Codex subscription
  billing.
- GPT-5.6 Sol, Terra, and Luna use their published short- and long-context
  Standard API prices. The unpublished GPT-5.3-Codex-Spark preview uses a clearly
  labeled GPT-5.5 proxy. Unknown, custom, and future models remain unpriced.
- GPT-5.6 pricing publishes cache-write premiums, but local Codex events do not
  report cache-write token counts. Those rates are exposed as metadata while
  cache-write costs are excluded from the estimate total.
- If models change during a manual token-counter window, reset the counter for a
  cleaner cost estimate.

## Troubleshooting

### I launched it but do not see it

The overlay saves its last screen position in `codex_usage_overlay.settings.json`.
On Windows, version `0.1.9` and newer re-enumerate active monitor work areas
while running and move the overlay back on-screen after docking, undocking,
display rearrangement, resolution changes, taskbar work-area changes, and
sleep/resume. Native Windows notifications accelerate recovery, while
lightweight polling provides a fallback. Stable automatic corrections are
saved so the next launch uses the recovered coordinates.

The `visible_window` mode withdraws the overlay when no non-minimized Codex
window is visible; the overlay process intentionally remains running and keeps
checking display topology before it is shown again. Version `0.1.9` does not
change the Python/Tk process DPI-awareness mode.

If the overlay still cannot be found, set `position` to `null` and restart the
existing process.

For the unified Windows desktop app, use version `0.1.8` or newer so the
`foreground` and `visible_window` modes recognize the packaged `ChatGPT.exe`
host. Other ChatGPT installations are intentionally ignored.

### Dragging or right-click menu feels unreliable

Update to `0.1.10` or newer, then check for old processes and restart the app.
Version `0.1.10` opens the custom owned Tk menu on right-button release and keeps
the topmost overlay visible while a menu or drag interaction owns focus. It
confirms menu focus loss before dismissal, watches mouse buttons without logging
input so Windows activation clicks outside the popup cannot be missed, and waits
250 ms after menu closure or drag release before re-evaluating foreground
visibility. The Details submenu reports the last closure reason, detected desktop
host build, and any sanitized UI or settings-save error. The overlay also prevents
duplicate instances so overlapping windows do not fight for clicks.

The Codex-capable desktop host is updated separately from this utility. Review
the [ChatGPT & Codex changelog](https://learn.chatgpt.com/docs/changelog) when a
host update changes behavior. After installing a desktop update, fully quit and
reopen ChatGPT as described in OpenAI's
[app update guidance](https://learn.chatgpt.com/docs/enterprise/manage-app-updates).

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
