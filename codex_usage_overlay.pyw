"""Tiny Codex usage overlay.

The app reads local Codex session JSONL files and local Codex telemetry logs to
show usage-related status. It does not call network APIs, read auth files, or
upload data.
"""

from __future__ import annotations

import ctypes
import ctypes.wintypes
import atexit
import hashlib
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
from typing import Any, Callable


APP_NAME = "Codex Usage Overlay"
APP_VERSION = "0.1.10"
SETTINGS_FILE_NAME = "codex_usage_overlay.settings.json"
RUNTIME_STATE_FILE_NAME = "codex-usage-overlay-state.json"
INSTANCE_LOCK_FILE_NAME = "codex-usage-overlay.lock"
DEFAULT_DISPLAY_WINDOWS = ("primary", "secondary")
VALID_DISPLAY_WINDOWS = ("primary", "secondary")
DEFAULT_LAYOUT_MODE = "horizontal"
VALID_LAYOUT_MODES = ("horizontal", "vertical")
VALID_VISIBILITY_MODES = ("always", "process", "foreground", "visible_window")
CODEX_PROCESS_NAMES = {"codex.exe"}
GENERIC_CODEX_PROCESS_NAMES = {"codex", "codex.exe"}
WINDOWS_CHATGPT_PROCESS_NAME = "chatgpt.exe"
WINDOWS_CODEX_PACKAGE_PREFIX = "openai.codex_"
WINDOWS_CODEX_PACKAGE_BUILD_RE = re.compile(
    r"^openai\.codex_(\d+(?:\.\d+){3})(?:_|$)",
    re.IGNORECASE,
)
DEFAULT_OPACITY = 0.9
POLL_INTERVAL_MS = 500
HIDDEN_POLL_INTERVAL_MS = 1_000
HIDDEN_LOG_POLL_INTERVAL_SECONDS = 5
PROCESS_VISIBILITY_POLL_INTERVAL_SECONDS = 1
RUNTIME_STATE_WRITE_INTERVAL_SECONDS = 2
SESSION_FULL_RESCAN_INTERVAL_SECONDS = 30
SQLITE_FALLBACK_PROBE_INTERVAL_SECONDS = 5
DISPLAY_TOPOLOGY_POLL_INTERVAL_SECONDS = 5
MIN_VISIBLE_PIXELS = 24
DEFAULT_OVERLAY_WIDTH = 190
DEFAULT_OVERLAY_HEIGHT = 40
DISPLAY_TOPOLOGY_SAMPLE_SECONDS = 0.25
DISPLAY_TOPOLOGY_DEBOUNCE_SECONDS = 0.5
DISPLAY_TOPOLOGY_RETRY_SECONDS = 1
DISPLAY_TOPOLOGY_VERIFY_MS = 1_000
INSTANCE_LOCK_STARTUP_GRACE_SECONDS = 10
INSTANCE_LOCK_HEARTBEAT_STALE_SECONDS = 5
MAX_SESSION_FILES_TO_SCAN = 10
TAIL_BYTES = 1_048_576
RESET_PENDING_GRACE_SECONDS = 60
MODEL_DETECT_INTERVAL_SECONDS = 10
MODEL_LOG_ROWS_TO_SCAN = 250
SQLITE_RATE_ROWS_TO_SCAN = 200
API_PRICING_ASSUMPTION = "Standard API pricing assumed"
API_PRICING_SOURCE_URL = "https://developers.openai.com/api/docs/pricing"
LONG_CONTEXT_INPUT_THRESHOLD_TOKENS = 272_000
CACHE_WRITE_TELEMETRY_NOTE = (
    "Local Codex events do not report cache-write tokens; cache-write premiums are excluded."
)
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
MENU_BG = "#f8fafc"
MENU_BORDER = "#9ca3af"
MENU_TEXT = "#111827"
MENU_MUTED = "#6b7280"
MENU_HOVER = "#dbeafe"
MENU_DISABLED_BG = "#f8fafc"
MENU_SEPARATOR = "#d1d5db"
MENU_SCREEN_PADDING = 8
MENU_ROW_PADX = 8
MENU_ROW_PADY = 2
MENU_SEPARATOR_PADY = 2
MENU_MIN_WRAP_LENGTH = 180
MENU_MAX_WRAP_LENGTH = 420
MENU_SCROLLBAR_WIDTH = 16
MENU_RELATED_POPUP_GAP = 6
MENU_FOCUS_ARM_DELAY_MS = 150
MENU_FOCUS_LOSS_DEBOUNCE_MS = 100
MENU_FOCUS_LOSS_CONFIRMATIONS = 2
MENU_OUTSIDE_WATCH_INTERVAL_MS = 50
POST_MENU_VISIBILITY_DELAY_MS = 250
POST_DRAG_VISIBILITY_DELAY_MS = 250

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
    cache_write_per_million: float | None = None
    long_context_threshold_tokens: int | None = None
    long_context_input_per_million: float | None = None
    long_context_cached_input_per_million: float | None = None
    long_context_cache_write_per_million: float | None = None
    long_context_output_per_million: float | None = None


@dataclass(frozen=True)
class PricingResolution:
    pricing_model: str
    pricing: ModelPricing
    is_proxy: bool = False


@dataclass(frozen=True)
class DetectedModel:
    model: str | None
    source: str


@dataclass(frozen=True)
class ApiCostEstimate:
    model: str | None
    model_source: str
    pricing: ModelPricing | None
    pricing_model: str | None
    pricing_is_proxy: bool
    uncached_input_tokens: int
    cached_input_tokens: int
    output_tokens: int
    input_cost: float | None
    cached_input_cost: float | None
    output_cost: float | None
    total_cost: float | None
    long_context_request_count: int = 0
    cache_write_cost: float | None = None
    warning: str | None = None


@dataclass(frozen=True)
class MonitorWorkArea:
    device_name: str
    monitor_bounds: tuple[int, int, int, int]
    work_area: tuple[int, int, int, int]
    is_primary: bool = False


@dataclass(frozen=True)
class DisplayWidget:
    key: str
    text: str
    color: str


@dataclass(frozen=True)
class MenuRow:
    kind: str
    label: str = ""
    action: Callable[[], None] | None = None

    @classmethod
    def command(cls, label: str, action: Callable[[], None], enabled: bool = True) -> "MenuRow":
        if enabled:
            return cls("command", label, action)
        return cls("disabled", label)

    @classmethod
    def disabled(cls, label: str) -> "MenuRow":
        return cls("disabled", label)

    @classmethod
    def separator(cls) -> "MenuRow":
        return cls("separator")

    @property
    def clickable(self) -> bool:
        return self.kind == "command" and self.action is not None

    def invoke(self) -> bool:
        if not self.clickable or self.action is None:
            return False
        self.action()
        return True


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


