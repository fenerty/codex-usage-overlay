# Changelog

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
