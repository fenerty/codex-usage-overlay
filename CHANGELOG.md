# Changelog

## 0.1.11 - 2026-09-01

- Omit the rate-window label when exactly one effective window has a known
  remaining percentage, so current weekly-only telemetry renders as `33%`
  instead of `7d 33%`.
- Apply the same compact display when a user selects one of two available
  windows, while preserving labels for multiple displayed windows and for
  telemetry without a usable percentage.
- Preserve optional reset countdowns, diagnostic labels, settings behavior,
  runtime state, and command-line status output.

## 0.1.10 - 2026-08-29

- Open the custom context menu on right-button release and keep the overlay
  visible for the complete menu interaction.
- Keep the parent overlay topmost while its menu is open, handle outside clicks
  on both press and release, and use a mouse-button-only Windows watchdog when
  an activation click is not delivered to Tk.
- Centralize command, Escape, outside-click, confirmed focus-loss,
  display-change, replacement, repeated-open, and quit cleanup in an
  idempotent menu lifecycle.
- Confirm focus loss across two debounced checks and wait 250 ms after menu
  closure before reconciling foreground visibility, allowing Windows to return
  focus to the ChatGPT desktop host.
- Treat dragging as an overlay interaction and hold foreground-mode visibility
  through motion and a 250 ms post-release reconciliation window.
- Cancel popup, focus, visibility, refresh, and display callbacks before menu
  replacement or shutdown so destroyed Tk roots are never accessed.
- Retry SQLite immediately after transient read failures even when filesystem
  signatures collide, and add a lightweight content fingerprint to the
  database/WAL/SHM signatures used for incremental polling.
- Save settings through an atomic temporary-file replacement, preserve the
  previous file on failure, and expose sanitized save errors instead of
  silently discarding them.
- Add local runtime diagnostics for overlay/menu/drag state, the last menu closure,
  UI and settings errors, visibility mode, and the detected packaged desktop
  host build.
- Preserve existing settings, CLI behavior, privacy boundaries, and standard
  library-only packaging.

## 0.1.9 - 2026-07-17

- Recover the overlay into an active monitor work area after live
  display-topology, resolution, taskbar work-area, and sleep/resume changes.
- Combine lightweight topology polling with debounced native Windows
  notifications, and persist stable automatically corrected coordinates.
- Keep display recovery active while visibility modes withdraw the overlay,
  without changing the existing Python/Tk DPI-awareness mode.
- Keep the existing 500 ms refresh while shown, use a 1-second hidden
  visibility wake and 5-second hidden log ingestion, and catch up immediately
  before showing.
- Make SQLite reads and session-file discovery incremental during steady-state
  polling, and write the runtime heartbeat every 2 seconds.
- Use database/WAL/SHM signatures plus a 5-second maximum-ID safety probe, and
  retain a 30-second recursive session-discovery fallback.
- Reuse unchanged labels, visibility state, and monitor snapshots; enumerate
  displays after native notifications or on a 5-second fallback instead of on
  every refresh or drag event.
- Reduce background work without adding dependencies or changing the UI,
  settings, data sources, or displayed calculations.

## 0.1.8 - 2026-07-13

- Replace GPT-5.6 proxy estimates with published Sol, Terra, and Luna Standard
  API prices, including per-request long-context pricing above 272,000 input
  tokens.
- Expose cache-write rates as metadata while excluding cache-write premiums from
  totals because local Codex events do not report cache-write token counts.
- Render only rate windows present in telemetry, including the current single
  weekly-window shape, while retaining legacy 5-hour plus weekly parsing.
- Replace missing-window placeholders with one waiting state when no rate data is
  available.
- Remove the 2x2 grid layout and migrate saved grid settings to horizontal.

## 0.1.7 - 2026-07-09

- Recognize the unified Windows `ChatGPT.exe` host only when it belongs to the
  `OpenAI.Codex` package.
- Restore process, foreground, and visible-window modes for the new desktop app.
- Add exact GPT-5.4 mini API pricing and labeled GPT-5.5 proxy estimates for
  preview-only Codex models.
- Include pricing-model and proxy metadata in the runtime state file.

## 0.1.6 - 2026-07-09

- Keep the context menu within the active monitor's usable work area.
- Add scrolling and adaptive text wrapping for menus on constrained displays.
- Move status and usage diagnostics into a dedicated Details submenu.

## 0.1.5 - 2026-06-08

- Replace the native Tk context menu with a custom owned Tk menu window.
- Capture menu clicks explicitly so first-click menu selections do not pass through to the Codex window underneath.
- Keep the custom menu topmost while the overlay itself stays beneath it.

## 0.1.4 - 2026-06-07

- Prevent the overlay refresh loop from raising the overlay over the context menu.
- Defer rendering and topmost refreshes while the right-click menu is open.

## 0.1.3 - 2026-06-07

- Replace Tk menu checkbuttons/radiobuttons with plain command items to fix delayed toggles.
- Add horizontal, vertical, and 2x2 overlay layout modes.
- Render rate windows, token counter, and API estimate as grid-positioned widgets.

## 0.1.2 - 2026-06-07

- Fix a 0.1.1 regression that could make dragging stop working.
- Restore first-click right-click menu behavior for toggles and commands.
- Keep the single-instance lock and visible-screen position clamp from 0.1.1.

## 0.1.1 - 2026-06-07

- Improve dragging by capturing mouse movement for the full drag operation.
- Defer visual refreshes while dragging or using the right-click menu.
- Open the right-click menu on release and apply menu toggles from explicit states.
- Clamp saved positions to the visible screen area to avoid off-screen launches.
- Add a temp-file single-instance lock to avoid overlapping duplicate overlays.

## 0.1.0 - 2026-06-02

- Initial public source release.
- Add Codex usage overlay with 5-hour and 7-day rate-limit percentages.
- Add reset countdown display, manual token counter, and API-equivalent cost estimate.
- Add local SQLite rate-limit source for fresher Codex-reported percentages.
- Add Windows visibility modes and draggable always-on-top overlay.
- Add unit tests and GitHub Actions workflow.