# Current official OpenAI Standard API prices, per 1M text tokens.
# Source: https://developers.openai.com/api/docs/pricing
API_MODEL_PRICING = {
    "gpt-5.6-sol": ModelPricing(
        "gpt-5.6 Sol", 5.00, 0.50, 30.00, API_PRICING_SOURCE_URL,
        cache_write_per_million=6.25,
        long_context_threshold_tokens=LONG_CONTEXT_INPUT_THRESHOLD_TOKENS,
        long_context_input_per_million=10.00,
        long_context_cached_input_per_million=1.00,
        long_context_cache_write_per_million=12.50,
        long_context_output_per_million=45.00,
    ),
    "gpt-5.6-terra": ModelPricing(
        "gpt-5.6 Terra", 2.50, 0.25, 15.00, API_PRICING_SOURCE_URL,
        cache_write_per_million=3.125,
        long_context_threshold_tokens=LONG_CONTEXT_INPUT_THRESHOLD_TOKENS,
        long_context_input_per_million=5.00,
        long_context_cached_input_per_million=0.50,
        long_context_cache_write_per_million=6.25,
        long_context_output_per_million=22.50,
    ),
    "gpt-5.6-luna": ModelPricing(
        "gpt-5.6 Luna", 1.00, 0.10, 6.00, API_PRICING_SOURCE_URL,
        cache_write_per_million=1.25,
        long_context_threshold_tokens=LONG_CONTEXT_INPUT_THRESHOLD_TOKENS,
        long_context_input_per_million=2.00,
        long_context_cached_input_per_million=0.20,
        long_context_cache_write_per_million=2.50,
        long_context_output_per_million=9.00,
    ),
    "gpt-5.5": ModelPricing(
        "gpt-5.5", 5.00, 0.50, 30.00, API_PRICING_SOURCE_URL,
        long_context_threshold_tokens=LONG_CONTEXT_INPUT_THRESHOLD_TOKENS,
        long_context_input_per_million=10.00,
        long_context_cached_input_per_million=1.00,
        long_context_output_per_million=45.00,
    ),
    "gpt-5.5-pro": ModelPricing(
        "gpt-5.5 pro", 30.00, None, 180.00, API_PRICING_SOURCE_URL,
        long_context_threshold_tokens=LONG_CONTEXT_INPUT_THRESHOLD_TOKENS,
        long_context_input_per_million=60.00,
        long_context_output_per_million=270.00,
    ),
    "gpt-5.4": ModelPricing(
        "gpt-5.4", 2.50, 0.25, 15.00, API_PRICING_SOURCE_URL,
        long_context_threshold_tokens=LONG_CONTEXT_INPUT_THRESHOLD_TOKENS,
        long_context_input_per_million=5.00,
        long_context_cached_input_per_million=0.50,
        long_context_output_per_million=22.50,
    ),
    "gpt-5.4-mini": ModelPricing(
        "gpt-5.4 mini", 0.75, 0.075, 4.50, API_PRICING_SOURCE_URL
    ),
}
API_PRICING_PROXY_MODELS = {
    "gpt-5.3-codex-spark": "gpt-5.5",
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


def long_window_label(rate_window: RateWindow) -> str:
    label = rate_window.label
    if label.endswith("h"):
        return f"{label[:-1]}-hour limit"
    if label.endswith("d"):
        return f"{label[:-1]}-day limit"
    if label.endswith("m"):
        return f"{label[:-1]}-minute limit"
    if label.lower() in {"limit", "rate limit"}:
        return "Rate limit"
    return f"{label} limit"


def parse_rate_window(raw: Any, fallback_label: str = "limit") -> RateWindow | None:
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

    primary = parse_rate_window(rate_limits.get("primary"))
    secondary = parse_rate_window(rate_limits.get("secondary"))
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
    if normalized == "gpt-5.6":
        return "gpt-5.6-sol"
    return normalized


def pricing_for_model(model: str | None) -> ModelPricing | None:
    key = normalize_model_key(model)
    if key is None:
        return None
    return API_MODEL_PRICING.get(key)


def resolve_model_pricing(model: str | None) -> PricingResolution | None:
    key = normalize_model_key(model)
    if key is None:
        return None

    exact_pricing = API_MODEL_PRICING.get(key)
    if exact_pricing is not None:
        return PricingResolution(pricing_model=key, pricing=exact_pricing)

    proxy_model = API_PRICING_PROXY_MODELS.get(key)
    proxy_pricing = API_MODEL_PRICING.get(proxy_model) if proxy_model else None
    if proxy_model is None or proxy_pricing is None:
        return None
    return PricingResolution(pricing_model=proxy_model, pricing=proxy_pricing, is_proxy=True)


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
    short_context_usage: TokenUsage | None = None,
    long_context_usage: TokenUsage | None = None,
    long_context_request_count: int = 0,
) -> ApiCostEstimate:
    if short_context_usage is None and long_context_usage is None:
        short_context_usage = usage
        long_context_usage = TokenUsage()
    else:
        short_context_usage = short_context_usage or TokenUsage()
        long_context_usage = long_context_usage or TokenUsage()

    short_cached_tokens = min(
        short_context_usage.cached_input_tokens,
        short_context_usage.input_tokens,
    )
    long_cached_tokens = min(
        long_context_usage.cached_input_tokens,
        long_context_usage.input_tokens,
    )
    cached_input_tokens = short_cached_tokens + long_cached_tokens
    uncached_input_tokens = (
        short_context_usage.input_tokens
        - short_cached_tokens
        + long_context_usage.input_tokens
        - long_cached_tokens
    )
    output_tokens = short_context_usage.output_tokens + long_context_usage.output_tokens
    pricing_resolution = resolve_model_pricing(detected_model.model)
    pricing = pricing_resolution.pricing if pricing_resolution else None

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
            pricing_model=None,
            pricing_is_proxy=False,
            uncached_input_tokens=uncached_input_tokens,
            cached_input_tokens=cached_input_tokens,
            output_tokens=output_tokens,
            input_cost=None,
            cached_input_cost=None,
            output_cost=None,
            total_cost=None,
            long_context_request_count=long_context_request_count,
            warning=warning,
        )

    short_cached_rate = pricing.cached_input_per_million
    if short_cached_rate is None:
        short_cached_rate = pricing.input_per_million

    long_input_rate = pricing.long_context_input_per_million
    if long_input_rate is None:
        long_input_rate = pricing.input_per_million
    long_cached_rate = pricing.long_context_cached_input_per_million
    if long_cached_rate is None:
        long_cached_rate = long_input_rate
    long_output_rate = pricing.long_context_output_per_million
    if long_output_rate is None:
        long_output_rate = pricing.output_per_million

    short_uncached_tokens = short_context_usage.input_tokens - short_cached_tokens
    long_uncached_tokens = long_context_usage.input_tokens - long_cached_tokens
    input_cost = (
        short_uncached_tokens * pricing.input_per_million
        + long_uncached_tokens * long_input_rate
    ) / 1_000_000
    cached_input_cost = (
        short_cached_tokens * short_cached_rate
        + long_cached_tokens * long_cached_rate
    ) / 1_000_000
    output_cost = (
        short_context_usage.output_tokens * pricing.output_per_million
        + long_context_usage.output_tokens * long_output_rate
    ) / 1_000_000
    return ApiCostEstimate(
        model=detected_model.model,
        model_source=detected_model.source,
        pricing=pricing,
        pricing_model=pricing_resolution.pricing_model,
        pricing_is_proxy=pricing_resolution.is_proxy,
        uncached_input_tokens=uncached_input_tokens,
        cached_input_tokens=cached_input_tokens,
        output_tokens=output_tokens,
        input_cost=input_cost,
        cached_input_cost=cached_input_cost,
        output_cost=output_cost,
        total_cost=input_cost + cached_input_cost + output_cost,
        long_context_request_count=long_context_request_count,
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


def format_published_api_rate(value: float | None) -> str:
    if value is None:
        return "--"
    return format_api_cost(value)


def format_model_name(model: str | None) -> str:
    if not model:
        return "unknown"
    if model.lower().startswith("gpt-"):
        return model.upper()
    return model


def format_api_cost_estimate(estimate: ApiCostEstimate) -> str:
    if estimate.total_cost is None:
        return "API est. --"
    if estimate.pricing_is_proxy:
        return f"{format_api_cost(estimate.total_cost)} API est. ({format_model_name(estimate.pricing_model)} proxy)"
    return f"{format_api_cost(estimate.total_cost)} API est."


def model_pricing_to_dict(pricing: ModelPricing | None) -> dict[str, Any] | None:
    if pricing is None:
        return None
    return {
        "display_name": pricing.display_name,
        "input_per_million": pricing.input_per_million,
        "cached_input_per_million": pricing.cached_input_per_million,
        "cache_write_per_million": pricing.cache_write_per_million,
        "output_per_million": pricing.output_per_million,
        "long_context_threshold_tokens": pricing.long_context_threshold_tokens,
        "long_context_input_per_million": pricing.long_context_input_per_million,
        "long_context_cached_input_per_million": pricing.long_context_cached_input_per_million,
        "long_context_cache_write_per_million": pricing.long_context_cache_write_per_million,
        "long_context_output_per_million": pricing.long_context_output_per_million,
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
        "pricing_model": estimate.pricing_model,
        "pricing_is_proxy": estimate.pricing_is_proxy,
        "uncached_input_tokens": estimate.uncached_input_tokens,
        "cached_input_tokens": estimate.cached_input_tokens,
        "output_tokens": estimate.output_tokens,
        "input_cost": estimate.input_cost,
        "cached_input_cost": estimate.cached_input_cost,
        "output_cost": estimate.output_cost,
        "long_context_request_count": estimate.long_context_request_count,
        "cache_write_cost": estimate.cache_write_cost,
        "cache_write_cost_included": False,
        "cache_write_note": CACHE_WRITE_TELEMETRY_NOTE,
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

    primary = parse_rate_window(rate_limits.get("primary"))
    secondary = parse_rate_window(rate_limits.get("secondary"))
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
        self._last_snapshot: RateSnapshot | None = None
        self._last_row_id: int | None = None
        self._database_identity: tuple[int, int] | None = None
        self._database_signature: tuple[tuple[int, int, int, str] | None, ...] | None = None
        self._last_probe_at = 0.0
        self._retry_required = False

    @staticmethod
    def _path_signature(path: Path) -> tuple[int, int, int, str] | None:
        try:
            stat_result = path.stat()
        except OSError:
            return None
        fingerprint = hashlib.blake2s(digest_size=8)
        try:
            with path.open("rb") as handle:
                header = handle.read(100)
                fingerprint.update(header[24:32])
                if stat_result.st_size > 100:
                    handle.seek(max(0, int(stat_result.st_size) - 32))
                    fingerprint.update(handle.read(32))
        except OSError:
            fingerprint.update(b"unreadable")
        return (
            int(stat_result.st_size),
            int(stat_result.st_mtime_ns),
            int(stat_result.st_ctime_ns),
            fingerprint.hexdigest(),
        )

    def _current_database_signature(self) -> tuple[tuple[int, int, int, str] | None, ...]:
        return tuple(
            self._path_signature(Path(f"{self.path}{suffix}"))
            for suffix in ("", "-wal", "-shm")
        )

    def _current_database_identity(self) -> tuple[int, int] | None:
        try:
            stat_result = self.path.stat()
        except OSError:
            return None
        file_identifier = int(stat_result.st_ino)
        if file_identifier == 0:
            file_identifier = int(stat_result.st_ctime_ns)
        return (int(stat_result.st_dev), file_identifier)

    def latest_snapshot(
        self,
        row_limit: int = SQLITE_RATE_ROWS_TO_SCAN,
        force_rescan: bool = False,
        now: float | None = None,
    ) -> RateSnapshot | None:
        self.last_error = None
        if not self.path.exists():
            return None

        observed_at = time.monotonic() if now is None else now
        database_signature = self._current_database_signature()
        database_identity = self._current_database_identity()
        signature_changed = database_signature != self._database_signature
        fallback_probe_due = (
            self._last_row_id is None
            or observed_at - self._last_probe_at >= SQLITE_FALLBACK_PROBE_INTERVAL_SECONDS
        )
        if (
            not force_rescan
            and self._last_row_id is not None
            and not signature_changed
            and not fallback_probe_due
            and not self._retry_required
        ):
            return self._last_snapshot

        connection = None
        try:
            uri = self.path.resolve().as_uri() + "?mode=ro"
            connection = sqlite3.connect(uri, uri=True, timeout=0.2)
            max_row = connection.execute("SELECT MAX(id) FROM logs").fetchone()
            max_row_id = parse_int(max_row[0] if max_row else 0)
            database_replaced = (
                self._database_identity is not None
                and database_identity is not None
                and database_identity != self._database_identity
            )
            row_id_rolled_back = (
                self._last_row_id is not None and max_row_id < self._last_row_id
            )
            full_rescan = (
                force_rescan
                or self._last_row_id is None
                or database_replaced
                or row_id_rolled_back
            )

            if full_rescan:
                rows = connection.execute(
                    """
                    SELECT id, ts, target, feedback_log_body
                    FROM logs
                    WHERE feedback_log_body LIKE ?
                      AND feedback_log_body LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (
                        f"%{SQLITE_RATE_LOG_MARKER}%",
                        f"%{SQLITE_RATE_EVENT_TYPE}%",
                        row_limit,
                    ),
                )
            elif max_row_id > self._last_row_id:
                rows = connection.execute(
                    """
                    SELECT id, ts, target, feedback_log_body
                    FROM logs
                    WHERE id > ?
                      AND feedback_log_body LIKE ?
                      AND feedback_log_body LIKE ?
                    ORDER BY id DESC
                    LIMIT ?
                    """,
                    (
                        self._last_row_id,
                        f"%{SQLITE_RATE_LOG_MARKER}%",
                        f"%{SQLITE_RATE_EVENT_TYPE}%",
                        row_limit,
                    ),
                )
            else:
                rows = ()

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
            if snapshots:
                newest = max(snapshots, key=timestamp_sort_key)
                if (
                    self._last_snapshot is None
                    or timestamp_sort_key(newest) >= timestamp_sort_key(self._last_snapshot)
                ):
                    self._last_snapshot = newest

            self._last_row_id = max_row_id
            self._database_identity = database_identity
            self._database_signature = database_signature
            self._last_probe_at = (
                time.monotonic() if now is None else now
            )
            self._retry_required = False
            return self._last_snapshot
        except (OSError, sqlite3.Error, ValueError) as exc:
            self.last_error = str(exc)
            self._retry_required = True
            return self._last_snapshot
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
        self._last_full_session_scan_at: float | None = None
        self.last_error: str | None = None

    def read_updates(
        self,
        force_rescan: bool = False,
        now: float | None = None,
    ) -> LogReadBatch:
        try:
            self.last_error = None
            observed_at = time.monotonic() if now is None else now
            self._session_files = self._refresh_session_files(
                force_rescan=force_rescan,
                now=observed_at,
            )
            active_files = self._session_files[:MAX_SESSION_FILES_TO_SCAN]
            self._prune_tracking({path for path, _stat_result in active_files})

            snapshots: list[RateSnapshot] = []
            token_events: list[TokenEvent] = []
            for path, stat_result in active_files:
                text = self._read_file_updates(path, stat_result, force_tail=force_rescan)
                snapshots.extend(parse_rate_snapshots_from_text(text, str(path)))
                token_events.extend(parse_token_events_from_text(text, str(path)))

            sqlite_snapshot = self.sqlite_reader.latest_snapshot(
                force_rescan=force_rescan,
                now=observed_at,
            )
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

    def latest_snapshot(
        self,
        force_rescan: bool = False,
        now: float | None = None,
    ) -> RateSnapshot | None:
        return self.read_updates(force_rescan=force_rescan, now=now).snapshot

    def _refresh_session_files(
        self,
        force_rescan: bool,
        now: float,
    ) -> list[tuple[Path, os.stat_result]]:
        sessions_dir = self.codex_home / "sessions"
        if not sessions_dir.exists():
            self.last_error = f"Missing sessions folder: {sessions_dir}"
            return []

        full_rescan_due = (
            force_rescan
            or self._last_full_session_scan_at is None
            or now - self._last_full_session_scan_at >= SESSION_FULL_RESCAN_INTERVAL_SECONDS
        )
        if full_rescan_due:
            files = self._find_session_files()
            self._last_full_session_scan_at = now
            return files[:MAX_SESSION_FILES_TO_SCAN]

        candidates: dict[Path, os.stat_result] = {}
        for path, _old_stat in self._session_files[:MAX_SESSION_FILES_TO_SCAN]:
            try:
                candidates[path] = path.stat()
            except OSError:
                continue

        hot_directories = {
            path.parent
            for path, _stat_result in self._session_files[:MAX_SESSION_FILES_TO_SCAN]
        }
        for current in (datetime.now(), datetime.now(timezone.utc)):
            hot_directories.add(
                sessions_dir
                / f"{current.year:04d}"
                / f"{current.month:02d}"
                / f"{current.day:02d}"
            )

        for directory in hot_directories:
            try:
                paths = directory.glob("*.jsonl")
                for path in paths:
                    try:
                        candidates[path] = path.stat()
                    except OSError:
                        continue
            except OSError:
                continue

        return sorted(
            candidates.items(),
            key=lambda item: item[1].st_mtime,
            reverse=True,
        )[:MAX_SESSION_FILES_TO_SCAN]

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


def available_rate_window_keys(snapshot: RateSnapshot | None) -> list[str]:
    if snapshot is None:
        return []
    return [key for key in VALID_DISPLAY_WINDOWS if getattr(snapshot, key) is not None]


def effective_display_windows(
    settings: dict[str, Any],
    snapshot: RateSnapshot | None,
) -> list[str]:
    available = available_rate_window_keys(snapshot)
    if not available:
        return []
    selected = normalize_display_windows(settings.get("display_windows"))
    visible = [key for key in selected if key in available]
    return visible or available


def active_display_widget_keys(
    settings: dict[str, Any],
    snapshot: RateSnapshot | None = None,
) -> list[str]:
    keys = effective_display_windows(settings, snapshot) or ["rate_waiting"]
    if settings.get("show_token_counter", False):
        keys.append("token_counter")
    if settings.get("show_api_cost_estimate", False):
        keys.append("api_cost")
    return keys


def layout_position(index: int, layout_mode: str) -> tuple[int, int]:
    mode = normalize_layout_mode(layout_mode)
    if mode == "vertical":
        return (index, 0)
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


def clamp_popup_position(
    position: list[int] | tuple[int, int],
    bounds: tuple[int, int, int, int],
    window_size: tuple[int, int],
) -> list[int]:
    left, top, width, height = bounds
    window_width, window_height = normalize_window_size(window_size)
    if width <= 0 or height <= 0:
        return [0, 0]

    usable_left, usable_top, usable_right, usable_bottom = popup_usable_edges(bounds)
    available_width = max(1, usable_right - usable_left)
    available_height = max(1, usable_bottom - usable_top)
    anchor_x = int(position[0])
    anchor_y = int(position[1])

    if window_width > available_width:
        x = usable_left
    else:
        space_right = usable_right - anchor_x
        space_left = anchor_x - usable_left
        x = anchor_x if window_width <= space_right or space_right >= space_left else anchor_x - window_width

    if window_height > available_height:
        y = usable_top
    else:
        space_below = usable_bottom - anchor_y
        space_above = anchor_y - usable_top
        y = anchor_y if window_height <= space_below or space_below >= space_above else anchor_y - window_height

    max_x = usable_right - window_width
    max_y = usable_bottom - window_height
    return [
        clamp_int(x, usable_left, max_x),
        clamp_int(y, usable_top, max_y),
    ]


def popup_usable_edges(
    bounds: tuple[int, int, int, int],
    padding: int = MENU_SCREEN_PADDING,
) -> tuple[int, int, int, int]:
    left, top, width, height = bounds
    if width <= 0 or height <= 0:
        return (0, 0, 1, 1)
    horizontal_padding = min(max(0, padding), max(0, (width - 1) // 2))
    vertical_padding = min(max(0, padding), max(0, (height - 1) // 2))
    return (
        left + horizontal_padding,
        top + vertical_padding,
        left + width - horizontal_padding,
        top + height - vertical_padding,
    )


def popup_max_size(bounds: tuple[int, int, int, int]) -> tuple[int, int]:
    usable_left, usable_top, usable_right, usable_bottom = popup_usable_edges(bounds)
    return (
        max(1, usable_right - usable_left),
        max(1, usable_bottom - usable_top),
    )


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


def bounds_edges(bounds: tuple[int, int, int, int]) -> tuple[int, int, int, int]:
    left, top, width, height = bounds
    return (left, top, left + max(0, width), top + max(0, height))


def overlay_bounds(
    position: list[int] | tuple[int, int],
    window_size: tuple[int, int] | None = None,
) -> tuple[int, int, int, int]:
    width, height = normalize_window_size(window_size)
    return (int(position[0]), int(position[1]), width, height)


def bounds_contains(
    container: tuple[int, int, int, int],
    item: tuple[int, int, int, int],
) -> bool:
    container_left, container_top, container_right, container_bottom = bounds_edges(
        container
    )
    item_left, item_top, item_right, item_bottom = bounds_edges(item)
    return (
        item_left >= container_left
        and item_top >= container_top
        and item_right <= container_right
        and item_bottom <= container_bottom
    )


def bounds_intersection_size(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> tuple[int, int]:
    first_left, first_top, first_right, first_bottom = bounds_edges(first)
    second_left, second_top, second_right, second_bottom = bounds_edges(second)
    return (
        max(0, min(first_right, second_right) - max(first_left, second_left)),
        max(0, min(first_bottom, second_bottom) - max(first_top, second_top)),
    )


def bounds_distance_squared(
    first: tuple[int, int, int, int],
    second: tuple[int, int, int, int],
) -> int:
    first_left, first_top, first_right, first_bottom = bounds_edges(first)
    second_left, second_top, second_right, second_bottom = bounds_edges(second)
    horizontal_gap = max(first_left - second_right, second_left - first_right, 0)
    vertical_gap = max(first_top - second_bottom, second_top - first_bottom, 0)
    return horizontal_gap * horizontal_gap + vertical_gap * vertical_gap


def _valid_monitor_work_areas(
    monitors: list[MonitorWorkArea] | tuple[MonitorWorkArea, ...],
) -> list[MonitorWorkArea]:
    return [
        monitor
        for monitor in monitors
        if monitor.monitor_bounds[2] > 0
        and monitor.monitor_bounds[3] > 0
        and monitor.work_area[2] > 0
        and monitor.work_area[3] > 0
    ]


def _monitor_tie_key(monitor: MonitorWorkArea) -> tuple[Any, ...]:
    return (
        0 if monitor.is_primary else 1,
        monitor.device_name.casefold(),
        monitor.work_area,
        monitor.monitor_bounds,
    )


def primary_monitor_work_area(
    monitors: list[MonitorWorkArea] | tuple[MonitorWorkArea, ...],
) -> MonitorWorkArea | None:
    valid = _valid_monitor_work_areas(monitors)
    if not valid:
        return None
    return min(valid, key=_monitor_tie_key)


def choose_monitor_for_overlay(
    position: list[int] | tuple[int, int],
    window_size: tuple[int, int] | None,
    monitors: list[MonitorWorkArea] | tuple[MonitorWorkArea, ...],
) -> MonitorWorkArea | None:
    valid = _valid_monitor_work_areas(monitors)
    if not valid:
        return None

    rectangle = overlay_bounds(position, window_size)
    width, height = normalize_window_size(window_size)
    minimum_width = min(MIN_VISIBLE_PIXELS, width)
    minimum_height = min(MIN_VISIBLE_PIXELS, height)

    contained = [monitor for monitor in valid if bounds_contains(monitor.work_area, rectangle)]
    if contained:
        return min(contained, key=_monitor_tie_key)

    work_intersections: list[tuple[int, MonitorWorkArea]] = []
    for monitor in valid:
        intersection_width, intersection_height = bounds_intersection_size(
            rectangle,
            monitor.work_area,
        )
        if intersection_width >= minimum_width and intersection_height >= minimum_height:
            work_intersections.append((intersection_width * intersection_height, monitor))
    if work_intersections:
        greatest_area = max(area for area, _monitor in work_intersections)
        return min(
            (monitor for area, monitor in work_intersections if area == greatest_area),
            key=_monitor_tie_key,
        )

    monitor_intersections: list[tuple[int, MonitorWorkArea]] = []
    for monitor in valid:
        intersection_width, intersection_height = bounds_intersection_size(
            rectangle,
            monitor.monitor_bounds,
        )
        area = intersection_width * intersection_height
        if area > 0:
            monitor_intersections.append((area, monitor))
    if monitor_intersections:
        greatest_area = max(area for area, _monitor in monitor_intersections)
        return min(
            (monitor for area, monitor in monitor_intersections if area == greatest_area),
            key=_monitor_tie_key,
        )

    nearest_distance = min(
        bounds_distance_squared(rectangle, monitor.work_area) for monitor in valid
    )
    return min(
        (
            monitor
            for monitor in valid
            if bounds_distance_squared(rectangle, monitor.work_area) == nearest_distance
        ),
        key=_monitor_tie_key,
    )


def normalize_overlay_position_for_monitors(
    value: Any,
    monitors: list[MonitorWorkArea] | tuple[MonitorWorkArea, ...],
    window_size: tuple[int, int] | None = None,
) -> list[int] | None:
    valid = _valid_monitor_work_areas(monitors)
    if not valid:
        return None

    if (
        isinstance(value, (list, tuple))
        and len(value) == 2
        and all(isinstance(item, int) for item in value)
    ):
        requested = [int(value[0]), int(value[1])]
        monitor = choose_monitor_for_overlay(requested, window_size, valid)
        if monitor is None:
            return None
        if bounds_contains(monitor.work_area, overlay_bounds(requested, window_size)):
            return requested
        return clamp_overlay_position(requested, monitor.work_area, window_size)

    monitor = primary_monitor_work_area(valid)
    if monitor is None:
        return None
    return default_overlay_position(monitor.work_area, window_size)


def monitor_topology_fingerprint(
    monitors: list[MonitorWorkArea] | tuple[MonitorWorkArea, ...],
) -> tuple[tuple[Any, ...], ...]:
    return tuple(
        sorted(
            (
                monitor.device_name.casefold(),
                monitor.monitor_bounds,
                monitor.work_area,
                bool(monitor.is_primary),
            )
            for monitor in _valid_monitor_work_areas(monitors)
        )
    )


WM_SETTINGCHANGE = 0x001A
WM_DISPLAYCHANGE = 0x007E
WM_NCDESTROY = 0x0082
WM_POWERBROADCAST = 0x0218
WM_DEVICECHANGE = 0x0219
WM_DPICHANGED = 0x02E0
SPI_SETWORKAREA = 0x002F
DBT_DEVNODES_CHANGED = 0x0007
DBT_CONFIGCHANGED = 0x0018
PBT_APMRESUMECRITICAL = 0x0006
PBT_APMRESUMESUSPEND = 0x0007
PBT_APMRESUMEAUTOMATIC = 0x0012


def is_display_reconcile_message(message: int, wparam: int = 0) -> bool:
    if message in (WM_DISPLAYCHANGE, WM_DPICHANGED):
        return True
    if message == WM_SETTINGCHANGE:
        # Shell and taskbar implementations are not uniform about wParam. A
        # fingerprint comparison makes unrelated setting notifications cheap.
        return True
    if message == WM_DEVICECHANGE:
        # Arrival/removal broadcasts are also useful hints. The later monitor
        # fingerprint comparison filters device changes unrelated to displays.
        return True
    if message == WM_POWERBROADCAST:
        return wparam in (
            PBT_APMRESUMECRITICAL,
            PBT_APMRESUMESUSPEND,
            PBT_APMRESUMEAUTOMATIC,
        )
    return False


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


class WindowsMonitorInfo(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.wintypes.DWORD),
        ("rcMonitor", ctypes.wintypes.RECT),
        ("rcWork", ctypes.wintypes.RECT),
        ("dwFlags", ctypes.wintypes.DWORD),
    ]


class WindowsMonitorInfoEx(ctypes.Structure):
    _fields_ = WindowsMonitorInfo._fields_ + [
        ("szDevice", ctypes.wintypes.WCHAR * 32),
    ]


class WindowsDisplayDevice(ctypes.Structure):
    _fields_ = [
        ("cb", ctypes.wintypes.DWORD),
        ("DeviceName", ctypes.wintypes.WCHAR * 32),
        ("DeviceString", ctypes.wintypes.WCHAR * 128),
        ("StateFlags", ctypes.wintypes.DWORD),
        ("DeviceID", ctypes.wintypes.WCHAR * 128),
        ("DeviceKey", ctypes.wintypes.WCHAR * 128),
    ]


def _windows_active_display_device_names(user32: Any) -> set[str] | None:
    attached_to_desktop = 0x00000001
    mirroring_driver = 0x00000008
    try:
        user32.EnumDisplayDevicesW.argtypes = [
            ctypes.wintypes.LPCWSTR,
            ctypes.wintypes.DWORD,
            ctypes.POINTER(WindowsDisplayDevice),
            ctypes.wintypes.DWORD,
        ]
        user32.EnumDisplayDevicesW.restype = ctypes.wintypes.BOOL
        devices: set[str] = set()
        for index in range(64):
            device = WindowsDisplayDevice()
            device.cb = ctypes.sizeof(WindowsDisplayDevice)
            if not user32.EnumDisplayDevicesW(None, index, ctypes.byref(device), 0):
                break
            flags = int(device.StateFlags)
            name = str(device.DeviceName).casefold()
            if name and flags & attached_to_desktop and not flags & mirroring_driver:
                devices.add(name)
        return devices or None
    except (AttributeError, OSError, TypeError, ValueError):
        return None


def windows_monitor_work_areas() -> tuple[MonitorWorkArea, ...]:
    if platform.system() != "Windows":
        return ()
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        active_devices = _windows_active_display_device_names(user32)
        if not active_devices:
            return ()
        callback_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        callback_type = callback_factory(
            ctypes.wintypes.BOOL,
            ctypes.c_void_p,
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.wintypes.RECT),
            ctypes.wintypes.LPARAM,
        )
        monitors: list[MonitorWorkArea] = []
        seen: set[tuple[Any, ...]] = set()
        callback_errors: list[BaseException] = []

        def collect_monitor(
            monitor_handle: int,
            _device_context: int,
            _monitor_rect: ctypes.POINTER(ctypes.wintypes.RECT),
            _data: int,
        ) -> bool:
            try:
                info = WindowsMonitorInfoEx()
                info.cbSize = ctypes.sizeof(WindowsMonitorInfoEx)
                if not user32.GetMonitorInfoW(monitor_handle, ctypes.byref(info)):
                    callback_errors.append(OSError("GetMonitorInfoW failed"))
                    return False
                monitor_rect = info.rcMonitor
                work_rect = info.rcWork
                monitor_bounds = (
                    int(monitor_rect.left),
                    int(monitor_rect.top),
                    int(monitor_rect.right - monitor_rect.left),
                    int(monitor_rect.bottom - monitor_rect.top),
                )
                work_area = (
                    int(work_rect.left),
                    int(work_rect.top),
                    int(work_rect.right - work_rect.left),
                    int(work_rect.bottom - work_rect.top),
                )
                device_name = str(info.szDevice)
                normalized_device_name = device_name.casefold()
                if normalized_device_name not in active_devices:
                    return True
                if monitor_bounds[2] <= 0 or monitor_bounds[3] <= 0:
                    callback_errors.append(ValueError("Invalid active monitor rectangle"))
                    return False
                if work_area[2] <= 0 or work_area[3] <= 0:
                    callback_errors.append(ValueError("Invalid active monitor work area"))
                    return False
                identity = (normalized_device_name, monitor_bounds, work_area)
                if identity in seen:
                    return True
                seen.add(identity)
                monitors.append(
                    MonitorWorkArea(
                        device_name=device_name,
                        monitor_bounds=monitor_bounds,
                        work_area=work_area,
                        is_primary=bool(int(info.dwFlags) & 1),
                    )
                )
                return True
            except BaseException as exc:
                callback_errors.append(exc)
                return False

        callback = callback_type(collect_monitor)
        user32.GetMonitorInfoW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(WindowsMonitorInfoEx),
        ]
        user32.GetMonitorInfoW.restype = ctypes.wintypes.BOOL
        user32.EnumDisplayMonitors.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(ctypes.wintypes.RECT),
            callback_type,
            ctypes.wintypes.LPARAM,
        ]
        user32.EnumDisplayMonitors.restype = ctypes.wintypes.BOOL
        if not user32.EnumDisplayMonitors(None, None, callback, 0) or callback_errors:
            return ()
        expected_monitor_count = int(user32.GetSystemMetrics(80))
        if expected_monitor_count <= 0 or len(monitors) != expected_monitor_count:
            return ()
        return tuple(sorted(monitors, key=_monitor_tie_key))
    except (AttributeError, OSError, TypeError, ValueError):
        return ()


def windows_monitor_work_area(position: list[int] | tuple[int, int]) -> tuple[int, int, int, int] | None:
    if platform.system() != "Windows":
        return None
    monitors = windows_monitor_work_areas()
    selected = choose_monitor_for_overlay(position, (1, 1), monitors)
    if selected is not None:
        return selected.work_area
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.MonitorFromPoint.argtypes = [ctypes.wintypes.POINT, ctypes.wintypes.DWORD]
        user32.MonitorFromPoint.restype = ctypes.c_void_p
        user32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.POINTER(WindowsMonitorInfo)]
        user32.GetMonitorInfoW.restype = ctypes.wintypes.BOOL
        point = ctypes.wintypes.POINT(int(position[0]), int(position[1]))
        monitor = user32.MonitorFromPoint(point, 2)
        if not monitor:
            return None
        info = WindowsMonitorInfo()
        info.cbSize = ctypes.sizeof(WindowsMonitorInfo)
        if not user32.GetMonitorInfoW(monitor, ctypes.byref(info)):
            return None
        rect = info.rcWork
        width = int(rect.right - rect.left)
        height = int(rect.bottom - rect.top)
        if width <= 0 or height <= 0:
            return None
        return (int(rect.left), int(rect.top), width, height)
    except (OSError, AttributeError, TypeError, ValueError):
        return None


class WindowsDisplayChangeObserver:
    """Best-effort observer; native callbacks only record display hints."""

    SUBCLASS_ID = 0x434F4458
    GA_ROOT = 2

    def __init__(self, root: tk.Tk) -> None:
        self.root = root
        self._hwnd = 0
        self._closing = False
        self._pending: set[tuple[int, int]] = set()
        self._callback_error: BaseException | None = None
        self._user32: Any = None
        self._comctl32: Any = None
        self._kernel32: Any = None
        self._proc_type: Any = None
        self._proc: Any = None

        if platform.system() != "Windows":
            return
        try:
            self._initialize_native_api()
            self.root.bind("<Map>", self._on_map, add="+")
            self.root.after_idle(self.install_if_mapped)
        except (AttributeError, OSError, TypeError, tk.TclError):
            self._user32 = None
            self._comctl32 = None
            self._kernel32 = None
            self._proc = None

    def _initialize_native_api(self) -> None:
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._comctl32 = ctypes.WinDLL("comctl32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        callback_factory = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        self._proc_type = callback_factory(
            ctypes.c_ssize_t,
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
            ctypes.c_size_t,
            ctypes.c_size_t,
        )
        self._proc = self._proc_type(self._subclass_proc)

        self._comctl32.SetWindowSubclass.argtypes = [
            ctypes.wintypes.HWND,
            self._proc_type,
            ctypes.c_size_t,
            ctypes.c_size_t,
        ]
        self._comctl32.SetWindowSubclass.restype = ctypes.wintypes.BOOL
        self._comctl32.DefSubclassProc.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.wintypes.UINT,
            ctypes.c_size_t,
            ctypes.c_ssize_t,
        ]
        self._comctl32.DefSubclassProc.restype = ctypes.c_ssize_t
        self._comctl32.RemoveWindowSubclass.argtypes = [
            ctypes.wintypes.HWND,
            self._proc_type,
            ctypes.c_size_t,
        ]
        self._comctl32.RemoveWindowSubclass.restype = ctypes.wintypes.BOOL
        self._user32.GetAncestor.argtypes = [ctypes.wintypes.HWND, ctypes.wintypes.UINT]
        self._user32.GetAncestor.restype = ctypes.wintypes.HWND
        self._user32.IsWindow.argtypes = [ctypes.wintypes.HWND]
        self._user32.IsWindow.restype = ctypes.wintypes.BOOL
        self._user32.GetWindowThreadProcessId.argtypes = [
            ctypes.wintypes.HWND,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        self._user32.GetWindowThreadProcessId.restype = ctypes.wintypes.DWORD
        self._kernel32.GetCurrentThreadId.argtypes = []
        self._kernel32.GetCurrentThreadId.restype = ctypes.wintypes.DWORD

    @staticmethod
    def _handle_value(value: Any) -> int:
        return int(getattr(value, "value", value) or 0)

    @staticmethod
    def _parse_handle(value: Any) -> int:
        text = str(value).strip()
        return int(text, 16) if text.lower().startswith("0x") else int(text, 10)

    def _wrapper_handle(self) -> int:
        raw_handle = self.root.tk.call("wm", "frame", self.root._w)
        candidate = self._parse_handle(raw_handle)
        root_handle = self._user32.GetAncestor(ctypes.wintypes.HWND(candidate), self.GA_ROOT)
        return self._handle_value(root_handle) or candidate

    def _on_map(self, event: tk.Event) -> None:
        if getattr(event, "widget", self.root) is self.root and not self._closing:
            self.root.after_idle(self.install_if_mapped)

    def install_if_mapped(self) -> bool:
        if self._closing or self._proc is None or self._user32 is None:
            return False
        try:
            if not self.root.winfo_exists() or not self.root.winfo_ismapped():
                return False
            hwnd = self._wrapper_handle()
            if not hwnd or not self._user32.IsWindow(ctypes.wintypes.HWND(hwnd)):
                return False
            if hwnd == self._hwnd:
                return True
            owner_thread = int(
                self._user32.GetWindowThreadProcessId(ctypes.wintypes.HWND(hwnd), None)
            )
            if owner_thread != int(self._kernel32.GetCurrentThreadId()):
                return False
            if self._hwnd and not self.detach():
                return False
            installed = bool(
                self._comctl32.SetWindowSubclass(
                    ctypes.wintypes.HWND(hwnd),
                    self._proc,
                    ctypes.c_size_t(self.SUBCLASS_ID),
                    ctypes.c_size_t(0),
                )
            )
            if installed:
                self._hwnd = hwnd
            return installed
        except (AttributeError, OSError, TypeError, ValueError, tk.TclError):
            return False

    def _subclass_proc(
        self,
        hwnd: int,
        message: int,
        wparam: int,
        lparam: int,
        _subclass_id: int,
        _reference_data: int,
    ) -> int:
        try:
            message_value = int(message)
            wparam_value = int(wparam)
            if message_value == WM_NCDESTROY:
                self._comctl32.RemoveWindowSubclass(
                    hwnd,
                    self._proc,
                    ctypes.c_size_t(self.SUBCLASS_ID),
                )
                if self._hwnd == self._handle_value(hwnd):
                    self._hwnd = 0
            elif is_display_reconcile_message(message_value, wparam_value):
                self._pending.add((message_value, wparam_value))
        except BaseException as exc:
            # Exceptions must never cross a native callback boundary.
            self._callback_error = exc
        return int(self._comctl32.DefSubclassProc(hwnd, message, wparam, lparam))

    def take_pending(self) -> set[tuple[int, int]]:
        pending = self._pending
        self._pending = set()
        return pending

    def take_callback_error(self) -> BaseException | None:
        error = self._callback_error
        self._callback_error = None
        return error

    def detach(self) -> bool:
        if not self._hwnd:
            return True
        try:
            hwnd = ctypes.wintypes.HWND(self._hwnd)
            if self._user32.IsWindow(hwnd):
                removed = bool(
                    self._comctl32.RemoveWindowSubclass(
                        hwnd,
                        self._proc,
                        ctypes.c_size_t(self.SUBCLASS_ID),
                    )
                )
                if not removed:
                    return False
            self._hwnd = 0
            return True
        except (AttributeError, OSError, TypeError, ValueError):
            return False

    def close_before_root_destroy(self) -> bool:
        self._closing = True
        return self.detach()


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


def diagnostic_error_text(exc: BaseException, limit: int = 500) -> str:
    if isinstance(exc, OSError) and exc.strerror:
        message = exc.strerror
    else:
        message = str(exc)
    compact = " ".join(message.split()) or "unknown error"
    return f"{type(exc).__name__}: {compact}"[:limit]


def save_settings(settings: dict[str, Any], path: Path | None = None) -> str | None:
    target = path or settings_path()
    temp_path = target.with_name(target.name + ".tmp")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        with temp_path.open("w", encoding="utf-8") as handle:
            json.dump(settings, handle, indent=2, sort_keys=True)
            handle.write("\n")
            handle.flush()
            os.fsync(handle.fileno())
        temp_path.replace(target)
    except (OSError, TypeError, ValueError) as exc:
        try:
            temp_path.unlink()
        except (FileNotFoundError, OSError):
            pass
        return diagnostic_error_text(exc)
    return None


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
        self.short_context_totals = TokenUsage()
        self.long_context_totals = TokenUsage()
        self.long_context_request_count = 0
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
            if event.usage.input_tokens > LONG_CONTEXT_INPUT_THRESHOLD_TOKENS:
                self.long_context_totals = add_token_usage(self.long_context_totals, event.usage)
                self.long_context_request_count += 1
            else:
                self.short_context_totals = add_token_usage(self.short_context_totals, event.usage)
            self.seen_events.add(event.fingerprint)
            self.last_update_at = current

    def reset(self, now: float | None = None) -> None:
        self.reset_at = time.time() if now is None else now
        self.totals = TokenUsage()
        self.short_context_totals = TokenUsage()
        self.long_context_totals = TokenUsage()
        self.long_context_request_count = 0
        self.seen_events.clear()
        self.last_update_at = None

    def display_text(self, now: float | None = None) -> str:
        return format_token_counter(self.totals.total_tokens, self.reset_at, now)

    def state_dict(self) -> dict[str, Any]:
        return {
            "reset_at": self.reset_at,
            "last_update_at": self.last_update_at,
            "totals": token_usage_to_dict(self.totals),
            "short_context_totals": token_usage_to_dict(self.short_context_totals),
            "long_context_totals": token_usage_to_dict(self.long_context_totals),
            "long_context_request_count": self.long_context_request_count,
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
        STILL_ACTIVE = 259
        kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.GetExitCodeProcess.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        kernel32.GetExitCodeProcess.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
        handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
        if not handle:
            return False
        try:
            exit_code = ctypes.wintypes.DWORD()
            if not kernel32.GetExitCodeProcess(handle, ctypes.byref(exit_code)):
                return False
            return int(exit_code.value) == STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)

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
        runtime_diagnostics: dict[str, Any] | None = None,
    ) -> bool:
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
            "runtime": dict(runtime_diagnostics or {}),
        }
        try:
            self.path.parent.mkdir(parents=True, exist_ok=True)
            temp_path = self.path.with_suffix(self.path.suffix + ".tmp")
            with temp_path.open("w", encoding="utf-8") as handle:
                json.dump(state, handle, indent=2, sort_keys=True)
                handle.write("\n")
            temp_path.replace(self.path)
            return True
        except OSError:
            return False

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
            process_name = entry.szExeFile.lower()
            if process_name in CODEX_PROCESS_NAMES or process_name == WINDOWS_CHATGPT_PROCESS_NAME:
                pid = int(entry.th32ProcessID)
                if windows_pid_is_codex(pid, process_name):
                    pids.add(pid)
            has_entry = kernel32.Process32NextW(snapshot, ctypes.byref(entry))
    finally:
        kernel32.CloseHandle(snapshot)
    return pids


def windows_process_path(pid: int) -> str:
    if platform.system() != "Windows" or pid <= 0:
        return ""

    try:
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.OpenProcess.argtypes = [
            ctypes.wintypes.DWORD,
            ctypes.wintypes.BOOL,
            ctypes.wintypes.DWORD,
        ]
        kernel32.OpenProcess.restype = ctypes.wintypes.HANDLE
        kernel32.QueryFullProcessImageNameW.argtypes = [
            ctypes.wintypes.HANDLE,
            ctypes.wintypes.DWORD,
            ctypes.wintypes.LPWSTR,
            ctypes.POINTER(ctypes.wintypes.DWORD),
        ]
        kernel32.QueryFullProcessImageNameW.restype = ctypes.wintypes.BOOL
        kernel32.CloseHandle.argtypes = [ctypes.wintypes.HANDLE]
        kernel32.CloseHandle.restype = ctypes.wintypes.BOOL
    except (OSError, AttributeError):
        return ""

    PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
    handle = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ""
    try:
        buffer = ctypes.create_unicode_buffer(32_768)
        size = ctypes.wintypes.DWORD(len(buffer))
        if kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
            return buffer.value
    finally:
        kernel32.CloseHandle(handle)
    return ""


def windows_path_process_name(executable_path: str | None) -> str:
    if not executable_path:
        return ""
    return re.split(r"[\\/]", executable_path.strip())[-1].lower()


def windows_codex_host_build_from_path(executable_path: str | None) -> str | None:
    if not executable_path:
        return None
    for part in re.split(r"[\\/]", executable_path):
        match = WINDOWS_CODEX_PACKAGE_BUILD_RE.match(part)
        if match:
            return match.group(1)
    return None


def detect_windows_codex_host_build() -> str | None:
    if platform.system() != "Windows":
        return None
    builds = {
        build
        for pid in windows_codex_pids()
        if (build := windows_codex_host_build_from_path(windows_process_path(pid)))
    }
    if not builds:
        return None
    return max(builds, key=lambda value: tuple(parse_int(part) for part in value.split(".")))


def is_codex_windows_process(process_name: str | None, executable_path: str | None = None) -> bool:
    normalized_name = (process_name or "").strip().lower()
    if normalized_name in CODEX_PROCESS_NAMES:
        return True
    if normalized_name != WINDOWS_CHATGPT_PROCESS_NAME or not executable_path:
        return False
    path_parts = [part.lower() for part in re.split(r"[\\/]", executable_path) if part]
    return any(part.startswith(WINDOWS_CODEX_PACKAGE_PREFIX) for part in path_parts)


def windows_pid_is_codex(pid: int, process_name: str | None = None) -> bool:
    normalized_name = (process_name or "").strip().lower()
    if normalized_name in CODEX_PROCESS_NAMES:
        return True
    executable_path = windows_process_path(pid)
    if not normalized_name:
        normalized_name = windows_path_process_name(executable_path)
    return is_codex_windows_process(normalized_name, executable_path)


def windows_process_name(pid: int) -> str:
    return windows_path_process_name(windows_process_path(pid))


def windows_foreground_is_codex() -> bool:
    if platform.system() != "Windows":
        return False

    user32 = ctypes.WinDLL("user32", use_last_error=True)
    hwnd = user32.GetForegroundWindow()
    if not hwnd:
        return False
    pid = ctypes.wintypes.DWORD()
    user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    return windows_pid_is_codex(int(pid.value))


def windows_mouse_button_activity() -> bool:
    """Return whether a primary mouse button is down or was pressed since polling."""
    if platform.system() != "Windows":
        return False
    try:
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        return any(
            int(user32.GetAsyncKeyState(button)) & 0x8001
            for button in (0x01, 0x02, 0x04)
        )
    except (AttributeError, OSError):
        return False


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
        if windows_pid_is_codex(int(pid.value)):
            matches["found"] = True
            return False
        return True

    user32.EnumWindows(EnumWindowsProc(callback), 0)
    return matches["found"]


class ContextMenuWindow:
    def __init__(self, app: "OverlayApp", rows: list[MenuRow]) -> None:
        self.app = app
        self.rows = rows
        self.window = tk.Toplevel(app.root)
        self.window.withdraw()
        self.window.overrideredirect(True)
        self.window.configure(bg=MENU_BORDER)
        self.window.transient(app.root)
        self.window.bind("<Escape>", self._dismiss)
        self.window.bind("<FocusOut>", self._schedule_focus_dismiss)
        self.window.bind("<FocusIn>", self._cancel_focus_dismiss)
        self.window.bind("<Button-1>", self._dismiss_if_outside, add="+")
        self.window.bind("<Button-3>", self._dismiss_if_outside, add="+")
        self.window.bind("<Button-2>", self._dismiss_if_outside, add="+")
        self.window.bind("<ButtonRelease-1>", self._dismiss_if_outside, add="+")
        self.window.bind("<ButtonRelease-2>", self._dismiss_if_outside, add="+")
        self.window.bind("<ButtonRelease-3>", self._dismiss_if_outside, add="+")
        try:
            self.window.attributes("-topmost", True)
        except tk.TclError:
            pass

        self.canvas = tk.Canvas(self.window, bg=MENU_BG, bd=0, highlightthickness=0)
        self.scrollbar = tk.Scrollbar(self.window, orient="vertical", command=self.canvas.yview)
        self.canvas.configure(yscrollcommand=self.scrollbar.set)
        self.frame = tk.Frame(self.canvas, bg=MENU_BG, padx=0, pady=2)
        self.canvas_window = self.canvas.create_window((0, 0), window=self.frame, anchor="nw")
        self.canvas.pack(side="left", padx=1, pady=1)
        self.canvas.bind("<Configure>", self._resize_canvas_window)
        self.frame.bind("<Configure>", self._update_scroll_region)
        self.closed = False
        self.focus_dismiss_enabled = False
        self._focus_arm_after_id: Any = None
        self._focus_dismiss_after_id: Any = None
        self._outside_watch_after_id: Any = None
        self._focus_loss_confirmations = 0
        self.scrollable = False
        self.row_labels: list[tk.Label] = []
        self._bind_scroll_events(self.window)
        self._bind_scroll_events(self.canvas)
        self._bind_scroll_events(self.frame)
        self.window.bind("<Down>", lambda _event: self._scroll_keyboard(3))
        self.window.bind("<Up>", lambda _event: self._scroll_keyboard(-3))
        self.window.bind("<Next>", lambda _event: self._scroll_keyboard(8))
        self.window.bind("<Prior>", lambda _event: self._scroll_keyboard(-8))
        self.window.bind("<Home>", lambda _event: self._scroll_to_edge(0.0))
        self.window.bind("<End>", lambda _event: self._scroll_to_edge(1.0))
        self._render_rows()

    def _render_rows(self) -> None:
        for row in self.rows:
            if row.kind == "separator":
                separator = tk.Frame(self.frame, bg=MENU_SEPARATOR, height=1)
                separator.pack(fill="x", padx=MENU_ROW_PADX, pady=MENU_SEPARATOR_PADY)
                self._bind_scroll_events(separator)
                continue

            fg = MENU_TEXT if row.clickable else MENU_MUTED
            label = tk.Label(
                self.frame,
                text=row.label,
                fg=fg,
                bg=MENU_BG if row.clickable else MENU_DISABLED_BG,
                font=("Segoe UI", 9),
                anchor="w",
                justify="left",
                padx=MENU_ROW_PADX,
                pady=MENU_ROW_PADY,
                wraplength=MENU_MAX_WRAP_LENGTH,
            )
            label.pack(fill="x")
            self.row_labels.append(label)
            self._bind_scroll_events(label)
            if row.clickable:
                label.configure(cursor="hand2")
                label.bind("<Enter>", lambda _event, item=label: item.configure(bg=MENU_HOVER))
                label.bind("<Leave>", lambda _event, item=label: item.configure(bg=MENU_BG))
                label.bind("<Button-1>", lambda _event, item=row: self._invoke(item))

    def show(self, x: int, y: int) -> None:
        bounds = self.app.popup_bounds((x, y))
        max_width, max_height = popup_max_size(bounds)
        max_canvas_height = max(1, max_height - 2)
        max_canvas_width = max(1, max_width - 2)
        self._configure_wrap_length(max_canvas_width)
        self.window.update_idletasks()

        requested_width = max(1, int(self.frame.winfo_reqwidth()))
        requested_height = max(1, int(self.frame.winfo_reqheight()))
        scrollable = requested_height > max_canvas_height
        scrollbar_width = MENU_SCROLLBAR_WIDTH if scrollable else 0
        canvas_width = min(requested_width, max(1, max_width - scrollbar_width - 2))
        self._configure_wrap_length(canvas_width)
        self.canvas.itemconfigure(self.canvas_window, width=canvas_width)
        self.window.update_idletasks()

        requested_width = max(1, int(self.frame.winfo_reqwidth()))
        requested_height = max(1, int(self.frame.winfo_reqheight()))
        self.scrollable = requested_height > max_canvas_height
        scrollbar_width = MENU_SCROLLBAR_WIDTH if self.scrollable else 0
        canvas_width = min(requested_width, max(1, max_width - scrollbar_width - 2))
        self._configure_wrap_length(canvas_width)
        self.canvas.itemconfigure(self.canvas_window, width=canvas_width)
        self.window.update_idletasks()

        requested_height = max(1, int(self.frame.winfo_reqheight()))
        self.scrollable = requested_height > max_canvas_height
        if self.scrollable and scrollbar_width == 0:
            canvas_width = min(requested_width, max(1, max_width - MENU_SCROLLBAR_WIDTH - 2))
            self._configure_wrap_length(canvas_width)
            self.canvas.itemconfigure(self.canvas_window, width=canvas_width)
            self.window.update_idletasks()
            requested_height = max(1, int(self.frame.winfo_reqheight()))
            self.scrollable = requested_height > max_canvas_height
        canvas_height = min(requested_height, max_canvas_height)
        self.canvas.configure(
            width=canvas_width,
            height=canvas_height,
            scrollregion=(0, 0, canvas_width, requested_height),
        )
        if self.scrollable:
            self.scrollbar.pack(side="right", fill="y", padx=(0, 1), pady=1)
        else:
            self.scrollbar.pack_forget()

        self.window.update_idletasks()
        width = max(1, int(self.window.winfo_reqwidth()))
        height = max(1, int(self.window.winfo_reqheight()))
        menu_x, menu_y = clamp_popup_position([x, y], bounds, (width, height))
        self.window.geometry(f"+{menu_x}+{menu_y}")
        self.window.deiconify()
        self.window.lift()
        try:
            self.window.attributes("-topmost", True)
        except tk.TclError:
            pass
        try:
            self.window.grab_set()
        except tk.TclError:
            pass
        try:
            self.window.focus_force()
        except tk.TclError:
            pass
        self._focus_arm_after_id = self.window.after(
            MENU_FOCUS_ARM_DELAY_MS,
            self._enable_focus_dismiss,
        )
        self._start_outside_watch()

    def close(self) -> None:
        if self.closed:
            return
        self.closed = True
        self._cancel_owned_callbacks()
        try:
            if self.window.grab_current() == self.window:
                self.window.grab_release()
        except tk.TclError:
            pass
        try:
            self.window.destroy()
        except tk.TclError:
            pass

    def related_popup_anchor(self) -> tuple[int, int]:
        try:
            return (
                int(self.window.winfo_rootx()) + int(self.window.winfo_width()) + MENU_RELATED_POPUP_GAP,
                int(self.window.winfo_rooty()),
            )
        except tk.TclError:
            return (0, 0)

    def _configure_wrap_length(self, max_width: int) -> None:
        wraplength = min(MENU_MAX_WRAP_LENGTH, max(MENU_MIN_WRAP_LENGTH, max_width - 32))
        for label in self.row_labels:
            label.configure(wraplength=wraplength)

    def _bind_scroll_events(self, widget: tk.Widget) -> None:
        widget.bind("<MouseWheel>", self._scroll_mousewheel)
        widget.bind("<Button-4>", lambda _event: self._scroll_keyboard(-3))
        widget.bind("<Button-5>", lambda _event: self._scroll_keyboard(3))

    def _resize_canvas_window(self, event: tk.Event) -> None:
        self.canvas.itemconfigure(self.canvas_window, width=max(1, int(event.width)))

    def _update_scroll_region(self, _event: tk.Event | None = None) -> None:
        bbox = self.canvas.bbox("all")
        if bbox is not None:
            self.canvas.configure(scrollregion=bbox)

    def _scroll_mousewheel(self, event: tk.Event) -> str | None:
        if not self.scrollable:
            return None
        delta = int(getattr(event, "delta", 0))
        units = -1 if delta > 0 else 1
        if abs(delta) >= 120:
            units = -int(delta / 120)
        self.canvas.yview_scroll(units, "units")
        return "break"

    def _scroll_keyboard(self, units: int) -> str:
        if self.scrollable:
            self.canvas.yview_scroll(units, "units")
        return "break"

    def _scroll_to_edge(self, fraction: float) -> str:
        if self.scrollable:
            self.canvas.yview_moveto(fraction)
        return "break"

    def _enable_focus_dismiss(self) -> None:
        self._focus_arm_after_id = None
        if self.closed:
            return
        self.focus_dismiss_enabled = True

    def _invoke(self, row: MenuRow) -> str:
        if not self.closed:
            row.invoke()
        return "break"

    def _dismiss(self, _event: tk.Event | None = None) -> str:
        self.app.finish_menu_interaction("escape")
        return "break"

    def _schedule_focus_dismiss(self, _event: tk.Event | None = None) -> None:
        if self.closed or not self.focus_dismiss_enabled:
            return
        self._cancel_focus_dismiss()
        self._focus_dismiss_after_id = self.window.after(
            MENU_FOCUS_LOSS_DEBOUNCE_MS,
            self._dismiss_if_focus_lost,
        )

    def _cancel_focus_dismiss(self, _event: tk.Event | None = None) -> None:
        after_id = self._focus_dismiss_after_id
        self._focus_dismiss_after_id = None
        self._focus_loss_confirmations = 0
        if after_id is None:
            return
        try:
            self.window.after_cancel(after_id)
        except (AttributeError, tk.TclError):
            pass

    def _cancel_owned_callbacks(self) -> None:
        after_ids = (
            getattr(self, "_focus_arm_after_id", None),
            getattr(self, "_focus_dismiss_after_id", None),
            getattr(self, "_outside_watch_after_id", None),
        )
        self._focus_arm_after_id = None
        self._focus_dismiss_after_id = None
        self._outside_watch_after_id = None
        self._focus_loss_confirmations = 0
        for after_id in after_ids:
            if after_id is None:
                continue
            try:
                self.window.after_cancel(after_id)
            except (AttributeError, tk.TclError):
                pass

    def _dismiss_if_focus_lost(self) -> None:
        self._focus_dismiss_after_id = None
        if self.closed:
            return
        try:
            focused = self.window.focus_displayof()
        except tk.TclError:
            focused = None
        if focused is not None and self._is_descendant(focused):
            self._focus_loss_confirmations = 0
            return
        self._focus_loss_confirmations += 1
        if self._focus_loss_confirmations < MENU_FOCUS_LOSS_CONFIRMATIONS:
            self._focus_dismiss_after_id = self.window.after(
                MENU_FOCUS_LOSS_DEBOUNCE_MS,
                self._dismiss_if_focus_lost,
            )
            return
        self.app.finish_menu_interaction("focus_loss")

    def _dismiss_if_outside(self, event: tk.Event) -> str | None:
        if not self._point_inside(int(event.x_root), int(event.y_root)):
            self.app.finish_menu_interaction("outside_click")
            return "break"
        return None

    def _start_outside_watch(self) -> None:
        if self.closed or platform.system() != "Windows":
            return
        # Drain the right-click that opened the popup before watching for the
        # next press. This observes mouse buttons only; it never installs a
        # system hook or records input.
        windows_mouse_button_activity()
        self._outside_watch_after_id = self.window.after(
            MENU_OUTSIDE_WATCH_INTERVAL_MS,
            self._watch_for_outside_click,
        )

    def _watch_for_outside_click(self) -> None:
        self._outside_watch_after_id = None
        if self.closed:
            return
        try:
            pointer_x = int(self.window.winfo_pointerx())
            pointer_y = int(self.window.winfo_pointery())
        except (AttributeError, tk.TclError, TypeError, ValueError):
            pointer_x = pointer_y = 0
        if windows_mouse_button_activity() and not self._point_inside(pointer_x, pointer_y):
            self.app.finish_menu_interaction("outside_click")
            return
        try:
            self._outside_watch_after_id = self.window.after(
                MENU_OUTSIDE_WATCH_INTERVAL_MS,
                self._watch_for_outside_click,
            )
        except (AttributeError, tk.TclError):
            self._outside_watch_after_id = None

    def _point_inside(self, x: int, y: int) -> bool:
        try:
            left = int(self.window.winfo_rootx())
            top = int(self.window.winfo_rooty())
            width = int(self.window.winfo_width())
            height = int(self.window.winfo_height())
        except tk.TclError:
            return False
        return left <= x < left + width and top <= y < top + height

    def _is_descendant(self, widget: tk.Widget) -> bool:
        while widget is not None:
            if widget == self.window:
                return True
            widget = widget.master
        return False


class OverlayApp:
    def __init__(self) -> None:
        self.settings_path = settings_path()
        self.settings = load_settings(self.settings_path)
        self.reader = RateLogReader()
        self.process_backend = ProcessBackend()
        self._installed_host_build = detect_windows_codex_host_build()
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
        self.menu_active = False
        self.menu_window: ContextMenuWindow | None = None
        self.menu_anchor: tuple[int, int] | None = None
        self.needs_render_after_drag = False
        self.needs_render_after_menu = False
        self.needs_visibility_after_menu = False
        self._post_menu_visibility_after_id: Any = None
        self._post_drag_visibility_after_id: Any = None
        self._last_menu_close_reason: str | None = None
        self.force_rescan = True
        self._startup_complete = False
        self._last_overlay_size: tuple[int, int] | None = None
        self._last_render_signature: tuple[Any, ...] | None = None
        self._force_render_requested = False
        self._stable_monitor_fingerprint: tuple[tuple[Any, ...], ...] | None = None
        self._stable_monitors: tuple[MonitorWorkArea, ...] = ()
        self._last_good_monitors: tuple[MonitorWorkArea, ...] = ()
        self._pending_monitor_fingerprint: tuple[tuple[Any, ...], ...] | None = None
        self._pending_monitor_first_seen_at = 0.0
        self._pending_monitor_last_seen_at = 0.0
        self._pending_monitor_sample_count = 0
        self._native_display_dirty = False
        self._native_display_dirty_at = 0.0
        self._display_reconcile_deferred = False
        self._display_verification_after_id: Any = None
        self._display_verification_due_at: float | None = None
        self._pending_drag_position: list[int] | None = None
        self._drag_monitors: tuple[MonitorWorkArea, ...] = ()
        self._drag_window_size: tuple[int, int] | None = None
        self._overlay_is_shown = False
        self._topmost_enabled: bool | None = None
        self._cached_should_show: bool | None = None
        self._last_visibility_check_at = 0.0
        self._next_log_poll_at = 0.0
        self._next_state_write_at = 0.0
        self._next_display_poll_at = 0.0
        self._last_display_scan_succeeded = True
        self._refresh_after_id: Any = None
        self._native_display_observer_error: str | None = None
        self._last_ui_error: str | None = None
        self._last_settings_error: str | None = None
        self._quitting = False

        self.root = tk.Tk()
        self.root.report_callback_exception = self._report_tk_callback_exception
        self.root.withdraw()
        self.root.title(APP_NAME)
        self.root.protocol("WM_DELETE_WINDOW", self.quit)
        self.root.overrideredirect(True)
        self.root.attributes("-topmost", True)
        self._topmost_enabled = True
        try:
            self.root.attributes("-alpha", self.settings["opacity"])
        except tk.TclError:
            pass

        self.root.configure(bg=COLOR_BORDER)
        self.container = tk.Frame(self.root, bg=COLOR_BG, padx=8, pady=5)
        self.container.pack(padx=1, pady=1)
        self.labels: list[tk.Label] = []

        self._bind_window_events(self.root)
        self._bind_window_events(self.container)
        self._display_observer = WindowsDisplayChangeObserver(self.root)

        self.refresh(force=True, schedule_next=False, apply_visibility=False)
        startup_monitors = windows_monitor_work_areas()
        if startup_monitors:
            self._last_good_monitors = tuple(startup_monitors)
            self._observe_display_sample(
                monitor_topology_fingerprint(startup_monitors),
                time.monotonic(),
            )
        self.reconcile_overlay_position(
            "startup",
            candidate=self.settings.get("position"),
            persist=False,
            monitors=startup_monitors,
        )
        self._startup_complete = True
        self._last_overlay_size = self.current_window_size()
        self.update_visibility(force=True)
        self._schedule_refresh()

    def _bind_window_events(self, widget: tk.Widget) -> None:
        widget.bind("<ButtonPress-1>", self.start_drag)
        widget.bind("<B1-Motion>", self.drag)
        widget.bind("<ButtonRelease-1>", self.end_drag)
        widget.bind("<ButtonRelease-3>", self.show_menu)
        widget.bind("<ButtonRelease-2>", self.show_menu)

    def _position_window(self) -> None:
        self.reconcile_overlay_position(
            "position",
            candidate=self.settings.get("position"),
            persist=True,
        )

    def screen_bounds(self) -> tuple[int, int, int, int]:
        bounds = windows_virtual_screen_bounds()
        if bounds is not None:
            return bounds
        return (0, 0, int(self.root.winfo_screenwidth()), int(self.root.winfo_screenheight()))

    def popup_bounds(self, anchor: tuple[int, int]) -> tuple[int, int, int, int]:
        return windows_monitor_work_area(anchor) or self.screen_bounds()

    def _cached_monitor_snapshot(self) -> tuple[MonitorWorkArea, ...]:
        stable = tuple(getattr(self, "_stable_monitors", ()))
        if stable:
            return stable
        return tuple(getattr(self, "_last_good_monitors", ()))

    def _remember_monitor_snapshot(
        self,
        monitors: tuple[MonitorWorkArea, ...] | list[MonitorWorkArea],
    ) -> tuple[MonitorWorkArea, ...]:
        snapshot = tuple(monitors)
        if snapshot:
            self._last_good_monitors = snapshot
        return snapshot

    def current_window_size(self) -> tuple[int, int]:
        try:
            self.root.update_idletasks()
        except (AttributeError, tk.TclError):
            pass
        widths = [DEFAULT_OVERLAY_WIDTH]
        heights = [DEFAULT_OVERLAY_HEIGHT]
        for method_name, values in (
            ("winfo_width", widths),
            ("winfo_reqwidth", widths),
            ("winfo_height", heights),
            ("winfo_reqheight", heights),
        ):
            try:
                values.append(int(getattr(self.root, method_name)()))
            except (AttributeError, TypeError, ValueError, tk.TclError):
                pass
        return (max(widths), max(heights))

    def current_window_position(self) -> list[int]:
        try:
            return [int(self.root.winfo_x()), int(self.root.winfo_y())]
        except (AttributeError, TypeError, ValueError, tk.TclError):
            saved = self.settings.get("position")
            if (
                isinstance(saved, list)
                and len(saved) == 2
                and all(isinstance(item, int) for item in saved)
            ):
                return [int(saved[0]), int(saved[1])]
            return [0, 0]

    def _apply_root_position(self, position: list[int], flush: bool = True) -> bool:
        try:
            self.root.geometry(f"{int(position[0]):+d}{int(position[1]):+d}")
            if flush:
                self.root.update_idletasks()
            return True
        except (AttributeError, TypeError, ValueError, tk.TclError):
            return False

    def reconcile_overlay_position(
        self,
        reason: str,
        candidate: Any = Ellipsis,
        persist: bool = False,
        monitors: tuple[MonitorWorkArea, ...] | list[MonitorWorkArea] | None = None,
    ) -> bool:
        if self.is_dragging and reason not in ("drag_release", "startup"):
            return False

        size = self.current_window_size()
        current = self.current_window_position()
        requested = current if candidate is Ellipsis else candidate
        active_monitors = (
            tuple(monitors)
            if monitors is not None
            else self._cached_monitor_snapshot() or windows_monitor_work_areas()
        )
        if active_monitors:
            self._remember_monitor_snapshot(active_monitors)
        fingerprint: tuple[tuple[Any, ...], ...] | None = None

        if active_monitors:
            fingerprint = monitor_topology_fingerprint(active_monitors)
            target = normalize_overlay_position_for_monitors(requested, active_monitors, size)
        else:
            # A transient empty Windows enumeration must never move or persist
            # an already-running window. Startup/reset retain the legacy bounds
            # as a non-persisting emergency placement path.
            if platform.system() == "Windows" and candidate is Ellipsis:
                return False
            target = normalize_overlay_position(requested, self.screen_bounds(), size)
            persist = False

        if target is None:
            return False
        moved = current != target
        if moved and not self._apply_root_position(target):
            return False

        applied = self.current_window_position() if moved else current
        if moved and applied != target:
            # Withdrawn Tk windows can defer native realization. The requested
            # geometry is retained, but persistence waits for a later check.
            applied = target
            verified = False
        elif active_monitors:
            verified = (
                normalize_overlay_position_for_monitors(applied, active_monitors, size) == applied
            )
        else:
            verified = True

        topology_is_stable = (
            fingerprint is not None
            and fingerprint == getattr(self, "_stable_monitor_fingerprint", None)
        )
        saved_position = self.settings.get("position")
        if (
            persist
            and topology_is_stable
            and verified
            and saved_position is not None
            and saved_position != applied
        ):
            self.settings["position"] = list(applied)
            self.save_settings()
        return moved

    def _consume_native_display_notifications(self, now: float) -> bool:
        observer = getattr(self, "_display_observer", None)
        if observer is not None and getattr(observer, "_hwnd", 0) == 0:
            try:
                observer.install_if_mapped()
            except (AttributeError, tk.TclError):
                pass
        if observer is not None:
            try:
                callback_error = observer.take_callback_error()
            except AttributeError:
                callback_error = None
            if callback_error is not None:
                self._native_display_observer_error = repr(callback_error)
        pending = observer.take_pending() if observer is not None else set()
        if not pending:
            return False
        self._native_display_dirty = True
        self._native_display_dirty_at = now
        return True

    def _reset_pending_display_sample(self) -> None:
        self._pending_monitor_fingerprint = None
        self._pending_monitor_first_seen_at = 0.0
        self._pending_monitor_last_seen_at = 0.0
        self._pending_monitor_sample_count = 0

    def _observe_display_sample(
        self,
        fingerprint: tuple[tuple[Any, ...], ...],
        now: float,
    ) -> None:
        if fingerprint != self._pending_monitor_fingerprint:
            self._pending_monitor_fingerprint = fingerprint
            self._pending_monitor_first_seen_at = now
            self._pending_monitor_last_seen_at = now
            self._pending_monitor_sample_count = 1
            return
        if now - self._pending_monitor_last_seen_at >= DISPLAY_TOPOLOGY_SAMPLE_SECONDS:
            self._pending_monitor_sample_count += 1
            self._pending_monitor_last_seen_at = now

    def _close_menu_for_display_change(self) -> bool:
        if not self.menu_active:
            return False
        render_pending = self.needs_render_after_menu
        self.finish_menu_interaction(
            "display_change",
            apply_deferred_render=False,
            schedule_visibility=False,
        )
        return render_pending

    def _apply_stable_display_topology(
        self,
        monitors: tuple[MonitorWorkArea, ...],
        fingerprint: tuple[tuple[Any, ...], ...],
    ) -> None:
        topology_changed = fingerprint != self._stable_monitor_fingerprint
        render_pending = self._close_menu_for_display_change() if topology_changed else False
        self._stable_monitor_fingerprint = fingerprint
        self._stable_monitors = tuple(monitors)
        self._remember_monitor_snapshot(monitors)
        self._native_display_dirty = False
        self._display_reconcile_deferred = False
        self._reset_pending_display_sample()

        pending_drag_position = self._pending_drag_position
        self._pending_drag_position = None
        self.reconcile_overlay_position(
            "topology",
            candidate=pending_drag_position if pending_drag_position is not None else Ellipsis,
            persist=pending_drag_position is None,
            monitors=monitors,
        )
        if pending_drag_position is not None:
            final_position = self.current_window_position()
            verified_position = normalize_overlay_position_for_monitors(
                final_position,
                monitors,
                self.current_window_size(),
            )
            if verified_position == final_position and self.settings.get("position") != final_position:
                self.settings["position"] = final_position
                self.save_settings()
        if topology_changed or render_pending:
            self.request_render(force=True)
        self._schedule_display_verification()

    def _check_display_topology(
        self,
        now: float | None = None,
        monitors: tuple[MonitorWorkArea, ...] | list[MonitorWorkArea] | None = None,
    ) -> bool:
        observed_at = time.monotonic() if now is None else now
        self._consume_native_display_notifications(observed_at)
        observed_monitors = (
            windows_monitor_work_areas()
            if monitors is None
            else tuple(monitors)
        )
        self._last_display_scan_succeeded = bool(observed_monitors)
        if not observed_monitors:
            return False
        self._remember_monitor_snapshot(observed_monitors)
        fingerprint = monitor_topology_fingerprint(observed_monitors)
        stable_fingerprint = self._stable_monitor_fingerprint
        needs_reconcile = (
            stable_fingerprint is None
            or fingerprint != stable_fingerprint
            or self._native_display_dirty
            or self._display_reconcile_deferred
        )
        if not needs_reconcile:
            self._reset_pending_display_sample()
            return False

        self._observe_display_sample(fingerprint, observed_at)
        sample_is_stable = (
            self._pending_monitor_sample_count >= 2
            and observed_at - self._pending_monitor_first_seen_at
            >= DISPLAY_TOPOLOGY_SAMPLE_SECONDS
        )
        native_events_are_quiet = (
            not self._native_display_dirty
            or observed_at - self._native_display_dirty_at
            >= DISPLAY_TOPOLOGY_DEBOUNCE_SECONDS
        )
        if not sample_is_stable or not native_events_are_quiet:
            return False
        if self.is_dragging:
            self._display_reconcile_deferred = True
            return False

        self._apply_stable_display_topology(tuple(observed_monitors), fingerprint)
        return True

    def _schedule_display_verification(self) -> None:
        self._display_verification_after_id = None
        self._display_verification_due_at = (
            time.monotonic() + DISPLAY_TOPOLOGY_VERIFY_MS / 1_000
        )

    def _verify_display_topology(self, now: float | None = None) -> None:
        self._display_verification_after_id = None
        self._display_verification_due_at = None
        observed_at = time.monotonic() if now is None else now
        monitors = windows_monitor_work_areas()
        self._last_display_scan_succeeded = bool(monitors)
        if not monitors:
            self._native_display_dirty = True
            self._native_display_dirty_at = observed_at
            return
        self._remember_monitor_snapshot(monitors)
        fingerprint = monitor_topology_fingerprint(monitors)
        if fingerprint != self._stable_monitor_fingerprint:
            self._observe_display_sample(fingerprint, observed_at)
            return
        if not self.is_dragging:
            self.reconcile_overlay_position(
                "topology_verification",
                persist=True,
                monitors=monitors,
            )

    def start_drag(self, event: tk.Event) -> None:
        if self.menu_active or getattr(self, "_quitting", False):
            return
        self._cancel_post_menu_visibility_reconcile()
        self._cancel_post_drag_visibility_reconcile()
        self.is_dragging = True
        self.drag_offset = (event.x_root - self.root.winfo_x(), event.y_root - self.root.winfo_y())
        now = time.monotonic()
        self._consume_native_display_notifications(now)
        topology_pending = (
            getattr(self, "_native_display_dirty", False)
            or getattr(self, "_pending_monitor_fingerprint", None) is not None
            or getattr(self, "_display_reconcile_deferred", False)
            or getattr(self, "_stable_monitor_fingerprint", None) is None
        )
        if topology_pending:
            fresh_monitors = windows_monitor_work_areas()
            self._check_display_topology(now=now, monitors=fresh_monitors)
            self._next_display_poll_at = self._display_followup_deadline(now)
            monitors = fresh_monitors or self._cached_monitor_snapshot()
        else:
            monitors = self._cached_monitor_snapshot() or windows_monitor_work_areas()
        self._drag_monitors = self._remember_monitor_snapshot(monitors) if monitors else ()
        self._drag_window_size = self.current_window_size()

    def drag(self, event: tk.Event) -> None:
        if self.drag_offset is None or not self.is_dragging:
            return
        offset_x, offset_y = self.drag_offset
        try:
            pointer_x = int(event.x_root)
            pointer_y = int(event.y_root)
        except (AttributeError, TypeError, ValueError):
            pointer_x = int(self.root.winfo_pointerx())
            pointer_y = int(self.root.winfo_pointery())
        proposed = [pointer_x - offset_x, pointer_y - offset_y]
        monitors = tuple(getattr(self, "_drag_monitors", ()))
        window_size = getattr(self, "_drag_window_size", None) or self.current_window_size()
        if monitors:
            position = normalize_overlay_position_for_monitors(
                proposed,
                monitors,
                window_size,
            )
        else:
            position = clamp_overlay_position(
                proposed,
                self.screen_bounds(),
                window_size,
            )
        if position is not None:
            self._apply_root_position(position, flush=False)

    def end_drag(self, _event: tk.Event) -> None:
        if not self.is_dragging:
            return
        self.drag_offset = None
        self.is_dragging = False
        now = time.monotonic()
        self._consume_native_display_notifications(now)
        monitors = windows_monitor_work_areas()
        self._last_display_scan_succeeded = bool(monitors)
        topology_is_transitioning = False
        if monitors:
            self._remember_monitor_snapshot(monitors)
            fingerprint = monitor_topology_fingerprint(monitors)
            self.reconcile_overlay_position(
                "drag_release",
                candidate=self.current_window_position(),
                persist=False,
                monitors=monitors,
            )
            final_position = self.current_window_position()
            topology_is_transitioning = (
                self._display_reconcile_deferred
                or self._native_display_dirty
                or fingerprint != self._stable_monitor_fingerprint
            )
            if topology_is_transitioning:
                self._pending_drag_position = final_position
                self._observe_display_sample(fingerprint, now)
            elif self.settings.get("position") != final_position:
                self.settings["position"] = final_position
                self.save_settings()
        elif platform.system() != "Windows":
            final_position = clamp_overlay_position(
                self.current_window_position(),
                self.screen_bounds(),
                self.current_window_size(),
            )
            if self.settings.get("position") != final_position:
                self.settings["position"] = final_position
                self.save_settings()
        else:
            # Preserve the user's completed drag in memory, but wait for a
            # valid monitor snapshot before moving or writing it to settings.
            self._pending_drag_position = self.current_window_position()
            self._native_display_dirty = True
            self._native_display_dirty_at = now
            topology_is_transitioning = True
        self._next_display_poll_at = (
            self._display_followup_deadline(now)
            if topology_is_transitioning
            else now + DISPLAY_TOPOLOGY_POLL_INTERVAL_SECONDS
        )
        self._drag_monitors = ()
        self._drag_window_size = None
        if self.needs_render_after_drag:
            self.request_render(force=True)
        self._schedule_post_drag_visibility_reconcile()

    def run(self) -> None:
        try:
            self.root.mainloop()
        finally:
            observer = getattr(self, "_display_observer", None)
            if observer is not None:
                observer.close_before_root_destroy()
            self.runtime_state.delete()

    def _schedule_refresh(self) -> None:
        old_identifier = getattr(self, "_refresh_after_id", None)
        if old_identifier is not None:
            try:
                self.root.after_cancel(old_identifier)
            except (AttributeError, tk.TclError):
                pass
        if getattr(self, "_quitting", False):
            self._refresh_after_id = None
            return

        delay_ms = (
            POLL_INTERVAL_MS
            if getattr(self, "_overlay_is_shown", False)
            else HIDDEN_POLL_INTERVAL_MS
        )
        try:
            self._refresh_after_id = self.root.after(delay_ms, self._scheduled_refresh)
        except (AttributeError, tk.TclError):
            self._refresh_after_id = None

    def _scheduled_refresh(self) -> None:
        self._refresh_after_id = None
        self.refresh()

    def _visibility_should_show(self, now: float, force: bool = False) -> bool:
        mode = normalize_visibility_mode(self.settings.get("visibility_mode"))
        if mode == "always":
            self._cached_should_show = True
            self._last_visibility_check_at = now
            return True

        cached = getattr(self, "_cached_should_show", None)
        last_check_at = getattr(self, "_last_visibility_check_at", 0.0)
        if mode == "process":
            interval = PROCESS_VISIBILITY_POLL_INTERVAL_SECONDS
        elif getattr(self, "_overlay_is_shown", False):
            interval = POLL_INTERVAL_MS / 1_000
        else:
            interval = HIDDEN_POLL_INTERVAL_MS / 1_000

        if force or cached is None or now - last_check_at >= interval:
            cached = self.process_backend.should_show(mode)
            self._cached_should_show = bool(cached)
            self._last_visibility_check_at = now
        return bool(cached)

    def _semantic_state_signature(self) -> tuple[Any, ...]:
        counter = self.token_counter
        return (
            self.snapshot,
            self.detected_model,
            self.counter_reset_model,
            counter.reset_at,
            counter.totals,
            counter.short_context_totals,
            counter.long_context_totals,
            counter.long_context_request_count,
            counter.last_update_at,
            len(counter.seen_events),
            getattr(self, "_last_menu_close_reason", None),
            getattr(self, "_last_ui_error", None),
            getattr(self, "_last_settings_error", None),
        )

    def _refresh_data(self, force_rescan: bool, now: float) -> bool:
        previous_signature = self._semantic_state_signature()
        self.refresh_detected_model(force=force_rescan)
        batch = self.reader.read_updates(force_rescan=force_rescan, now=now)
        if batch.snapshot is not None:
            self.snapshot = batch.snapshot
        self.token_counter.add_events(batch.token_events)
        return self._semantic_state_signature() != previous_signature

    def _runtime_diagnostics(self) -> dict[str, Any]:
        return {
            "overlay_shown": bool(getattr(self, "_overlay_is_shown", False)),
            "menu_active": bool(getattr(self, "menu_active", False)),
            "drag_active": bool(getattr(self, "is_dragging", False)),
            "visibility_mode": normalize_visibility_mode(
                getattr(self, "settings", {}).get("visibility_mode")
            ),
            "last_menu_close_reason": getattr(self, "_last_menu_close_reason", None),
            "last_ui_error": getattr(self, "_last_ui_error", None),
            "last_settings_error": getattr(self, "_last_settings_error", None),
            "installed_host_build": getattr(self, "_installed_host_build", None),
        }

    def _report_tk_callback_exception(
        self,
        exception_type: type[BaseException],
        exception_value: BaseException,
        _traceback: Any,
    ) -> None:
        if not isinstance(exception_value, exception_type):
            exception_value = exception_type(str(exception_value))
        self._last_ui_error = diagnostic_error_text(exception_value)
        if getattr(self, "_quitting", False):
            return
        try:
            self._write_runtime_state()
        except Exception:
            pass

    def _write_runtime_state(self, now: float | None = None) -> bool:
        observed_at = time.monotonic() if now is None else now
        written = self.runtime_state.write(
            self.snapshot,
            self.token_counter,
            self.current_api_cost_estimate(),
            self._runtime_diagnostics(),
        )
        if written is not False:
            self._next_state_write_at = (
                observed_at + RUNTIME_STATE_WRITE_INTERVAL_SECONDS
            )
            return True
        return False

    def _check_display_topology_if_due(
        self,
        now: float,
        force: bool = False,
        monitors: tuple[MonitorWorkArea, ...] | list[MonitorWorkArea] | None = None,
    ) -> bool:
        native_notification = self._consume_native_display_notifications(now)
        verification_due_at = getattr(self, "_display_verification_due_at", None)
        if verification_due_at is not None and now >= verification_due_at:
            if monitors is None and not force and not native_notification:
                self._verify_display_topology(now=now)
                self._next_display_poll_at = self._display_followup_deadline(now)
                return False
            self._display_verification_after_id = None
            self._display_verification_due_at = None

        if (
            monitors is None
            and not force
            and not native_notification
            and now < getattr(self, "_next_display_poll_at", 0.0)
        ):
            return False

        if monitors is None:
            changed = self._check_display_topology(now=now)
        else:
            changed = self._check_display_topology(now=now, monitors=monitors)
        self._next_display_poll_at = self._display_followup_deadline(now)
        return changed

    def _display_followup_deadline(self, now: float) -> float:
        if not getattr(self, "_last_display_scan_succeeded", True):
            retry_seconds = (
                DISPLAY_TOPOLOGY_RETRY_SECONDS
                if platform.system() == "Windows"
                else DISPLAY_TOPOLOGY_POLL_INTERVAL_SECONDS
            )
            return now + retry_seconds

        pending_fingerprint = getattr(self, "_pending_monitor_fingerprint", None)
        display_dirty = getattr(self, "_native_display_dirty", False)
        reconcile_deferred = getattr(self, "_display_reconcile_deferred", False)
        if getattr(self, "is_dragging", False) and reconcile_deferred:
            return now + DISPLAY_TOPOLOGY_POLL_INTERVAL_SECONDS

        if pending_fingerprint is not None:
            sample_count = getattr(self, "_pending_monitor_sample_count", 0)
            if sample_count < 2:
                last_sample_at = getattr(self, "_pending_monitor_last_seen_at", now)
                return max(now, last_sample_at + DISPLAY_TOPOLOGY_SAMPLE_SECONDS)
            if display_dirty:
                dirty_at = getattr(self, "_native_display_dirty_at", now)
                return max(now, dirty_at + DISPLAY_TOPOLOGY_DEBOUNCE_SECONDS)
            return now + DISPLAY_TOPOLOGY_SAMPLE_SECONDS

        if (
            display_dirty
            or reconcile_deferred
            or getattr(self, "_stable_monitor_fingerprint", None) is None
        ):
            return now + DISPLAY_TOPOLOGY_RETRY_SECONDS
        return now + DISPLAY_TOPOLOGY_POLL_INTERVAL_SECONDS

    def refresh(
        self,
        force: bool = False,
        schedule_next: bool = True,
        apply_visibility: bool = True,
    ) -> None:
        now = time.monotonic()
        was_shown = getattr(self, "_overlay_is_shown", False)
        if apply_visibility:
            if self._visibility_interaction_active():
                should_show = was_shown
            else:
                should_show = self._visibility_should_show(now, force=force)
        else:
            should_show = True
        becoming_visible = apply_visibility and should_show and not was_shown
        pre_show_monitors = (
            tuple(windows_monitor_work_areas())
            if becoming_visible
            else None
        )

        force_rescan = bool(force or self.force_rescan)
        log_poll_due = (
            force_rescan
            or becoming_visible
            or now >= getattr(self, "_next_log_poll_at", 0.0)
        )
        data_changed = False
        if log_poll_due:
            data_changed = self._refresh_data(force_rescan=force_rescan, now=now)
            self.force_rescan = False
            log_interval = (
                POLL_INTERVAL_MS / 1_000
                if should_show
                else HIDDEN_LOG_POLL_INTERVAL_SECONDS
            )
            self._next_log_poll_at = now + log_interval

        if should_show:
            self.request_render()
        if self._startup_complete:
            self._check_display_topology_if_due(
                now,
                force=force,
                monitors=pre_show_monitors,
            )
        if apply_visibility:
            self.update_visibility(
                should_show=should_show,
                monitors=pre_show_monitors,
            )
        if force or data_changed or now >= getattr(self, "_next_state_write_at", 0.0):
            self._write_runtime_state(now)
        if schedule_next:
            self._schedule_refresh()

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
        return estimate_api_cost(
            self.token_counter.totals,
            self.detected_model,
            self.counter_reset_model,
            short_context_usage=self.token_counter.short_context_totals,
            long_context_usage=self.token_counter.long_context_totals,
            long_context_request_count=self.token_counter.long_context_request_count,
        )

    def _render_model(
        self,
    ) -> tuple[tuple[DisplayWidget, ...], str, tuple[Any, ...]]:
        widgets = tuple(self.display_widgets())
        layout_mode = self.settings.get("layout_mode", DEFAULT_LAYOUT_MODE)
        return widgets, layout_mode, (widgets, layout_mode)

    def request_render(self, force: bool = False) -> None:
        if not force and self.menu_active:
            if not hasattr(self, "_last_render_signature"):
                self.needs_render_after_menu = True
            else:
                _widgets, _layout_mode, signature = self._render_model()
                self.needs_render_after_menu = (
                    signature != self._last_render_signature
                )
            return
        if not force and self.is_dragging:
            if not hasattr(self, "_last_render_signature"):
                self.needs_render_after_drag = True
            else:
                _widgets, _layout_mode, signature = self._render_model()
                self.needs_render_after_drag = (
                    signature != self._last_render_signature
                )
            return
        self.needs_render_after_menu = False
        self.needs_render_after_drag = False
        if force:
            self._force_render_requested = True
        self.render()

    def render(self) -> None:
        force_render = bool(getattr(self, "_force_render_requested", False))
        self._force_render_requested = False
        widgets, layout_mode, render_signature = self._render_model()
        if (
            not force_render
            and render_signature == getattr(self, "_last_render_signature", None)
        ):
            return

        previous_size = self._last_overlay_size
        for label in self.labels:
            label.destroy()
        self.labels.clear()

        positions = layout_positions(len(widgets), layout_mode)
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
        self._last_render_signature = render_signature
        rendered_size = self.current_window_size()
        self._last_overlay_size = rendered_size
        if self._startup_complete and previous_size is not None and rendered_size != previous_size:
            topology_pending = (
                getattr(self, "_pending_monitor_fingerprint", None) is not None
                or getattr(self, "_native_display_dirty", False)
                or getattr(self, "_display_reconcile_deferred", False)
            )
            if topology_pending:
                self._display_reconcile_deferred = True
                observed_at = time.monotonic()
                self._next_display_poll_at = min(
                    getattr(self, "_next_display_poll_at", observed_at),
                    observed_at + DISPLAY_TOPOLOGY_SAMPLE_SECONDS,
                )
                return
            monitors = self._cached_monitor_snapshot()
            fingerprint = monitor_topology_fingerprint(monitors) if monitors else None
            self.reconcile_overlay_position(
                "render_size",
                persist=fingerprint == self._stable_monitor_fingerprint,
                monitors=monitors,
            )

    def display_widgets(self) -> list[DisplayWidget]:
        widgets: list[DisplayWidget] = []
        selected = effective_display_windows(self.settings, self.snapshot)
        show_resets = bool(self.settings.get("show_resets", False))
        if not selected:
            widgets.append(DisplayWidget("rate_waiting", "Waiting for Codex rate data", COLOR_MUTED))
        for key in selected:
            rate_window = self.get_window(key)
            if rate_window is None:
                continue
            color = (
                COLOR_MUTED
                if rate_window.remaining_percent is None
                else percent_color(rate_window.remaining_percent)
            )
            widgets.append(
                DisplayWidget(
                    key,
                    self.format_window_text(rate_window, show_resets),
                    color,
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

        return widgets

    def format_window_text(self, rate_window: RateWindow, show_resets: bool) -> str:
        remaining = "--" if rate_window.remaining_percent is None else f"{rate_window.remaining_percent}%"
        text = f"{rate_window.label} {remaining}"
        if show_resets:
            text += f" reset {format_reset_countdown(rate_window.resets_at)}"
        return text

    def get_window(self, key: str) -> RateWindow | None:
        if self.snapshot is None:
            return None
        return self.snapshot.primary if key == "primary" else self.snapshot.secondary

    def _set_topmost(self, enabled: bool) -> None:
        requested = bool(enabled)
        if getattr(self, "_topmost_enabled", None) == requested:
            return
        try:
            self.root.attributes("-topmost", requested)
        except (AttributeError, tk.TclError):
            return
        self._topmost_enabled = requested

    def update_visibility(
        self,
        force: bool = False,
        should_show: bool | None = None,
        monitors: tuple[MonitorWorkArea, ...] | list[MonitorWorkArea] | None = None,
    ) -> None:
        if self._visibility_interaction_active():
            self.needs_visibility_after_menu = True
            return
        self.needs_visibility_after_menu = False
        now = time.monotonic()
        if should_show is None:
            should_show = self._visibility_should_show(now, force=force)
        else:
            self._cached_should_show = bool(should_show)

        was_shown = getattr(self, "_overlay_is_shown", False)
        if should_show:
            if not was_shown:
                observed_monitors = (
                    windows_monitor_work_areas()
                    if monitors is None
                    else tuple(monitors)
                )
                if observed_monitors:
                    self._remember_monitor_snapshot(observed_monitors)
                fingerprint = (
                    monitor_topology_fingerprint(observed_monitors)
                    if observed_monitors
                    else None
                )
                self.reconcile_overlay_position(
                    "pre_deiconify",
                    persist=fingerprint == getattr(self, "_stable_monitor_fingerprint", None),
                    monitors=observed_monitors,
                )
                self.root.deiconify()
                self._overlay_is_shown = True
                self._set_topmost(True)
            elif force:
                self._set_topmost(True)
        else:
            if was_shown:
                self.root.withdraw()
            self._overlay_is_shown = False

    def show_menu(self, event: tk.Event) -> None:
        if self.is_dragging or getattr(self, "_quitting", False):
            return
        if self.menu_active:
            self.finish_menu_interaction(
                "reopen",
                apply_deferred_render=False,
                schedule_visibility=False,
            )
        self.begin_menu_interaction()
        self.menu_anchor = (int(event.x_root), int(event.y_root))
        self.replace_menu_window(self.build_menu_rows(), self.menu_anchor)

    def begin_menu_interaction(self) -> None:
        self._cancel_post_menu_visibility_reconcile()
        self._cancel_post_drag_visibility_reconcile()
        self.menu_active = True
        self.needs_render_after_menu = False
        self.needs_visibility_after_menu = False

    def _close_menu_window(self, reason: str) -> bool:
        menu_window = getattr(self, "menu_window", None)
        self.menu_window = None
        if menu_window is None:
            return False
        self._last_menu_close_reason = reason
        try:
            menu_window.close()
        except (AttributeError, tk.TclError):
            pass
        return True

    def _cancel_post_menu_visibility_reconcile(self) -> None:
        after_id = getattr(self, "_post_menu_visibility_after_id", None)
        self._post_menu_visibility_after_id = None
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except (AttributeError, tk.TclError):
            pass

    def _reconcile_visibility_after_menu(self) -> None:
        self._post_menu_visibility_after_id = None
        if getattr(self, "_quitting", False) or getattr(self, "menu_active", False):
            return
        self.update_visibility(force=True)

    def _schedule_post_menu_visibility_reconcile(self) -> None:
        self._cancel_post_menu_visibility_reconcile()
        if getattr(self, "_quitting", False):
            return
        try:
            self._post_menu_visibility_after_id = self.root.after(
                POST_MENU_VISIBILITY_DELAY_MS,
                self._reconcile_visibility_after_menu,
            )
        except (AttributeError, tk.TclError):
            self._post_menu_visibility_after_id = None
            self.update_visibility(force=True)

    def _cancel_post_drag_visibility_reconcile(self) -> None:
        after_id = getattr(self, "_post_drag_visibility_after_id", None)
        self._post_drag_visibility_after_id = None
        if after_id is None:
            return
        try:
            self.root.after_cancel(after_id)
        except (AttributeError, tk.TclError):
            pass

    def _reconcile_visibility_after_drag(self) -> None:
        self._post_drag_visibility_after_id = None
        if (
            getattr(self, "_quitting", False)
            or getattr(self, "is_dragging", False)
            or getattr(self, "menu_active", False)
        ):
            return
        self.update_visibility(force=True)

    def _schedule_post_drag_visibility_reconcile(self) -> None:
        self._cancel_post_drag_visibility_reconcile()
        if getattr(self, "_quitting", False):
            return
        try:
            self._post_drag_visibility_after_id = self.root.after(
                POST_DRAG_VISIBILITY_DELAY_MS,
                self._reconcile_visibility_after_drag,
            )
        except (AttributeError, tk.TclError):
            self._post_drag_visibility_after_id = None
            self.update_visibility(force=True)

    def _visibility_interaction_active(self) -> bool:
        return bool(
            getattr(self, "menu_active", False)
            or getattr(self, "is_dragging", False)
            or getattr(self, "_post_menu_visibility_after_id", None) is not None
            or getattr(self, "_post_drag_visibility_after_id", None) is not None
        )

    def finish_menu_interaction(
        self,
        reason: str = "dismissed",
        *,
        apply_deferred_render: bool = True,
        schedule_visibility: bool = True,
    ) -> bool:
        had_interaction = getattr(self, "menu_active", False) or getattr(
            self,
            "menu_window",
            None,
        ) is not None
        if not had_interaction:
            return False
        render_pending = getattr(self, "needs_render_after_menu", False)
        self._close_menu_window(reason)
        self._last_menu_close_reason = reason
        self.menu_anchor = None
        self.menu_active = False
        self.needs_render_after_menu = False
        self.needs_visibility_after_menu = False
        if (
            apply_deferred_render
            and render_pending
            and not getattr(self, "_quitting", False)
        ):
            self.request_render(force=True)
        if schedule_visibility:
            self._schedule_post_menu_visibility_reconcile()
        elif getattr(self, "_overlay_is_shown", False) and not getattr(
            self,
            "_quitting",
            False,
        ):
            self._set_topmost(True)
        return True

    def replace_menu_window(self, rows: list[MenuRow], anchor: tuple[int, int] | None = None) -> None:
        if anchor is None:
            anchor = self.current_menu_anchor()
        self._close_menu_window("replacement")
        self.menu_anchor = anchor
        self.menu_window = ContextMenuWindow(self, rows)
        self.menu_window.show(anchor[0], anchor[1])

    def current_menu_anchor(self) -> tuple[int, int]:
        if self.menu_window is not None:
            return self.menu_window.related_popup_anchor()
        if self.menu_anchor is not None:
            return self.menu_anchor
        try:
            return (int(self.root.winfo_pointerx()), int(self.root.winfo_pointery()))
        except tk.TclError:
            return (0, 0)

    def show_detail_menu(self) -> None:
        self.replace_menu_window(self.build_detail_menu_rows())

    def show_main_menu(self) -> None:
        self.replace_menu_window(self.build_menu_rows())

    def run_menu_command(self, action: Callable[[], None]) -> None:
        self.finish_menu_interaction("command")
        action()

    def build_menu_rows(self) -> list[MenuRow]:
        rows: list[MenuRow] = []
        current_visibility = normalize_visibility_mode(self.settings.get("visibility_mode"))
        available_windows = available_rate_window_keys(self.snapshot)
        current_windows = set(effective_display_windows(self.settings, self.snapshot))
        show_resets = bool(self.settings.get("show_resets", False))
        show_token_counter = bool(self.settings.get("show_token_counter", False))
        show_api_cost_estimate = bool(self.settings.get("show_api_cost_estimate", False))
        current_layout = normalize_layout_mode(self.settings.get("layout_mode"))

        rows.append(MenuRow.disabled("Visibility"))
        visibility_items = [
            ("Always", "always"),
            ("When Codex process is running", "process"),
            ("When Codex is foreground", "foreground"),
            ("When Codex window is visible", "visible_window"),
        ]
        for label, mode in visibility_items:
            supported = self.process_backend.is_supported(mode)
            item_label = label if supported else f"{label} (Windows only)"
            rows.append(
                MenuRow.command(
                    selected_menu_label(item_label, current_visibility == mode),
                    lambda selected=mode: self.run_menu_command(lambda: self.set_visibility_mode(selected)),
                    enabled=supported,
                )
            )

        rows.append(MenuRow.separator())
        rows.append(MenuRow.disabled("Display"))
        if not available_windows:
            rows.append(MenuRow.disabled("Rate windows: waiting for data"))
        else:
            for key in available_windows:
                rate_window = self.get_window(key)
                if rate_window is None:
                    continue
                rows.append(
                    MenuRow.command(
                        checked_menu_label(long_window_label(rate_window), key in current_windows),
                        lambda selected=key: self.run_menu_command(
                            lambda: self.toggle_display_window(selected)
                        ),
                    )
                )
        rows.append(
            MenuRow.command(
                checked_menu_label("Show Reset Countdown", show_resets),
                lambda: self.run_menu_command(self.toggle_show_resets),
            )
        )
        rows.append(
            MenuRow.command(
                checked_menu_label("Show Token Counter", show_token_counter),
                lambda: self.run_menu_command(self.toggle_show_token_counter),
            )
        )
        rows.append(
            MenuRow.command(
                checked_menu_label("Show API Cost Estimate", show_api_cost_estimate),
                lambda: self.run_menu_command(self.toggle_show_api_cost_estimate),
            )
        )

        rows.append(MenuRow.separator())
        rows.append(MenuRow.disabled("Layout"))
        layout_items = [
            ("Horizontal", "horizontal"),
            ("Vertical", "vertical"),
        ]
        for label, mode in layout_items:
            rows.append(
                MenuRow.command(
                    selected_menu_label(label, current_layout == mode),
                    lambda selected=mode: self.run_menu_command(lambda: self.set_layout_mode(selected)),
                )
            )

        rows.append(MenuRow.separator())
        rows.append(MenuRow.command("Details...", self.show_detail_menu))
        rows.append(MenuRow.command("Reset Token Counter", lambda: self.run_menu_command(self.reset_token_counter)))

        rows.append(MenuRow.separator())
        rows.append(MenuRow.command("Refresh", lambda: self.run_menu_command(self.manual_refresh)))
        rows.append(MenuRow.command("Reset position", lambda: self.run_menu_command(self.reset_position)))
        rows.append(MenuRow.command("Quit", lambda: self.run_menu_command(self.quit)))
        return rows

    def build_detail_menu_rows(self) -> list[MenuRow]:
        rows: list[MenuRow] = []
        rows.append(MenuRow.command("Back to menu", self.show_main_menu))
        rows.append(MenuRow.separator())
        rows.append(MenuRow.disabled("Status"))
        rows.append(MenuRow.disabled(self.status_text()))
        if self.snapshot and self.snapshot.plan_type:
            rows.append(MenuRow.disabled(f"Plan: {self.snapshot.plan_type}"))
        if self.snapshot:
            rows.append(MenuRow.disabled(self.source_status_text()))
        host_build = getattr(self, "_installed_host_build", None)
        rows.append(MenuRow.disabled(f"Desktop host: {host_build or 'not detected'}"))
        if getattr(self, "_last_menu_close_reason", None):
            rows.append(
                MenuRow.disabled(
                    f"Last menu close: {self._last_menu_close_reason}"
                )
            )
        if getattr(self, "_last_ui_error", None):
            rows.append(MenuRow.disabled(f"Last UI error: {self._last_ui_error}"))
        if getattr(self, "_last_settings_error", None):
            rows.append(
                MenuRow.disabled(
                    f"Last settings error: {self._last_settings_error}"
                )
            )

        if self.snapshot:
            rows.append(MenuRow.separator())
            rows.append(MenuRow.disabled("Rate Windows"))
            for key in available_rate_window_keys(self.snapshot):
                rate_window = self.get_window(key)
                if rate_window:
                    value = (
                        "--"
                        if rate_window.remaining_percent is None
                        else f"{rate_window.remaining_percent}% remaining"
                    )
                    rows.append(
                        MenuRow.disabled(
                            f"{rate_window.label}: {value}, resets {format_reset_time(rate_window.resets_at)}"
                        )
                    )

        rows.append(MenuRow.separator())
        rows.append(MenuRow.disabled("Token Counter"))
        rows.append(MenuRow.disabled(self.token_counter.display_text()))
        rows.append(
            MenuRow.disabled(
                f"Input {format_token_count(self.token_counter.totals.input_tokens)}, "
                f"cached {format_token_count(self.token_counter.totals.cached_input_tokens)}, "
                f"output {format_token_count(self.token_counter.totals.output_tokens)}, "
                f"reasoning {format_token_count(self.token_counter.totals.reasoning_output_tokens)}"
            )
        )
        rows.append(
            MenuRow.disabled(
                f"Reset {format_snapshot_time(datetime.fromtimestamp(self.token_counter.reset_at, timezone.utc).isoformat())}"
            )
        )

        self.add_api_estimate_menu_rows(rows)
        return rows

    def add_api_estimate_menu_rows(self, rows: list[MenuRow]) -> None:
        estimate = self.current_api_cost_estimate()
        model_name = estimate.model or "unknown"
        rows.append(MenuRow.separator())
        rows.append(MenuRow.disabled("API Estimate"))
        rows.append(MenuRow.disabled(format_api_cost_estimate(estimate)))
        rows.append(MenuRow.disabled(f"Detected model: {model_name} ({estimate.model_source})"))
        if estimate.pricing is None:
            rows.append(MenuRow.disabled("Pricing model: unavailable"))
        else:
            pricing_label = format_model_name(estimate.pricing_model)
            if estimate.pricing_is_proxy:
                pricing_label += " proxy"
            rows.append(MenuRow.disabled(f"Pricing model: {pricing_label}; tier: Standard (assumed)"))
        rows.append(
            MenuRow.disabled(
                f"Tokens: input {format_token_count(estimate.uncached_input_tokens)}, "
                f"cached {format_token_count(estimate.cached_input_tokens)}, "
                f"output {format_token_count(estimate.output_tokens)}"
            )
        )
        rows.append(
            MenuRow.disabled(
                f"Long-context requests (>{format_token_count(LONG_CONTEXT_INPUT_THRESHOLD_TOKENS)} input): "
                f"{estimate.long_context_request_count}"
            )
        )

        if estimate.pricing is None:
            rows.append(MenuRow.disabled(f"No pricing row configured for {model_name}"))
        else:
            cached_rate = estimate.pricing.cached_input_per_million
            rows.append(
                MenuRow.disabled(
                    f"Rates /1M (short): input {format_api_rate(estimate.pricing.input_per_million)}, "
                    f"cached {format_api_rate(cached_rate)}, "
                    f"write {format_published_api_rate(estimate.pricing.cache_write_per_million)}, "
                    f"output {format_api_rate(estimate.pricing.output_per_million)}"
                )
            )
            if estimate.pricing.long_context_input_per_million is not None:
                rows.append(
                    MenuRow.disabled(
                        f"Rates /1M (>{format_token_count(estimate.pricing.long_context_threshold_tokens or LONG_CONTEXT_INPUT_THRESHOLD_TOKENS)} input): "
                        f"input {format_api_rate(estimate.pricing.long_context_input_per_million)}, "
                        f"cached {format_api_rate(estimate.pricing.long_context_cached_input_per_million)}, "
                        f"write {format_published_api_rate(estimate.pricing.long_context_cache_write_per_million)}, "
                        f"output {format_api_rate(estimate.pricing.long_context_output_per_million)}"
                    )
                )
            rows.append(
                MenuRow.disabled(
                    f"Costs: input {format_api_cost(estimate.input_cost)}, "
                    f"cached {format_api_cost(estimate.cached_input_cost)}, "
                    f"output {format_api_cost(estimate.output_cost)}"
                )
            )

        if estimate.warning:
            rows.append(MenuRow.disabled(estimate.warning))
        rows.append(MenuRow.disabled(CACHE_WRITE_TELEMETRY_NOTE))
        rows.append(MenuRow.disabled("API-equivalent estimate only; not actual Codex billing"))

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
        self.update_visibility(force=True)
        self._schedule_refresh()

    def set_display_window(self, key: str, enabled: bool) -> None:
        current = set(normalize_display_windows(self.settings.get("display_windows")))
        effective = set(effective_display_windows(self.settings, self.snapshot))
        if enabled:
            current.add(key)
        elif key in effective and len(effective) == 1:
            return
        else:
            current.discard(key)

        ordered = [item for item in VALID_DISPLAY_WINDOWS if item in current]
        if not ordered:
            return
        self.settings["display_windows"] = ordered
        self.save_settings()
        self.request_render()

    def toggle_display_window(self, key: str) -> None:
        current = set(effective_display_windows(self.settings, self.snapshot))
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
        self._write_runtime_state()
        self.request_render()

    def reset_position(self) -> None:
        self.settings["position"] = None
        self.save_settings()
        self.request_render()
        self.reconcile_overlay_position(
            "reset",
            candidate=None,
            persist=False,
        )

    def save_settings(self) -> None:
        previous_error = getattr(self, "_last_settings_error", None)
        self._last_settings_error = save_settings(self.settings, self.settings_path)
        if self._last_settings_error is not None or previous_error is not None:
            try:
                self._write_runtime_state()
            except (AttributeError, OSError, tk.TclError):
                pass

    def quit(self) -> None:
        if self._quitting:
            return
        self._quitting = True
        self._cancel_post_menu_visibility_reconcile()
        self._cancel_post_drag_visibility_reconcile()
        self.finish_menu_interaction(
            "quit",
            apply_deferred_render=False,
            schedule_visibility=False,
        )
        refresh_after_id = getattr(self, "_refresh_after_id", None)
        if refresh_after_id is not None:
            try:
                self.root.after_cancel(refresh_after_id)
            except (AttributeError, tk.TclError):
                pass
            self._refresh_after_id = None
        if self._display_verification_after_id is not None:
            try:
                self.root.after_cancel(self._display_verification_after_id)
            except (AttributeError, tk.TclError):
                pass
            self._display_verification_after_id = None
        self._display_verification_due_at = None
        observer = getattr(self, "_display_observer", None)
        if observer is not None:
            observer.close_before_root_destroy()
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
