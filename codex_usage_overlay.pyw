"""Tiny Codex usage overlay.

The app reads local Codex session JSONL files and local Codex telemetry logs to
show usage-related status. It does not call network APIs, read auth files, or
upload data.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import atexit
import json
import math
import os
import platform
import re
import sqlite3
import subprocess
import sys
import tempfile
import time
import tkinter as tk
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from tkinter import Menu
from typing import Any


APP_NAME = "Codex Usage Overlay"
APP_VERSION = "0.1.3"
SETTINGS_FILE_NAME = "codex_usage_overlay.settings.json"
RUNTIME_STATE_FILE_NAME = "codex-usage-overlay-state.json"
INSTANCE_LOCK_FILE_NAME = "codex-usage-overlay.lock"
DEFAULT_DISPLAY_WINDOWS = ("primary", "secondary")
VALID_DISPLAY_WINDOWS = ("primary", "secondary")
DEFAULT_LAYOUT_MODE = "horizontal"
VALID_LAYOUT_MODES = ("horizontal", "vertical", "grid_2x2")
VALID_VISIBILITY_MODES = ("always", "process", "foreground", "visible_window")
CODEX_PROCESS_NAMES = {"codex.exe"}
GENERIC_CODEX_PROCESS_NAMES = {"codex", "codex.exe"}
DEFAULT_OPACITY = 0.9
POLL_INTERVAL_MS = 500
MIN_VISIBLE_PIXELS = 24
DEFAULT_OVERLAY_WIDTH = 190
DEFAULT_OVERLAY_HEIGHT = 40
INSTANCE_LOCK_STARTUP_GRACE_SECONDS = 10
INSTANCE_LOCK_HEARTBEAT_STALE_SECONDS = 5
MAX_SESSION_FILES_TO_SCAN = 10
TAIL_BYTES = 1_048_576
RESET_PENDING_GRACE_SECONDS = 60
MODEL_DETECT_INTERVAL_SECONDS = 10
MODEL_LOG_ROWS_TO_SCAN = 250
SQLITE_RATE_ROWS_TO_SCAN = 200
API_PRICING_ASSUMPTION = "Standard API pricing assumed"
API_PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/models/compare"
GPT_55_PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/models/gpt-5.5"
TOKEN_USAGE_KEYS = (
    "input_tokens",
    "cached_input_tokens",
    "output_tokens",
    "reasoning_output_tokens",
    "total_tokens",
)

COLOR_BG = "#15181d"
COLOR_BORDER = "#2f343d"
COLOR_TEXT = "#e7ecf3"
COLOR_MUTED = "#8b949e"
COLOR_GREEN = "#4ade80"
COLOR_AMBER = "#fbbf24"
COLOR_RED = "#fb7185"

MODEL_ASSIGNMENT_RE = re.compile(r'(?:^|[\s{,])model=(?:"([^"]+)"|([^\s},]+))', re.IGNORECASE)
CONFIG_MODEL_RE = re.compile(r'^\s*model\s*=\s*"([^"]+)"\s*$', re.IGNORECASE | re.MULTILINE)
MODEL_LOG_MARKERS = (
    'event.name="codex.sse_event"',
    "session_task.turn",
    "run_sampling_request",
)
SQLITE_RATE_LOG_MARKER = "websocket event:"
SQLITE_RATE_EVENT_TYPE = "codex.rate_limits"


@dataclass(frozen=True)
class RateWindow:
    label: str
    window_minutes: int | None
    used_percent: float | None
    remaining_percent: int | None
    resets_at: int | None


@dataclass(frozen=True)
class RateSnapshot:
    timestamp: str
    primary: RateWindow | None
    secondary: RateWindow | None
    plan_type: str | None
    rate_limit_reached_type: str | None
    source_path: str | None = None
    source_kind: str = "session_jsonl"
    source_observed_at: float | None = None


@dataclass(frozen=True)
class TokenUsage:
    input_tokens: int = 0
    cached_input_tokens: int = 0
    output_tokens: int = 0
    reasoning_output_tokens: int = 0
    total_tokens: int = 0


@dataclass(frozen=True)
class ModelPricing:
    display_name: str
    input_per_million: float
    cached_input_per_million: float | None
    output_per_million: float
    source_url: str


@dataclass(frozen=True)
class DetectedModel:
    model: str | None
    source: str


@dataclass(frozen=True)
class ApiCostEstimate:
    model: str | None
    model_source: str
    pricing: ModelPricing | None
    uncached_input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    input_cost: float | None
    cached_input_cost: float | None
    output_cost: float | None
    total_cost: float | None
    warning: str | None = None


@dataclass(frozen=True)
class DisplayWidget:
    key: str
    text: str
    color: str


@dataclass(frozen=True)
class TokenEvent:
    timestamp: str
    fingerprint: str
    usage: TokenUsage | None
    source_path: str | None = None


@dataclass(frozen=True)
class LogReadBatch:
    snapshot: RateSnapshot | None
    token_events: list[TokenEvent]


# Current official OpenAI API prices, per 1M text tokens.
# Sources:
# - https://developers.openai.com/api/docs/models/gpt-5.5
# - https://developers.openai.com/api/docs/models/compare
API_MODEL_PRICING = {
    "gpt-5.5": ModelPricing("gpt-5.5", 5.00, 0.50, 30.00, GPT_55_PRICING_SOURCE_URL),
    "gpt-5.5-pro": ModelPricing("gpt-5.5 pro", 30.00, None, 180.00, API_PRICING_SOURCE_URL),
    "gpt-5.4": ModelPricing("gpt-5.4", 2.50, 0.25, 15.00, API_PRICING_SOURCE_URL),
}


def parse_percent(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return max(0.0, min(100.0, number))


def whole_remaining_percent_from_used(value: Any) -> int | None:
    used_percent = parse_percent(value)
    if used_percent is None:
        return None
    remaining = 100.0 - used_percent
    return max(0, min(100, math.floor(remaining)))


def window_label(window_minutes: Any, fallback: str) -> str:
    try:
        minutes = int(window_minutes)
    except (TypeError, ValueError):
        return fallback

    if minutes <= 0:
        return fallback
    if minutes % 1440 == 0:
        return f"{minutes // 1440}d"
    if minutes % 60 == 0:
        return f"{minutes // 60}h"
    return f"{minutes}m"


def long_window_label(key: str, rate_window: RateWindow | None) -> str:
    label = rate_window.label if rate_window else default_window_label(key)
    if label.endswith("h"):
        return f"{label[:-1]}-hour limit"
    if label.endswith("d"):
        return f"{label[:-1]}-day limit"
    if label.endswith("m"):
        return f"{label[:-1]}-minute limit"
    return f"{label} limit"


def default_window_label(key: str) -> str:
    return "5h" if key == "primary" else "7d"


def parse_rate_window(raw: Any, fallback_label: str) -> RateWindow | None:
    if not isinstance(raw, dict):
        return None

    used_percent = parse_percent(raw.get("used_percent"))
    remaining = whole_remaining_percent_from_used(raw.get("used_percent"))
    window_minutes = raw.get("window_minutes")
    try:
        parsed_minutes = int(window_minutes) if window_minutes is not None else None
    except (TypeError, ValueError):
        parsed_minutes = None

    resets_at = raw.get("resets_at")
    if resets_at is None:
        resets_at = raw.get("reset_at")
    try:
        parsed_resets_at = int(resets_at) if resets_at is not None else None
    except (TypeError, ValueError):
        parsed_resets_at = None

    return RateWindow(
        label=window_label(parsed_minutes, fallback_label),
        window_minutes=parsed_minutes,
        used_percent=used_percent,
        remaining_percent=remaining,
        resets_at=parsed_resets_at,
    )


def parse_rate_line(
    line: str,
    source_path: str | None = None,
    source_kind: str = "session_jsonl",
    source_observed_at: float | None = None,
) -> RateSnapshot | None:
    if '"rate_limits"' not in line or '"token_count"' not in line:
        return None

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None

    if event.get("type") != "event_msg":
        return None

    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None

    rate_limits = event.get("rate_limits")
    if not isinstance(rate_limits, dict):
        rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None

    primary = parse_rate_window(rate_limits.get("primary"), "5h")
    secondary = parse_rate_window(rate_limits.get("secondary"), "7d")
    if primary is None and secondary is None:
        return None

    return RateSnapshot(
        timestamp=str(event.get("timestamp") or ""),
        primary=primary,
        secondary=secondary,
        plan_type=rate_limits.get("plan_type"),
        rate_limit_reached_type=rate_limits.get("rate_limit_reached_type"),
        source_path=source_path,
        source_kind=source_kind,
        source_observed_at=source_observed_at,
    )


def parse_int(value: Any) -> int:
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0


def parse_token_usage(raw: Any) -> TokenUsage | None:
    if not isinstance(raw, dict):
        return None
    if not any(key in raw for key in TOKEN_USAGE_KEYS):
        return None
    return TokenUsage(
        input_tokens=parse_int(raw.get("input_tokens")),
        cached_input_tokens=parse_int(raw.get("cached_input_tokens")),
        output_tokens=parse_int(raw.get("output_tokens")),
        reasoning_output_tokens=parse_int(raw.get("reasoning_output_tokens")),
        total_tokens=parse_int(raw.get("total_tokens")),
    )


def token_usage_to_dict(usage: TokenUsage) -> dict[str, int]:
    return {key: getattr(usage, key) for key in TOKEN_USAGE_KEYS}


def add_token_usage(left: TokenUsage, right: TokenUsage) -> TokenUsage:
    return TokenUsage(
        input_tokens=left.input_tokens + right.input_tokens,
        cached_input_tokens=left.cached_input_tokens + right.cached_input_tokens,
        output_tokens=left.output_tokens + right.output_tokens,
        reasoning_output_tokens=left.reasoning_output_tokens + right.reasoning_output_tokens,
        total_tokens=left.total_tokens + right.total_tokens,
    )


def normalize_model_key(model: str | None) -> str | None:
    if not model:
        return None
    normalized = model.strip().lower().replace("_", "-").replace(" ", "-")
    if normalized in {"gpt-5.5-pro", "gpt-5.5pro"}:
        return "gpt-5.5-pro"
    return normalized


def pricing_for_model(model: str | None) -> ModelPricing | None:
    key = normalize_model_key(model)
    if key is None:
        return None
    return API_MODEL_PRICING.get(key)


def extract_model_from_text(text: str | None) -> str | None:
    if not text:
        return None
    match = MODEL_ASSIGNMENT_RE.search(text)
    if not match:
        return None
    return (match.group(1) or match.group(2) or "").strip() or None


def detect_model_from_logs(path: Path, row_limit: int = MODEL_LOG_ROWS_TO_SCAN) -> str | None:
    if not path.exists():
        return None
    connection = None
    try:
        uri = path.resolve().as_uri() + "?mode=ro"
        connection = sqlite3.connect(uri, uri=True, timeout=0.2)
        rows = connection.execute(
            "SELECT feedback_log_body FROM logs ORDER BY id DESC LIMIT ?",
            (row_limit,),
        )
        for (body,) in rows:
            if not isinstance(body, str) or not any(marker in body for marker in MODEL_LOG_MARKERS):
                continue
            model = extract_model_from_text(body)
            if model:
                return model
    except (OSError, sqlite3.Error, ValueError):
        return None
    finally:
        if connection is not None:
            connection.close()
    return None


def detect_model_from_config(path: Path) -> str | None:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError:
        return None
    match = CONFIG_MODEL_RE.search(text)
    if not match:
        return None
    return match.group(1).strip() or None


def detect_latest_model(codex_home: Path | None = None) -> DetectedModel:
    home = codex_home or resolve_codex_home()
    log_model = detect_model_from_logs(home / "logs_2.sqlite")
    if log_model:
        return DetectedModel(model=log_model, source="logs_2.sqlite")

    config_model = detect_model_from_config(home / "config.toml")
    if config_model:
        return DetectedModel(model=config_model, source="config.toml")

    return DetectedModel(model=None, source="unknown")


def estimate_api_cost(
    usage: TokenUsage,
    detected_model: DetectedModel,
    reset_model: str | None = None,
) -> ApiCostEstimate:
    cached_input_tokens = min(usage.cached_input_tokens, usage.input_tokens)
    uncached_input_tokens = max(0, usage.input_tokens - cached_input_tokens)
    output_tokens = usage.output_tokens
    pricing = pricing_for_model(detected_model.model)

    warning = None
    if (
        reset_model
        and detected_model.model
        and normalize_model_key(reset_model) != normalize_model_key(detected_model.model)
    ):
        warning = "Model changed since reset; reset counter for a cleaner estimate"

    if pricing is None:
        return ApiCostEstimate(
            model=detected_model.model,
            model_source=detected_model.source,
            pricing=None,
            uncached_input_tokens=uncached_input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            input_cost=None,
            cached_input_cost=None,
            output_cost=None,
            total_cost=None,
            warning=warning,
        )

    cached_rate = pricing.cached_input_per_million
    if cached_rate is None:
        cached_rate = pricing.input_per_million

    input_cost = uncached_input_tokens * pricing.input_per_million / 1_000_000
    cached_input_cost = cached_input_tokens * cached_rate / 1_000_000
    output_cost = output_tokens * pricing.output_per_million / 1_000_000
    return ApiCostEstimate(
        model=detected_model.model,
        model_source=detected_model.source,
        pricing=pricing,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        input_cost=input_cost,
        cached_input_cost=cached_input_cost,
        output_cost=output_cost,
        total_cost=input_cost + cached_input_cost + output_cost,
        warning=warning,
    )


def format_api_cost(value: float | None) -> str:
    if value is None:
        return "--"
    if value == 0:
        return "$0.00"
    if abs(value) < 0.01:
        return f"${value:.4f}".rstrip("0").rstrip(".")
    return f"${value:,.2f}"


def format_api_rate(value: float | None) -> str:
    if value is None:
        return "same as input"
    return format_api_cost(value)


def format_api_cost_estimate(estimate: ApiCostEstimate) -> str:
    if estimate.total_cost is None:
        return "API est. --"
    return f"{format_api_cost(estimate.total_cost)} API est."


def model_pricing_to_dict(pricing: ModelPricing | None) -> dict[str, Any] | None:
    if pricing is None:
        return None
    return {
        "display_name": pricing.display_name,
        "input_per_million": pricing.input_per_million,
        "cached_input_per_million": pricing.cached_input_per_million,
        "output_per_million": pricing.output_per_million,
        "source_url": pricing.source_url,
        "assumption": API_PRICING_ASSUMPTION,
    }


def api_cost_estimate_to_dict(estimate: ApiCostEstimate | None) -> dict[str, Any] | None:
    if estimate is None:
        return None
    return {
        "model": estimate.model,
        "model_source": estimate.model_source,
        "pricing": model_pricing_to_dict(estimate.pricing),
        "uncached_input_tokens": estimate.uncached_input_tokens,
        "cached_input_tokens": estimate.cached_input_tokens,
        "output_tokens": estimate.output_tokens,
        "input_cost": estimate.input_cost,
        "cached_input_cost": estimate.cached_input_cost,
        "output_cost": estimate.output_cost,
        "total_cost": estimate.total_cost,
        "display": format_api_cost_estimate(estimate),
        "warning": estimate.warning,
    }


def parse_token_event_line(line: str, source_path: str | None = None) -> TokenEvent | None:
    if '"token_count"' not in line:
        return None

    try:
        event = json.loads(line)
    except json.JSONDecodeError:
        return None

    if event.get("type") != "event_msg":
        return None

    payload = event.get("payload")
    if not isinstance(payload, dict) or payload.get("type") != "token_count":
        return None

    info = payload.get("info")
    if not isinstance(info, dict):
        info = {}

    usage = parse_token_usage(info.get("last_token_usage"))
    total_usage = parse_token_usage(info.get("total_token_usage"))
    timestamp = str(event.get("timestamp") or "")
    fingerprint = "|".join(
        [
            source_path or "",
            timestamp,
            str(usage.total_tokens if usage else ""),
            str(total_usage.total_tokens if total_usage else ""),
        ]
    )
    return TokenEvent(
        timestamp=timestamp,
        fingerprint=fingerprint,
        usage=usage,
        source_path=source_path,
    )


def timestamp_sort_key(snapshot: RateSnapshot) -> tuple[float, str, str]:
    parsed_timestamp = timestamp_to_epoch(snapshot.timestamp)
    if parsed_timestamp is None:
        parsed_timestamp = snapshot.source_observed_at or 0.0
    source_priority = "1" if snapshot.source_kind == "logs_2.sqlite" else "0"
    return (parsed_timestamp, source_priority, snapshot.source_path or "")


def read_tail_text(path: Path, max_bytes: int = TAIL_BYTES) -> str:
    try:
        size = path.stat().st_size
        with path.open("rb") as handle:
            if size > max_bytes:
                handle.seek(-max_bytes, os.SEEK_END)
                handle.readline()
            data = handle.read()
    except OSError:
        return ""
    return data.decode("utf-8", errors="replace")


def parse_rate_snapshots_from_text(text: str, source_path: str | None = None) -> list[RateSnapshot]:
    snapshots: list[RateSnapshot] = []
    for line in text.splitlines():
        snapshot = parse_rate_line(line, source_path)
        if snapshot is not None:
            snapshots.append(snapshot)
    return snapshots


def timestamp_from_epoch(value: float | int | None) -> str:
    if value is None:
        return ""
    try:
        return datetime.fromtimestamp(float(value), timezone.utc).isoformat()
    except (OSError, ValueError, TypeError):
        return ""


def parse_sqlite_rate_limit_log_body(
    body: str | None,
    observed_at: float | None = None,
    source_path: str | None = None,
) -> RateSnapshot | None:
    if not isinstance(body, str):
        return None
    if SQLITE_RATE_LOG_MARKER not in body or SQLITE_RATE_EVENT_TYPE not in body:
        return None

    marker_index = body.find(SQLITE_RATE_LOG_MARKER)
    if marker_index < 0:
        return None

    json_text = body[marker_index + len(SQLITE_RATE_LOG_MARKER) :].lstrip()
    try:
        payload, _end_index = json.JSONDecoder().raw_decode(json_text)
    except json.JSONDecodeError:
        return None

    if not isinstance(payload, dict) or payload.get("type") != SQLITE_RATE_EVENT_TYPE:
        return None

    rate_limits = payload.get("rate_limits")
    if not isinstance(rate_limits, dict):
        return None

    primary = parse_rate_window(rate_limits.get("primary"), "5h")
    secondary = parse_rate_window(rate_limits.get("secondary"), "7d")
    if primary is None and secondary is None:
        return None

    reached_type = rate_limits.get("rate_limit_reached_type")
    if reached_type is None and bool(rate_limits.get("limit_reached")):
        reached_type = "codex"

    return RateSnapshot(
        timestamp=timestamp_from_epoch(observed_at),
        primary=primary,
        secondary=secondary,
        plan_type=payload.get("plan_type"),
        rate_limit_reached_type=reached_type,
        source_path=source_path,
        source_kind="logs_2.sqlite",
        source_observed_at=observed_at,
    )


def parse_token_events_from_text(text: str, source_path: str | None = None) -> list[TokenEvent]:
    events: list[TokenEvent] = []
    for line in text.splitlines():
        event = parse_token_event_line(line, source_path)
        if event is not None:
            events.append(event)
    return events


def newest_snapshot_from_paths(paths: list[Path]) -> RateSnapshot | None:
    snapshots: list[RateSnapshot] = []
    for path in paths:
        snapshots.extend(parse_rate_snapshots_from_text(read_tail_text(path), str(path)))
    if not snapshots:
        return None
    return max(snapshots, key=timestamp_sort_key)


def resolve_codex_home() -> Path:
    codex_home = os.environ.get("CODEX_HOME")
    if codex_home:
        return Path(codex_home).expanduser()
    return Path.home() / ".codex"


class SqliteRateLimitReader:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.last_error: str | None = None

    def latest_snapshot(self, row_limit: int = SQLITE_RATE_ROWS_TO_SCAN) -> RateSnapshot | None:
        self.last_error = None
        if not self.path.exists():
            return None

        connection = None
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=0.2)
            rows = connection.execute(
                """
                SELECT id, ts, target, feedback_log_body
                FROM logs
                WHERE feedback_log_body LIKE ?
                  AND feedback_log_body LIKE ?
                ORDER BY id DESC
                LIMIT ?
                """,
                (f"%{SQLITE_RATE_LOG_MARKER}%", f"%{SQLITE_RATE_EVENT_TYPE}%", row_limit),
            )
            snapshots: list[RateSnapshot] = []
            for row_id, observed_at, target, body in rows:
                if target != "codex_api::endpoint::responses_websocket":
                    continue
                try:
                    observed_float = float(observed_at)
                except (TypeError, ValueError):
                    observed_float = None
                source_path = f"{self.path}:{row_id}"
                snapshot = parse_sqlite_rate_limit_log_body(body, observed_float, source_path)
                if snapshot is not None:
                    snapshots.append(snapshot)
            if not snapshots:
                return None
            return max(snapshots, key=timestamp_sort_key)
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.last_error = str(exc)
            return None
        finally:
            if connection is not None:
                connection.close()


class RateLogReader:
    def __init__(self, codex_home: Path | None = None) -> None:
        self.codex_home = codex_home or resolve_codex_home()
        self.sqlite_reader = SqliteRateLimitReader(self.codex_home / "logs_2.sqlite")
        self._session_files: list[tuple[Path, os.stat_result]] = []
        self._offsets: dict[Path, int] = {}
        self._partials: dict[Path, str] = {}
        self._last_snapshot: RateSnapshot | None = None
        self.last_error: str | None = None

    def read_updates(self, force_rescan: bool = False) -> LogReadBatch:
        try:
            self.last_error = None
            self._session_files = self._find_session_files()
            active_files = self._session_files[:MAX_SESSION_FILES_TO_SCAN]
            self._prune_tracking({path for path, _stat_result in active_files})

            snapshots: list[RateSnapshot] = []
            token_events: list[TokenEvent] = []
            for path, stat_result in active_files:
                text = self._read_file_updates(path, stat_result, force_tail=force_rescan)
                snapshots.extend(parse_rate_snapshots_from_text(text, str(path)))
                token_events.extend(parse_token_events_from_text(text, str(path)))

            sqlite_snapshot = self.sqlite_reader.latest_snapshot()
            if sqlite_snapshot is not None:
                snapshots.append(sqlite_snapshot)

            if snapshots:
                newest = max(snapshots, key=timestamp_sort_key)
                if self._last_snapshot is None or timestamp_sort_key(newest) >= timestamp_sort_key(self._last_snapshot):
                    self._last_snapshot = newest
            return LogReadBatch(snapshot=self._last_snapshot, token_events=token_events)
        except Exception as exc:  # Defensive: never let a log race crash the overlay.
            self.last_error = str(exc)
            return LogReadBatch(snapshot=self._last_snapshot, token_events=[])

    def latest_snapshot(self, force_rescan: bool = False) -> RateSnapshot | None:
        return self.read_updates(force_rescan=force_rescan).snapshot

    def _find_session_files(self) -> list[tuple[Path, os.stat_result]]:
        sessions_dir = self.codex_home / "sessions"
        if not sessions_dir.exists():
            self.last_error = f"Missing sessions folder: {sessions_dir}"
            return []

        files: list[tuple[Path, os.stat_result]] = []
        for path in sessions_dir.rglob("*.jsonl"):
            try:
                stat_result = path.stat()
            except OSError:
                continue
            files.append((path, stat_result))
        return sorted(files, key=lambda item: item[1].st_mtime, reverse=True)

    def _read_file_updates(self, path: Path, stat_result: os.stat_result, force_tail: bool = False) -> str:
        size = stat_result.st_size
        previous_offset = self._offsets.get(path)
        if force_tail or previous_offset is None or size < previous_offset:
            self._offsets[path] = size
            self._partials[path] = ""
            return read_tail_text(path)

        if size == previous_offset:
            return ""

        try:
            with path.open("rb") as handle:
                handle.seek(previous_offset)
                data = handle.read(size - previous_offset)
        except OSError:
            return ""

        self._offsets[path] = size
        return self._complete_appended_text(path, data.decode("utf-8", errors="replace"))

    def _complete_appended_text(self, path: Path, text: str) -> str:
        combined = self._partials.get(path, "") + text
        if not combined:
            return ""
        if combined.endswith(("\n", "\r")):
            self._partials[path] = ""
            return combined

        lines = combined.splitlines(keepends=True)
        if not lines:
            self._partials[path] = combined
            return ""
        self._partials[path] = lines[-1]
        return "".join(lines[:-1])

    def _prune_tracking(self, active_paths: set[Path]) -> None:
        for tracked in list(self._offsets):
            if tracked not in active_paths:
                self._offsets.pop(tracked, None)
                self._partials.pop(tracked, None)


def normalize_display_windows(value: Any) -> list[str]:
    if isinstance(value, str):
        requested = [value]
    elif isinstance(value, (list, tuple, set)):
        requested = list(value)
    else:
        requested = list(DEFAULT_DISPLAY_WINDOWS)

    normalized = [item for item in requested if item in VALID_DISPLAY_WINDOWS]
    if not normalized:
        return list(DEFAULT_DISPLAY_WINDOWS)
    return list(dict.fromkeys(normalized))


def normalize_layout_mode(value: Any) -> str:
    return value if value in VALID_LAYOUT_MODES else DEFAULT_LAYOUT_MODE


def active_display_widget_keys(settings: dict[str, Any]) -> list[str]:
    keys = list(normalize_display_windows(settings.get("display_windows")))
    if settings.get("show_token_counter", False):
        keys.append("token_counter")
    if settings.get("show_api_cost_estimate", False):
        keys.append("api_cost")
    return keys


def layout_position(index: int, layout_mode: str) -> tuple[int, int]:
    mode = normalize_layout_mode(layout_mode)
    if mode == "vertical":
        return (index, 0)
    if mode == "grid_2x2":
        return (index // 2, index % 2)
    return (0, index)


def layout_positions(count: int, layout_mode: str) -> list[tuple[int, int]]:
    return [layout_position(index, layout_mode) for index in range(max(0, count))]


def checked_menu_label(label: str, enabled: bool) -> str:
    return f"[{'x' if enabled else ' '}] {label}"


def selected_menu_label(label: str, selected: bool) -> str:
    return f"({'*' if selected else ' '}) {label}"


def normalize_visibility_mode(value: Any) -> str:
    return value if value in VALID_VISIBILITY_MODES else "process"


def clamp_int(value: int, minimum: int, maximum: int) -> int:
    if minimum > maximum:
        return minimum
    return max(minimum, min(maximum, value))


def normalize_window_size(window_size: tuple[int, int] | None = None) -> tuple[int, int]:
    if window_size is None:
        return (DEFAULT_OVERLAY_WIDTH, DEFAULT_OVERLAY_HEIGHT)
    width, height = window_size
    return (max(1, int(width)), max(1, int(height)))


def default_overlay_position(
    bounds: tuple[int, int, int, int],
    window_size: tuple[int, int] | None = None,
) -> list[int]:
    left, top, width, _height = bounds
    window_width, _window_height = normalize_window_size(window_size)
    x = left + max(0, width - window_width - 12)
    y = top + 72
    return clamp_overlay_position([x, y], bounds, window_size)


def clamp_overlay_position(
    position: list[int] | tuple[int, int],
    bounds: tuple[int, int, int, int],
    window_size: tuple[int, int] | None = None,
) -> list[int]:
    left, top, width, height = bounds
    window_width, window_height = normalize_window_size(window_size)
    if width <= 0 or height <= 0:
        return [0, 0]

    if window_width <= width:
        min_x = left
        max_x = left + width - window_width
    else:
        min_x = left - window_width + MIN_VISIBLE_PIXELS
        max_x = left + width - MIN_VISIBLE_PIXELS

    if window_height <= height:
        min_y = top
        max_y = top + height - window_height
    else:
        min_y = top - window_height + MIN_VISIBLE_PIXELS
        max_y = top + height - MIN_VISIBLE_PIXELS

    return [
        clamp_int(int(position[0]), min_x, max_x),
        clamp_int(int(position[1]), min_y, max_y),
    ]


def normalize_overlay_position(
    value: Any,
    bounds: tuple[int, int, int, int],
    window_size: tuple[int, int] | None = None,
) -> list[int]:
    if (
        isinstance(value, list)
        and len(value) == 2
        and all(isinstance(item, int) for item in value)
    ):
        return clamp_overlay_position(value, bounds, window_size)
    return default_overlay_position(bounds, window_size)


def windows_virtual_screen_bounds() -> tuple[int, int, int, int] | None:
    if platform.system() != "Windows":
        return None
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        return (
            int(user32.GetSystemMetrics(76)),
            int(user32.GetSystemMetrics(77)),
            int(user32.GetSystemMetrics(78)),
            int(user32.GetSystemMetrics(79)),
        )
    except (OSError, AttributeError):
        return None


def settings_path() -> Path:
    try:
        base = Path(__file__).resolve().parent
    except NameError:
        base = Path.cwd()
    return base / SETTINGS_FILE_NAME


def load_settings(path: Path | None = None) -> dict[str, Any]:
    target = path or settings_path()
    defaults = {
        "visibility_mode": "process",
        "display_windows": list(DEFAULT_DISPLAY_WINDOWS),
        "layout_mode": DEFAULT_LAYOUT_MODE,
        "position": None,
        "opacity": DEFAULT_OPACITY,
        "show_resets": False,
        "show_token_counter": False,
        "show_api_cost_estimate": False,
    }

    try:
        with target.open("r", encoding="utf-8") as handle:
            loaded = json.load(handle)
    except (OSError, json.JSONDecodeError):
        loaded = {}

    if not isinstance(loaded, dict):
        loaded = {}

    settings = defaults | loaded
    settings["visibility_mode"] = normalize_visibility_mode(settings.get("visibility_mode"))
    settings["display_windows"] = normalize_display_windows(settings.get("display_windows"))
    settings["layout_mode"] = normalize_layout_mode(settings.get("layout_mode"))
    try:
        settings["opacity"] = max(0.2, min(1.0, float(settings.get("opacity", DEFAULT_OPACITY))))
    except (TypeError, ValueError):
        settings["opacity"] = DEFAULT_OPACITY
    settings["show_resets"] = bool(settings.get("show_resets", False))
    settings["show_token_counter"] = bool(settings.get("show_token_counter", False))
    settings["show_api_cost_estimate"] = bool(settings.get("show_api_cost_estimate", False))

    position = settings.get("position")
    if not (
        isinstance(position, list)
        and len(position) == 2
        and all(isinstance(item, int) for item in position)
    ):
        settings["position"] = None

    return settings


def save_settings(settings: dict[str, Any], path: Path | None = None) -> None:
    target = path or settings_path()
    try:
        with target.open("w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, sort_keys=True)
            handle.write("\n")
    except OSError:
        pass


def percent_color(value: int | None) -> str:
    if value is None:
        return COLOR_MUTED
    if value < 20:
        return COLOR_RED
    if value < 50:
        return COLOR_AMBER
    return COLOR_GREEN


def format_reset_time(value: int | None) -> str:
    if value is None:
        return "unknown reset"
    try:
        return datetime.fromtimestamp(value).strftime("%b %-d %-I:%M %p")
    except ValueError:
        try:
            return datetime.fromtimestamp(value).strftime("%b %#d %#I:%M %p")
        except (OSError, ValueError):
            return "unknown reset"


def format_reset_countdown(value: int | None, now: float | None = None) -> str:
    if value is None:
        return "--"
    current = time.time() if now is None else now
    remaining_seconds = int(math.ceil(value - current))
    if remaining_seconds < -RESET_PENDING_GRACE_SECONDS:
        return "pending"
    if remaining_seconds <= 0:
        return "now"

    minutes = max(1, math.ceil(remaining_seconds / 60))
    if minutes < 60:
        return f"{minutes}m"

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        if minutes:
            return f"{hours}h {minutes}m"
        return f"{hours}h"

    days, hours = divmod(hours, 24)
    if hours:
        return f"{days}d {hours}h"
    return f"{days}d"


def format_snapshot_time(timestamp: str) -> str:
    if not timestamp:
        return "unknown time"
    normalized = timestamp.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return timestamp
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    local = parsed.astimezone()
    return local.strftime("%I:%M:%S %p").lstrip("0")


def timestamp_to_epoch(timestamp: str) -> float | None:
    if not timestamp:
        return None
    normalized = timestamp.replace("Z", "+00:00")
    try:
        parsed = datetime.fromisoformat(normalized)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def format_elapsed(start_time: float, now: float | None = None) -> str:
    current = time.time() if now is None else now
    elapsed_seconds = max(0, int(current - start_time))
    minutes = elapsed_seconds // 60
    if minutes < 60:
        return f"{minutes}m"

    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        if minutes:
            return f"{hours}h {minutes}m"
        return f"{hours}h"

    days, hours = divmod(hours, 24)
    if hours:
        return f"{days}d {hours}h"
    return f"{days}d"


def format_age_seconds(value: int | None) -> str:
    if value is None:
        return "unknown age"
    if value < 60:
        return f"{value}s old"
    minutes = value // 60
    if minutes < 60:
        return f"{minutes}m old"
    hours, minutes = divmod(minutes, 60)
    if hours < 24:
        if minutes:
            return f"{hours}h {minutes}m old"
        return f"{hours}h old"
    days, hours = divmod(hours, 24)
    if hours:
        return f"{days}d {hours}h old"
    return f"{days}d old"


def format_token_count(value: int) -> str:
    if value < 1_000:
        return str(value)
    if value < 10_000:
        return f"{value / 1_000:.1f}k"
    if value < 1_000_000:
        return f"{round(value / 1_000)}k"
    if value < 10_000_000:
        return f"{value / 1_000_000:.1f}M"
    return f"{round(value / 1_000_000)}M"


def format_token_counter(total_tokens: int, reset_at: float, now: float | None = None) -> str:
    return f"{format_token_count(total_tokens)} tokens / {format_elapsed(reset_at, now)}"


class TokenCounter:
    def __init__(self, reset_at: float | None = None) -> None:
        self.reset_at = time.time() if reset_at is None else reset_at
        self.totals = TokenUsage()
        self.seen_events: set[str] = set()
        self.last_update_at: float | None = None

    def add_events(self, events: list[TokenEvent], now: float | None = None) -> None:
        current = time.time() if now is None else now
        for event in events:
            if event.usage is None or event.fingerprint in self.seen_events:
                continue

            event_epoch = timestamp_to_epoch(event.timestamp)
            if event_epoch is None or event_epoch < self.reset_at:
                continue

            self.totals = add_token_usage(self.totals, event.usage)
            self.seen_events.add(event.fingerprint)
            self.last_update_at = current

    def reset(self, now: float | None = None) -> None:
        self.reset_at = time.time() if now is None else now
        self.totals = TokenUsage()
        self.seen_events.clear()
        self.last_update_at = None

    def display_text(self, now: float | None = None) -> str:
        return format_token_counter(self.totals.total_tokens, self.reset_at, now)

    def state_dict(self) -> dict[str, Any]:
        return {
            "reset_at": self.reset_at,
            "last_update_at": self.last_update_at,
            "totals": token_usage_to_dict(self.totals),
            "seen_event_count": len(self.seen_events),
        }


def rate_window_to_dict(rate_window: RateWindow | None) -> dict[str, Any] | None:
    if rate_window is None:
        return None
    return {
        "label": rate_window.label,
        "window_minutes": rate_window.window_minutes,
        "used_percent": rate_window.used_percent,
        "remaining_percent": rate_window.remaining_percent,
        "resets_at": rate_window.resets_at,
    }


def rate_snapshot_to_dict(snapshot: RateSnapshot | None) -> dict[str, Any] | None:
    if snapshot is None:
        return None
    return {
        "timestamp": snapshot.timestamp,
        "primary": rate_window_to_dict(snapshot.primary),
        "secondary": rate_window_to_dict(snapshot.secondary),
        "plan_type": snapshot.plan_type,
        "rate_limit_reached_type": snapshot.rate_limit_reached_type,
        "source_path": snapshot.source_path,
        "source_kind": snapshot.source_kind,
        "source_observed_at": snapshot.source_observed_at,
    }


def snapshot_source_age_seconds(snapshot: RateSnapshot | None, now: float | None = None) -> int | None:
    if snapshot is None:
        return None
    source_time = snapshot.source_observed_at
    if source_time is None:
        source_time = timestamp_to_epoch(snapshot.timestamp)
    if source_time is None:
        return None
    current = time.time() if now is None else now
    return max(0, int(current - source_time))


def runtime_state_path() -> Path:
    return Path(tempfile.gettempdir()) / RUNTIME_STATE_FILE_NAME


def instance_lock_path() -> Path:
    return Path(tempfile.gettempdir()) / INSTANCE_LOCK_FILE_NAME


def process_exists(pid: int) -> bool:
    if pid <= 0:
        return False
    if pid == os.getpid():
        return True
    if platform.system() == "Windows":
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        kernel32.CloseHandle(handle)
        return True

    try:
        os.kill(pid, 0)
    except OSError:
        return False
    return True


class SingleInstanceLock:
    def __init__(
        self,
        path: Path | None = None,
        pid: int | None = None,
        state_path: Path | None = None,
    ) -> None:
        self.path = path or instance_lock_path()
        self.pid = os.getpid() if pid is None else pid
        self.state_path = state_path or runtime_state_path()
        self.acquired = False

    def acquire(self) -> bool:
        for _attempt in range(2):
            try:
                self.path.parent.mkdir(parents=True, exist_ok=True)
                fd = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if self._existing_process_is_alive():
                    return False
                self._delete_stale()
                continue
            except OSError:
                return True

            with os.fdopen(fd, "w", encoding="utf-8") as handle:
                json.dump({"app": APP_NAME, "pid": self.pid, "created_at": time.time()}, handle)
                handle.write("\n")
            self.acquired = True
            return True
        return False

    def release(self) -> None:
        if not self.acquired:
            return
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            state = {}
        if parse_int(state.get("pid")) in {0, self.pid}:
            self._delete_stale()
        self.acquired = False

    def _existing_process_is_alive(self) -> bool:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return False
        pid = parse_int(state.get("pid"))
        if not process_exists(pid):
            return False
        if platform.system() != "Windows":
            return True

        now = time.time()
        if self._runtime_state_is_fresh_for_pid(pid, now):
            return True

        try:
            created_at = float(state.get("created_at", 0))
        except (TypeError, ValueError):
            created_at = 0
        return created_at > 0 and now - created_at <= INSTANCE_LOCK_STARTUP_GRACE_SECONDS

    def _runtime_state_is_fresh_for_pid(self, pid: int, now: float) -> bool:
        try:
            with self.state_path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return False

        if parse_int(state.get("pid")) != pid:
            return False
        try:
            last_update_at = float(state.get("last_update_at", 0))
        except (TypeError, ValueError):
            return False
        return last_update_at > 0 and now - last_update_at <= INSTANCE_LOCK_HEARTBEAT_STALE_SECONDS

    def _delete_stale(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class RuntimeStateStore:
    def __init__(self, path: Path | None = None, pid: int | None = None) -> None:
        self.path = path or runtime_state_path()
        self.pid = os.getpid() if pid is None else pid

    def cleanup_stale(self) -> None:
        try:
            with self.path.open("r", encoding="utf-8") as handle:
                state = json.load(handle)
        except (OSError, json.JSONDecodeError):
            return

        old_pid = parse_int(state.get("pid"))
        if old_pid and old_pid != self.pid and not process_exists(old_pid):
            self.delete()

    def write(
        self,
        snapshot: RateSnapshot | None,
        counter: TokenCounter,
        api_cost_estimate: ApiCostEstimate | None = None,
    ) -> None:
        now = time.time()
        state = {
            "app": APP_NAME,
            "pid": self.pid,
            "last_update_at": now,
            "last_source_timestamp": snapshot.timestamp if snapshot else None,
            "rate_source": snapshot.source_kind if snapshot else None,
            "source_event_timestamp": snapshot.timestamp if snapshot else None,
            "source_observed_at": snapshot.source_observed_at if snapshot else None,
            "source_age_seconds": snapshot_source_age_seconds(snapshot, now),
            "last_rate_snapshot": rate_snapshot_to_dict(snapshot),
            "token_counter": counter.state_dict(),
            "api_cost_estimate": api_cost_estimate_to_dict(api_cost_estimate),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            temp_path.replace(self.path)
        except OSError:
            pass

    def delete(self) -> None:
        try:
            self.path.unlink()
        except FileNotFoundError:
            pass
        except OSError:
            pass


class ProcessBackend:
    def is_process_running(self) -> bool:
        return bool(self.codex_pids())

    def codex_pids(self) -> set[int]:
        if platform.system() == "Windows":
            return windows_codex_pids()
        return generic_codex_pids()

    def is_foreground(self) -> bool:
        if platform.system() != "Windows":
            return False
        return windows_foreground_is_codex()

    def has_visible_window(self) -> bool:
        if platform.system() != "Windows":
            return False
        return windows_has_visible_codex_window()

    def is_supported(self, mode: str) -> bool:
        if mode in {"always", "process"}:
            return True
        return platform.system() == "Windows"

    def should_show(self, mode: str) -> bool:
        if mode == "always":
            return True
        if mode == "process":
            return self.is_process_running()
        if mode == "foreground":
            return self.is_foreground()
        if mode == "visible_window":
            return self.has_visible_window()
        return True


def generic_codex_pids() -> set[int]:
    if platform.system() == "Linux":
        return linux_codex_pids()
    return pgrep_codex_pids()


def linux_codex_pids() -> set[int]:
    proc = Path("/proc")
    if not proc.exists():
        return set()

    pids: set[int] = set()
    for path in proc.iterdir():
        if not path.name.isdigit():
            continue
        try:
            name = (path / "comm").read_text(encoding="utf-8", errors="ignore").strip().lower()
        except OSError:
            name = ""
        if name in GENERIC_CODEX_PROCESS_NAMES:
            pids.add(int(path.name))
            continue
        try:
            exe_name = (path / "exe").resolve().name.lower()
        except OSError:
            exe_name = ""
        if exe_name in GENERIC_CODEX_PROCESS_NAMES:
            pids.add(int(path.name))
    return pids


def pgrep_codex_pids() -> set[int]:
    pids: set[int] = set()
    for name in ("Codex", "codex"):
        try:
            result = subprocess.run(
                ["pgrep", "-x", name],
                check=False,
                capture_output=True,
                text=True,
                timeout=1,
            )
        except (OSError, subprocess.SubprocessError):
            continue
        for line in result.stdout.splitlines():
            try:
                pids.add(int(line.strip()))
            except ValueError:
                continue
    return pids


class PROCESSENTRY32W(ctypes.Structure):
    _fields_ = [
        ("dwSize", ctypes.wintypes.DWORD),
        ("cntUsage", ctypes.wintypes.DWORD),
        ("th32ProcessID", ctypes.wintypes.DWORD),
        ("th32DefaultHeapID", ctypes.POINTER(ctypes.wintypes.ULONG)),
        ("th32ModuleID", ctypes.wintypes.DWORD),
        ("cntThreads", ctypes.wintypes.DWORD),
        ("th32ParentProcessID", ctypes.wintypes.DWORD),
        ("pcPriClassBase", ctypes.wintypes.LONG),
        ("dwFlags", ctypes.wintypes.DWORD),
        ("szExeFile", ctypes.wintypes.WCHAR * 260),
    ]


def windows_codex_pids() -> set[int]:
    if platform.system() != "Windows":
        return set()

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    snapshot = kernel32.CreateToolhelp32Snapshot(0x00000002, 0)
    if snapshot == ctypes.wintypes.HANDLE(-1).value:
        return set()

    pids: set[int] = set()
    try:
        entry = PROCESSENTRY32W()
        entry.dwSize = ctypes.sizeof(PROCESSENTRY32W)
        has_entry = kernel32.Process32FirstW(snapshot, ctypes.byref(entry))
        while has_entry:
            if entry.szExeFile.lower() in CODEX_PROCESS_NAMES:
                pids.add(int(entry.th32ProcessID))
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def windows_process_name(pid: int) -> str:
    if platform.system() != "Windows":
        return ""

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    PROCESS_VM_READ = 0x0010

    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION | PROCESS_VM_READ, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(260)
        if psapi.GetModuleBaseNameW(handle, None, buffer, len(buffer)):
            return buffer.value.lower()
    finally:
        kernel32.CloseHandle(handle)
    return ""


def windows_foreground_is_codex() -> bool:
    if platform.system() != "Windows":
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return windows_process_name(int(pid.value)) in CODEX_PROCESS_NAMES


def windows_has_visible_codex_window() -> bool:
    if platform.system() != "Windows":
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    matches = {"found": False}

    EnumWindowsProc = ctypes.WINFUNCTYPE(ctypes.c_bool, ctypes.wintypes.HWND, ctypes.wintypes.LPARAM)

    def callback(hwnd: int, _lparam: int) -> bool:
        if not user32.IsWindowVisible(hwnd) or user32.IsIconic(hwnd):
            return True

        length = user32.GetWindowTextLengthW(hwnd)
        if length <= 0:
            return True

        pid = ctypes.wintypes.DWORD()
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if windows_process_name(int(pid.value)) in CODEX_PROCESS_NAMES:
            matches["found"] = True
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return matches["found"]


class OverlayApp:
    def __init__(self) -> None:
        self.settings_path = settings_path()
        self.settings = load_settings(self.settings_path)
        self.reader = RateLogReader()
        self.process_backend = ProcessBackend()
        self.token_counter = TokenCounter()
        self.detected_model = detect_latest_model(self.reader.codex_home)
        self.counter_reset_model = self.detected_model.model
        self.last_model_check_at = time.time()
        self.runtime_state = RuntimeStateStore()
        self.runtime_state.cleanup_stale()
        atexit.register(self.runtime_state.delete)
        self.snapshot: RateSnapshot | None = None
        self.drag_offset: tuple[int, int] | None = None
        self.is_dragging = False
        self.needs_render_after_drag = False
        self.force_rescan = True

        self.root = tk.Tk()
        self.root.title(APP_NAME)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        try:
            self.root.attributes("-alpha", self.settings["opacity"])
        except tk.TclError:
            pass

        self.root.configure(bg=COLOR_BORDER)
        self.container = tk.Frame(self.root, bg=COLOR_BG, padx=8, pady=5)
        self.container.pack(padx=1, pady=1)
        self.labels: list[tk.Label] = []

        self.menu = Menu(self.root, tearoff=False)

        self._bind_window_events(self.root)
        self._bind_window_events(self.container)
        self._position_window()
        self.refresh(force=True)

    def _bind_window_events(self, widget: tk.Widget) -> None:
        widget.bind("<ButtonPress-1>", self.start_drag)
        widget.bind("<B1-Motion>", self.drag)
        widget.bind("<ButtonRelease-1>", self.end_drag)
        widget.bind("<Button-3>", self.show_menu)
        widget.bind("<Button-2>", self.show_menu)

    def _position_window(self) -> None:
        original_position = self.settings.get("position")
        position = normalize_overlay_position(
            original_position,
            self.screen_bounds(),
            self.current_window_size(),
        )
        x, y = position
        self.root.geometry(f"+{x}+{y}")
        if original_position is not None and original_position != position:
            self.settings["position"] = position
            self.save_settings()

    def screen_bounds(self) -> tuple[int, int, int, int]:
        bounds = windows_virtual_screen_bounds()
        if bounds is not None:
            return bounds
        return (0, 0, int(self.root.winfo_screenwidth()), int(self.root.winfo_screenheight()))

    def current_window_size(self) -> tuple[int, int]:
        self.root.update_idletasks()
        return (
            max(DEFAULT_OVERLAY_WIDTH, int(self.root.winfo_width())),
            max(DEFAULT_OVERLAY_HEIGHT, int(self.root.winfo_height())),
        )

    def start_drag(self, event: tk.Event) -> None:
        self.is_dragging = True
        self.drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())

    def drag(self, event: tk.Event) -> None:
        if self.drag_offset is None or not self.is_dragging:
            return
        offset_x, offset_y = self.drag_offset
        pointer_x = int(getattr(event, "x_root", self.root.winfo_pointerx()))
        pointer_y = int(getattr(event, "y_root", self.root.winfo_pointery()))
        x, y = clamp_overlay_position(
            [pointer_x - offset_x, pointer_y - offset_y],
            self.screen_bounds(),
            self.current_window_size(),
        )
        self.root.geometry(f"+{x}+{y}")

    def end_drag(self, _event: tk.Event) -> None:
        if not self.is_dragging:
            return
        self.drag_offset = None
        self.is_dragging = False
        self.settings["position"] = clamp_overlay_position(
            [self.root.winfo_x(), self.root.winfo_y()],
            self.screen_bounds(),
            self.current_window_size(),
        )
        self.save_settings()
        if self.needs_render_after_drag:
            self.request_render(force=True)

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            self.runtime_state.delete()

    def refresh(self, force: bool = False, schedule_next: bool = True) -> None:
        self.refresh_detected_model(force=force or self.force_rescan)
        batch = self.reader.read_updates(force_rescan=force or self.force_rescan)
        self.force_rescan = False
        if batch.snapshot is not None:
            self.snapshot = batch.snapshot
        self.token_counter.add_events(batch.token_events)

        self.request_render()
        self.update_visibility()
        self.runtime_state.write(self.snapshot, self.token_counter, self.current_api_cost_estimate())
        if schedule_next:
            self.root.after(POLL_INTERVAL_MS, self.refresh)

    def manual_refresh(self) -> None:
        self.force_rescan = True
        self.refresh(force=True, schedule_next=False)

    def refresh_detected_model(self, force: bool = False) -> None:
        now = time.time()
        if not force and now - self.last_model_check_at < MODEL_DETECT_INTERVAL_SECONDS:
            return
        self.detected_model = detect_latest_model(self.reader.codex_home)
        self.last_model_check_at = now

    def current_api_cost_estimate(self) -> ApiCostEstimate:
        return estimate_api_cost(self.token_counter.totals, self.detected_model, self.counter_reset_model)

    def request_render(self, force: bool = False) -> None:
        if not force and self.is_dragging:
            self.needs_render_after_drag = True
            return
        self.needs_render_after_drag = False
        self.render()

    def render(self) -> None:
        for label in self.labels:
            label.destroy()
        self.labels.clear()

        widgets = self.display_widgets()
        positions = layout_positions(len(widgets), self.settings.get("layout_mode", DEFAULT_LAYOUT_MODE))
        for index, widget in enumerate(widgets):
            label = tk.Label(
                self.container,
                text=widget.text,
                fg=widget.color,
                bg=COLOR_BG,
                font=("Segoe UI", 10, "bold"),
                padx=3,
                pady=1,
            )
            row, column = positions[index]
            label.grid(row=row, column=column, sticky="w", padx=2, pady=1)
            self._bind_window_events(label)
            self.labels.append(label)

    def display_widgets(self) -> list[DisplayWidget]:
        widgets: list[DisplayWidget] = []
        selected = normalize_display_windows(self.settings.get("display_windows"))
        show_resets = bool(self.settings.get("show_resets", False))
        for key in selected:
            rate_window = self.get_window(key)
            if rate_window is None or rate_window.remaining_percent is None:
                text = f"{default_window_label(key)} --"
                if show_resets:
                    text += " reset --"
                widgets.append(DisplayWidget(key, text, COLOR_MUTED))
            else:
                widgets.append(
                    DisplayWidget(
                        key,
                        self.format_window_text(rate_window, show_resets),
                        percent_color(rate_window.remaining_percent),
                    )
                )

        if self.snapshot and self.snapshot.rate_limit_reached_type:
            if widgets:
                first = widgets[0]
                widgets[0] = DisplayWidget(first.key, f"{first.text} LIMIT", COLOR_RED)
            else:
                widgets.append(DisplayWidget("limit", "LIMIT", COLOR_RED))

        if self.settings.get("show_token_counter", False):
            widgets.append(DisplayWidget("token_counter", self.token_counter.display_text(), COLOR_TEXT))

        if self.settings.get("show_api_cost_estimate", False):
            estimate = self.current_api_cost_estimate()
            color = COLOR_MUTED if estimate.total_cost is None else COLOR_TEXT
            widgets.append(DisplayWidget("api_cost", format_api_cost_estimate(estimate), color))

        return widgets or [DisplayWidget("empty", "5h --  7d --", COLOR_MUTED)]

    def format_window_text(self, rate_window: RateWindow, show_resets: bool) -> str:
        text = f"{rate_window.label} {rate_window.remaining_percent}%"
        if show_resets:
            text += f" reset {format_reset_countdown(rate_window.resets_at)}"
        return text

    def get_window(self, key: str) -> RateWindow | None:
        if self.snapshot is None:
            return None
        return self.snapshot.primary if key == "primary" else self.snapshot.secondary

    def update_visibility(self) -> None:
        mode = self.settings.get("visibility_mode", "process")
        should_show = self.process_backend.should_show(mode)
        if should_show:
            self.root.deiconify()
            self.root.attributes("-topmost", True)
        else:
            self.root.withdraw()

    def show_menu(self, event: tk.Event) -> None:
        if self.is_dragging:
            return
        self.rebuild_menu()
        try:
            self.menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.menu.grab_release()

    def rebuild_menu(self) -> None:
        self.menu.delete(0, tk.END)
        current_visibility = normalize_visibility_mode(self.settings.get("visibility_mode"))
        current_windows = set(normalize_display_windows(self.settings.get("display_windows")))
        show_resets = bool(self.settings.get("show_resets", False))
        show_token_counter = bool(self.settings.get("show_token_counter", False))
        show_api_cost_estimate = bool(self.settings.get("show_api_cost_estimate", False))
        current_layout = normalize_layout_mode(self.settings.get("layout_mode"))

        self.menu.add_command(label=self.status_text(), state=tk.DISABLED)
        if self.snapshot and self.snapshot.plan_type:
            self.menu.add_command(label=f"Plan: {self.snapshot.plan_type}", state=tk.DISABLED)
        if self.snapshot:
            self.menu.add_command(label=self.source_status_text(), state=tk.DISABLED)
        self.menu.add_separator()

        self.menu.add_command(label="Visibility", state=tk.DISABLED)
        visibility_items = [
            ("Always", "always"),
            ("When Codex process is running", "process"),
            ("When Codex is foreground", "foreground"),
            ("When Codex window is visible", "visible_window"),
        ]
        for label, mode in visibility_items:
            supported = self.process_backend.is_supported(mode)
            item_label = label if supported else f"{label} (Windows only)"
            self.menu.add_command(
                label=selected_menu_label(item_label, current_visibility == mode),
                command=lambda selected=mode: self.set_visibility_mode(selected),
                state=tk.NORMAL if supported else tk.DISABLED,
            )

        self.menu.add_separator()
        self.menu.add_command(label="Rate Windows", state=tk.DISABLED)
        for key in VALID_DISPLAY_WINDOWS:
            self.menu.add_command(
                label=checked_menu_label(long_window_label(key, self.get_window(key)), key in current_windows),
                command=lambda selected=key: self.toggle_display_window(selected),
            )
        self.menu.add_command(
            label=checked_menu_label("Show Reset Countdown", show_resets),
            command=self.toggle_show_resets,
        )
        self.menu.add_command(
            label=checked_menu_label("Show Token Counter", show_token_counter),
            command=self.toggle_show_token_counter,
        )
        self.menu.add_command(
            label=checked_menu_label("Show API Cost Estimate", show_api_cost_estimate),
            command=self.toggle_show_api_cost_estimate,
        )

        self.menu.add_separator()
        self.menu.add_command(label="Layout", state=tk.DISABLED)
        layout_items = [
            ("Horizontal", "horizontal"),
            ("Vertical", "vertical"),
            ("2x2 Grid", "grid_2x2"),
        ]
        for label, mode in layout_items:
            self.menu.add_command(
                label=selected_menu_label(label, current_layout == mode),
                command=lambda selected=mode: self.set_layout_mode(selected),
            )

        if self.snapshot:
            self.menu.add_separator()
            for key in normalize_display_windows(self.settings["display_windows"]):
                rate_window = self.get_window(key)
                if rate_window:
                    value = (
                        "--"
                        if rate_window.remaining_percent is None
                        else f"{rate_window.remaining_percent}% remaining"
                    )
                    self.menu.add_command(
                        label=f"{rate_window.label}: {value}, resets {format_reset_time(rate_window.resets_at)}",
                        state=tk.DISABLED,
                    )

        self.menu.add_separator()
        self.menu.add_command(label="Token Counter", state=tk.DISABLED)
        self.menu.add_command(label=self.token_counter.display_text(), state=tk.DISABLED)
        self.menu.add_command(
            label=(
                f"Input {format_token_count(self.token_counter.totals.input_tokens)}, "
                f"cached {format_token_count(self.token_counter.totals.cached_input_tokens)}, "
                f"output {format_token_count(self.token_counter.totals.output_tokens)}, "
                f"reasoning {format_token_count(self.token_counter.totals.reasoning_output_tokens)}"
            ),
            state=tk.DISABLED,
        )
        self.menu.add_command(label=f"Reset {format_snapshot_time(datetime.fromtimestamp(self.token_counter.reset_at, timezone.utc).isoformat())}", state=tk.DISABLED)
        self.menu.add_command(label="Reset Token Counter", command=self.reset_token_counter)

        self.add_api_estimate_menu_items()

        self.menu.add_separator()
        self.menu.add_command(label="Refresh", command=self.manual_refresh)
        self.menu.add_command(label="Reset position", command=self.reset_position)
        self.menu.add_command(label="Quit", command=self.quit)

    def add_api_estimate_menu_items(self) -> None:
        estimate = self.current_api_cost_estimate()
        model_name = estimate.model or "unknown"
        self.menu.add_separator()
        self.menu.add_command(label="API Estimate", state=tk.DISABLED)
        self.menu.add_command(label=format_api_cost_estimate(estimate), state=tk.DISABLED)
        self.menu.add_command(
            label=f"Model: {model_name} ({estimate.model_source}); tier: Standard (assumed)",
            state=tk.DISABLED,
        )
        self.menu.add_command(
            label=(
                f"Tokens: input {format_token_count(estimate.uncached_input_tokens)}, "
                f"cached {format_token_count(estimate.cached_input_tokens)}, "
                f"output {format_token_count(estimate.output_tokens)}"
            ),
            state=tk.DISABLED,
        )

        if estimate.pricing is None:
            self.menu.add_command(label=f"No pricing row configured for {model_name}", state=tk.DISABLED)
        else:
            cached_rate = estimate.pricing.cached_input_per_million
            self.menu.add_command(
                label=(
                    f"Rates /1M: input {format_api_rate(estimate.pricing.input_per_million)}, "
                    f"cached {format_api_rate(cached_rate)}, "
                    f"output {format_api_rate(estimate.pricing.output_per_million)}"
                ),
                state=tk.DISABLED,
            )
            self.menu.add_command(
                label=(
                    f"Costs: input {format_api_cost(estimate.input_cost)}, "
                    f"cached {format_api_cost(estimate.cached_input_cost)}, "
                    f"output {format_api_cost(estimate.output_cost)}"
                ),
                state=tk.DISABLED,
            )

        if estimate.warning:
            self.menu.add_command(label=estimate.warning, state=tk.DISABLED)
        self.menu.add_command(label="API-equivalent estimate only; not actual Codex billing", state=tk.DISABLED)

    def status_text(self) -> str:
        if self.reader.last_error:
            return self.reader.last_error
        if self.snapshot is None:
            return "Waiting for Codex rate data"
        return f"Updated {format_snapshot_time(self.snapshot.timestamp)}"

    def source_status_text(self) -> str:
        if self.snapshot is None:
            return "Rate source: unknown"
        age = snapshot_source_age_seconds(self.snapshot)
        return f"Rate source: {self.snapshot.source_kind}, {format_age_seconds(age)}"

    def set_visibility_mode(self, mode: str) -> None:
        if not self.process_backend.is_supported(mode):
            return
        self.settings["visibility_mode"] = normalize_visibility_mode(mode)
        self.save_settings()
        self.update_visibility()

    def set_display_window(self, key: str, enabled: bool) -> None:
        current = set(normalize_display_windows(self.settings.get("display_windows")))
        if enabled:
            current.add(key)
        elif key in current:
            if len(current) == 1:
                return
            current.remove(key)

        ordered = [item for item in VALID_DISPLAY_WINDOWS if item in current]
        self.settings["display_windows"] = ordered
        self.save_settings()
        self.request_render()

    def toggle_display_window(self, key: str) -> None:
        current = set(normalize_display_windows(self.settings.get("display_windows")))
        self.set_display_window(key, key not in current)

    def set_show_resets(self, enabled: bool) -> None:
        self.settings["show_resets"] = bool(enabled)
        self.save_settings()
        self.request_render()

    def toggle_show_resets(self) -> None:
        self.set_show_resets(not bool(self.settings.get("show_resets", False)))

    def set_show_token_counter(self, enabled: bool) -> None:
        self.settings["show_token_counter"] = bool(enabled)
        self.save_settings()
        self.request_render()

    def toggle_show_token_counter(self) -> None:
        self.set_show_token_counter(not bool(self.settings.get("show_token_counter", False)))

    def set_show_api_cost_estimate(self, enabled: bool) -> None:
        self.settings["show_api_cost_estimate"] = bool(enabled)
        self.save_settings()
        self.request_render()

    def toggle_show_api_cost_estimate(self) -> None:
        self.set_show_api_cost_estimate(not bool(self.settings.get("show_api_cost_estimate", False)))

    def set_layout_mode(self, mode: str) -> None:
        self.settings["layout_mode"] = normalize_layout_mode(mode)
        self.save_settings()
        self.request_render()

    def reset_token_counter(self) -> None:
        self.refresh_detected_model(force=True)
        self.token_counter.reset()
        self.counter_reset_model = self.detected_model.model
        self.runtime_state.write(self.snapshot, self.token_counter, self.current_api_cost_estimate())
        self.request_render()

    def reset_position(self) -> None:
        self.settings["position"] = None
        self.save_settings()
        self._position_window()
        self.request_render()

    def save_settings(self) -> None:
        save_settings(self.settings, self.settings_path)

    def quit(self) -> None:
        self.runtime_state.delete()
        self.root.destroy()


def print_status() -> int:
    snapshot = RateLogReader().latest_snapshot(force_rescan=True)
    if snapshot is None:
        print("No Codex rate data found.")
        return 1
    parts = []
    for key in VALID_DISPLAY_WINDOWS:
        rate_window = snapshot.primary if key == "primary" else snapshot.secondary
        if rate_window and rate_window.remaining_percent is not None:
            parts.append(f"{rate_window.label} {rate_window.remaining_percent}%")
    if snapshot.rate_limit_reached_type:
        parts.append("LIMIT")
    print("  ".join(parts) or "No usable Codex rate windows found.")
    return 0


def print_version() -> int:
    print(f"{APP_NAME} {APP_VERSION}")
    return 0


def print_help() -> int:
    print(
        f"{APP_NAME} {APP_VERSION}\n"
        "\n"
        "Usage:\n"
        "  python codex_usage_overlay.pyw [--print-status|--version|--help]\n"
        "\n"
        "This local-only overlay reads Codex session JSONL files and logs_2.sqlite.\n"
        "It does not call network APIs, read auth.json, or upload data.\n"
    )
    return 0


def main() -> None:
    if "--version" in sys.argv:
        raise SystemExit(print_version())
    if "--help" in sys.argv or "-h" in sys.argv:
        raise SystemExit(print_help())
    if "--print-status" in sys.argv:
        raise SystemExit(print_status())

    instance_lock = SingleInstanceLock()
    if not instance_lock.acquire():
        return
    atexit.register(instance_lock.release)
    try:
        OverlayApp().run()
    finally:
        instance_lock.release()


if __name__ == "__main__":
    main()
