# Changelog

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
