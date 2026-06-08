import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path


APP_PATH = Path(__file__).with_name("codex_usage_overlay.pyw")
LOADER = importlib.machinery.SourceFileLoader("codex_usage_overlay", str(APP_PATH))
SPEC = importlib.util.spec_from_loader("codex_usage_overlay", LOADER)
overlay = importlib.util.module_from_spec(SPEC)
sys.modules["codex_usage_overlay"] = overlay
LOADER.exec_module(overlay)


def token_count_line(
    timestamp,
    primary=None,
    secondary=None,
    last_usage=None,
    total_usage=None,
    include_rate_limits=True,
):
    rate_limits = {
        "limit_id": "codex",
        "primary": primary,
        "secondary": secondary,
        "plan_type": "prolite",
        "rate_limit_reached_type": None,
    }
    payload = {"type": "token_count"}
    if last_usage is not None or total_usage is not None:
        payload["info"] = {}
        if last_usage is not None:
            payload["info"]["last_token_usage"] = last_usage
        if total_usage is not None:
            payload["info"]["total_token_usage"] = total_usage
    event = {
        "timestamp": timestamp,
        "type": "event_msg",
        "payload": payload,
    }
    if include_rate_limits:
        event["rate_limits"] = rate_limits
    return json.dumps(event)


def websocket_rate_log(
    primary=None,
    secondary=None,
    plan_type="prolite",
    limit_reached=False,
    additional_rate_limits=None,
):
    payload = {
        "type": "codex.rate_limits",
        "plan_type": plan_type,
        "rate_limits": {
            "allowed": not limit_reached,
            "limit_reached": limit_reached,
            "primary": primary,
            "secondary": secondary,
        },
        "code_review_rate_limits": None,
        "additional_rate_limits": additional_rate_limits,
    }
    return "stream_request: websocket event: " + json.dumps(payload) + " transport=responses_websocket"


def create_logs_db(path, rows):
    connection = sqlite3.connect(path)
    try:
        connection.execute(
            """
            CREATE TABLE logs (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                ts INTEGER NOT NULL,
                target TEXT NOT NULL,
                feedback_log_body TEXT
            )
            """
        )
        for row in rows:
            connection.execute(
                "INSERT INTO logs (ts, target, feedback_log_body) VALUES (?, ?, ?)",
                row,
            )
        connection.commit()
    finally:
        connection.close()


class RateParserTests(unittest.TestCase):
    def test_parses_both_windows(self):
        line = token_count_line(
            "2026-05-30T20:26:33.794Z",
            primary={"used_percent": 7.0, "window_minutes": 300, "resets_at": 1780186310},
            secondary={"used_percent": 6.0, "window_minutes": 10080, "resets_at": 1780600723},
        )

        snapshot = overlay.parse_rate_line(line)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.primary.label, "5h")
        self.assertEqual(snapshot.primary.remaining_percent, 93)
        self.assertEqual(snapshot.secondary.label, "7d")
        self.assertEqual(snapshot.secondary.remaining_percent, 94)

    def test_parses_one_missing_window(self):
        line = token_count_line(
            "2026-05-30T20:26:33.794Z",
            primary={"used_percent": 25, "window_minutes": 300},
            secondary=None,
        )

        snapshot = overlay.parse_rate_line(line)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.primary.remaining_percent, 75)
        self.assertIsNone(snapshot.secondary)

    def test_fractional_used_percent_does_not_overstate_remaining(self):
        line = token_count_line(
            "2026-05-30T20:26:33.794Z",
            primary={"used_percent": 24.5, "window_minutes": 300},
            secondary={"used_percent": 7.1, "window_minutes": 10080},
        )

        snapshot = overlay.parse_rate_line(line)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.primary.used_percent, 24.5)
        self.assertEqual(snapshot.primary.remaining_percent, 75)
        self.assertEqual(snapshot.secondary.used_percent, 7.1)
        self.assertEqual(snapshot.secondary.remaining_percent, 92)

    def test_ignores_malformed_json_and_partial_trailing_line(self):
        text = "\n".join(
            [
                "{bad json",
                token_count_line(
                    "2026-05-30T20:26:33.794Z",
                    primary={"used_percent": 10, "window_minutes": 300},
                    secondary={"used_percent": 12, "window_minutes": 10080},
                ),
                '{"timestamp":"2026-05-30T20:27:00.000Z","type":"event_msg","payload"',
            ]
        )

        snapshots = overlay.parse_rate_snapshots_from_text(text)

        self.assertEqual(len(snapshots), 1)
        self.assertEqual(snapshots[0].primary.remaining_percent, 90)

    def test_selects_newer_event_over_stale_event(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions" / "2026" / "05" / "30"
            sessions.mkdir(parents=True)
            path = sessions / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    [
                        token_count_line(
                            "2026-05-30T20:26:33.794Z",
                            primary={"used_percent": 50, "window_minutes": 300},
                            secondary={"used_percent": 40, "window_minutes": 10080},
                        ),
                        token_count_line(
                            "2026-05-30T20:27:33.794Z",
                            primary={"used_percent": 8, "window_minutes": 300},
                            secondary={"used_percent": 6, "window_minutes": 10080},
                        ),
                    ]
                ),
                encoding="utf-8",
            )

            snapshot = overlay.RateLogReader(Path(temp_dir)).latest_snapshot(force_rescan=True)

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.primary.remaining_percent, 92)
            self.assertEqual(snapshot.secondary.remaining_percent, 94)

    def test_reads_appended_lines_after_initial_read(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions" / "2026" / "06" / "01"
            sessions.mkdir(parents=True)
            path = sessions / "rollout.jsonl"
            path.write_text(
                token_count_line(
                    "2026-06-01T12:00:00.000Z",
                    primary={"used_percent": 50, "window_minutes": 300},
                    secondary={"used_percent": 10, "window_minutes": 10080},
                )
                + "\n",
                encoding="utf-8",
            )
            reader = overlay.RateLogReader(Path(temp_dir))

            first = reader.latest_snapshot(force_rescan=True)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    token_count_line(
                        "2026-06-01T12:00:01.000Z",
                        primary={"used_percent": 40, "window_minutes": 300},
                        secondary={"used_percent": 9, "window_minutes": 10080},
                    )
                    + "\n"
                )
            second = reader.latest_snapshot()

            self.assertEqual(first.primary.remaining_percent, 50)
            self.assertEqual(second.primary.remaining_percent, 60)
            self.assertEqual(second.secondary.remaining_percent, 91)

    def test_new_session_file_is_active_without_waiting(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions" / "2026" / "06" / "01"
            sessions.mkdir(parents=True)
            old_path = sessions / "old.jsonl"
            old_path.write_text(
                token_count_line(
                    "2026-06-01T12:00:00.000Z",
                    primary={"used_percent": 50, "window_minutes": 300},
                    secondary={"used_percent": 10, "window_minutes": 10080},
                )
                + "\n",
                encoding="utf-8",
            )
            reader = overlay.RateLogReader(Path(temp_dir))
            first = reader.latest_snapshot(force_rescan=True)

            new_path = sessions / "new.jsonl"
            new_path.write_text(
                token_count_line(
                    "2026-06-01T12:00:02.000Z",
                    primary={"used_percent": 30, "window_minutes": 300},
                    secondary={"used_percent": 8, "window_minutes": 10080},
                )
                + "\n",
                encoding="utf-8",
            )
            second = reader.latest_snapshot()

            self.assertEqual(first.primary.remaining_percent, 50)
            self.assertEqual(second.primary.remaining_percent, 70)
            self.assertEqual(second.secondary.remaining_percent, 92)

    def test_truncated_file_falls_back_safely(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions" / "2026" / "06" / "01"
            sessions.mkdir(parents=True)
            path = sessions / "rollout.jsonl"
            path.write_text(
                "\n".join(
                    token_count_line(
                        f"2026-06-01T12:00:{second:02d}.000Z",
                        primary={"used_percent": 80, "window_minutes": 300},
                        secondary={"used_percent": 10, "window_minutes": 10080},
                    )
                    for second in range(5)
                )
                + "\n",
                encoding="utf-8",
            )
            reader = overlay.RateLogReader(Path(temp_dir))
            first = reader.latest_snapshot(force_rescan=True)

            path.write_text(
                token_count_line(
                    "2026-06-01T12:01:00.000Z",
                    primary={"used_percent": 35, "window_minutes": 300},
                    secondary={"used_percent": 8, "window_minutes": 10080},
                )
                + "\n",
                encoding="utf-8",
            )
            second = reader.latest_snapshot()

            self.assertEqual(first.primary.remaining_percent, 20)
            self.assertEqual(second.primary.remaining_percent, 65)

    def test_reader_ignores_partial_trailing_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions" / "2026" / "06" / "01"
            sessions.mkdir(parents=True)
            path = sessions / "rollout.jsonl"
            path.write_text(
                token_count_line(
                    "2026-06-01T12:00:00.000Z",
                    primary={"used_percent": 10, "window_minutes": 300},
                    secondary={"used_percent": 12, "window_minutes": 10080},
                )
                + "\n"
                + '{"timestamp":"2026-06-01T12:00:01.000Z","type":"event_msg","payload"',
                encoding="utf-8",
            )

            snapshot = overlay.RateLogReader(Path(temp_dir)).latest_snapshot(force_rescan=True)

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.primary.remaining_percent, 90)

    def test_parses_sqlite_websocket_rate_limit_row(self):
        body = websocket_rate_log(
            primary={"used_percent": 21, "window_minutes": 300, "reset_at": 1780421759},
            secondary={"used_percent": 20, "window_minutes": 10080, "reset_at": 1780847180},
            additional_rate_limits={
                "GPT-5.3-Codex-Spark": {
                    "primary": {"used_percent": 0, "window_minutes": 300, "reset_at": 1780438843}
                }
            },
        )

        snapshot = overlay.parse_sqlite_rate_limit_log_body(body, observed_at=1780420843, source_path="logs_2.sqlite:1")

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.source_kind, "logs_2.sqlite")
        self.assertEqual(snapshot.primary.label, "5h")
        self.assertEqual(snapshot.primary.remaining_percent, 79)
        self.assertEqual(snapshot.primary.resets_at, 1780421759)
        self.assertEqual(snapshot.secondary.label, "7d")
        self.assertEqual(snapshot.secondary.remaining_percent, 80)

    def test_parses_sqlite_websocket_one_missing_window(self):
        body = websocket_rate_log(
            primary={"used_percent": 30, "window_minutes": 300, "reset_at": 1780421759},
            secondary=None,
        )

        snapshot = overlay.parse_sqlite_rate_limit_log_body(body, observed_at=1780420843)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.primary.remaining_percent, 70)
        self.assertIsNone(snapshot.secondary)

    def test_ignores_malformed_sqlite_websocket_json(self):
        body = 'stream_request: websocket event: {"type":"codex.rate_limits","rate_limits"'

        self.assertIsNone(overlay.parse_sqlite_rate_limit_log_body(body, observed_at=1780420843))

    def test_sqlite_snapshot_newer_than_jsonl_wins(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            sessions = home / "sessions" / "2026" / "06" / "02"
            sessions.mkdir(parents=True)
            (sessions / "rollout.jsonl").write_text(
                token_count_line(
                    "2026-06-02T17:20:00Z",
                    primary={"used_percent": 15, "window_minutes": 300, "resets_at": 1780421759},
                    secondary={"used_percent": 19, "window_minutes": 10080, "resets_at": 1780847180},
                )
                + "\n",
                encoding="utf-8",
            )
            create_logs_db(
                home / "logs_2.sqlite",
                [
                    (
                        1780420843,
                        "codex_api::endpoint::responses_websocket",
                        websocket_rate_log(
                            primary={"used_percent": 21, "window_minutes": 300, "reset_at": 1780421759},
                            secondary={"used_percent": 20, "window_minutes": 10080, "reset_at": 1780847180},
                        ),
                    )
                ],
            )

            snapshot = overlay.RateLogReader(home).latest_snapshot(force_rescan=True)

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.source_kind, "logs_2.sqlite")
            self.assertEqual(snapshot.primary.remaining_percent, 79)
            self.assertEqual(snapshot.secondary.remaining_percent, 80)

    def test_jsonl_snapshot_wins_without_usable_sqlite_data(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            sessions = home / "sessions" / "2026" / "06" / "02"
            sessions.mkdir(parents=True)
            (sessions / "rollout.jsonl").write_text(
                token_count_line(
                    "2026-06-02T17:20:00Z",
                    primary={"used_percent": 15, "window_minutes": 300, "resets_at": 1780421759},
                    secondary={"used_percent": 19, "window_minutes": 10080, "resets_at": 1780847180},
                )
                + "\n",
                encoding="utf-8",
            )
            create_logs_db(
                home / "logs_2.sqlite",
                [
                    (
                        1780420843,
                        "log",
                        websocket_rate_log(
                            primary={"used_percent": 21, "window_minutes": 300, "reset_at": 1780421759},
                            secondary={"used_percent": 20, "window_minutes": 10080, "reset_at": 1780847180},
                        ),
                    )
                ],
            )

            snapshot = overlay.RateLogReader(home).latest_snapshot(force_rescan=True)

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.source_kind, "session_jsonl")
            self.assertEqual(snapshot.primary.remaining_percent, 85)

    def test_ignores_copied_prompt_text_containing_rate_limits(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            sessions = home / "sessions" / "2026" / "06" / "02"
            sessions.mkdir(parents=True)
            (sessions / "rollout.jsonl").write_text(
                token_count_line(
                    "2026-06-02T17:20:00Z",
                    primary={"used_percent": 15, "window_minutes": 300, "resets_at": 1780421759},
                    secondary={"used_percent": 19, "window_minutes": 10080, "resets_at": 1780847180},
                )
                + "\n",
                encoding="utf-8",
            )
            create_logs_db(
                home / "logs_2.sqlite",
                [
                    (
                        1780420843,
                        "codex_otel.log_only",
                        "tool output copied text " + websocket_rate_log(
                            primary={"used_percent": 99, "window_minutes": 300, "reset_at": 1780421759},
                            secondary={"used_percent": 99, "window_minutes": 10080, "reset_at": 1780847180},
                        ),
                    )
                ],
            )

            snapshot = overlay.RateLogReader(home).latest_snapshot(force_rescan=True)

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.source_kind, "session_jsonl")
            self.assertEqual(snapshot.primary.remaining_percent, 85)


class TokenParserTests(unittest.TestCase):
    def test_token_event_with_last_usage(self):
        line = token_count_line(
            "1970-01-01T00:16:41Z",
            last_usage={
                "input_tokens": 10,
                "cached_input_tokens": 3,
                "output_tokens": 5,
                "reasoning_output_tokens": 2,
                "total_tokens": 15,
            },
            total_usage={"total_tokens": 100},
            include_rate_limits=False,
        )

        event = overlay.parse_token_event_line(line, "session.jsonl")

        self.assertIsNotNone(event)
        self.assertEqual(event.timestamp, "1970-01-01T00:16:41Z")
        self.assertEqual(event.usage.total_tokens, 15)
        self.assertIn("session.jsonl", event.fingerprint)

    def test_token_event_missing_usage_is_parsed_without_usage(self):
        line = token_count_line("1970-01-01T00:16:41Z", include_rate_limits=False)

        event = overlay.parse_token_event_line(line)

        self.assertIsNotNone(event)
        self.assertIsNone(event.usage)

    def test_token_event_with_usage_but_no_rate_limits(self):
        line = token_count_line(
            "1970-01-01T00:16:41Z",
            last_usage={"total_tokens": 25},
            include_rate_limits=False,
        )

        self.assertIsNone(overlay.parse_rate_line(line))
        event = overlay.parse_token_event_line(line)
        self.assertIsNotNone(event)
        self.assertEqual(event.usage.total_tokens, 25)

    def test_malformed_token_event_is_ignored(self):
        self.assertIsNone(overlay.parse_token_event_line("{bad json"))


class TokenCounterTests(unittest.TestCase):
    def event(self, timestamp, total_tokens, fingerprint=None):
        return overlay.TokenEvent(
            timestamp=timestamp,
            fingerprint=fingerprint or f"{timestamp}-{total_tokens}",
            usage=overlay.TokenUsage(
                input_tokens=total_tokens,
                cached_input_tokens=1,
                output_tokens=2,
                reasoning_output_tokens=3,
                total_tokens=total_tokens,
            ),
            source_path="session.jsonl",
        )

    def test_starts_at_zero_and_resets(self):
        counter = overlay.TokenCounter(reset_at=1_000)
        self.assertEqual(counter.totals.total_tokens, 0)

        counter.add_events([self.event("1970-01-01T00:16:41Z", 10)], now=1_001)
        self.assertEqual(counter.totals.total_tokens, 10)
        counter.reset(now=2_000)

        self.assertEqual(counter.reset_at, 2_000)
        self.assertEqual(counter.totals.total_tokens, 0)
        self.assertEqual(counter.seen_events, set())

    def test_counts_only_events_after_reset_time(self):
        counter = overlay.TokenCounter(reset_at=1_000)

        counter.add_events(
            [
                self.event("1970-01-01T00:16:39Z", 10),
                self.event("1970-01-01T00:16:41Z", 20),
            ],
            now=1_002,
        )

        self.assertEqual(counter.totals.total_tokens, 20)

    def test_aggregates_events_from_multiple_session_files(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            sessions = Path(temp_dir) / "sessions" / "2026" / "06" / "02"
            sessions.mkdir(parents=True)
            first = sessions / "one.jsonl"
            second = sessions / "two.jsonl"
            first.write_text(
                token_count_line(
                    "1970-01-01T00:16:41Z",
                    last_usage={"total_tokens": 11},
                    total_usage={"total_tokens": 11},
                    include_rate_limits=False,
                )
                + "\n",
                encoding="utf-8",
            )
            second.write_text(
                token_count_line(
                    "1970-01-01T00:16:42Z",
                    last_usage={"total_tokens": 12},
                    total_usage={"total_tokens": 12},
                    include_rate_limits=False,
                )
                + "\n",
                encoding="utf-8",
            )
            reader = overlay.RateLogReader(Path(temp_dir))
            batch = reader.read_updates(force_rescan=True)
            counter = overlay.TokenCounter(reset_at=1_000)
            counter.add_events(batch.token_events, now=1_003)

            self.assertEqual(counter.totals.total_tokens, 23)

    def test_does_not_double_count_reread_events(self):
        counter = overlay.TokenCounter(reset_at=1_000)
        event = self.event("1970-01-01T00:16:41Z", 20, fingerprint="same")

        counter.add_events([event], now=1_002)
        counter.add_events([event], now=1_003)

        self.assertEqual(counter.totals.total_tokens, 20)

    def test_formats_token_counter_compactly(self):
        self.assertEqual(overlay.format_token_counter(123_456, 1_000, now=3_520), "123k tokens / 42m")
        self.assertEqual(overlay.format_token_counter(1_234_567, 1_000, now=12_700), "1.2M tokens / 3h 15m")


class ApiCostEstimateTests(unittest.TestCase):
    def test_calculates_uncached_cached_and_output_cost(self):
        usage = overlay.TokenUsage(
            input_tokens=100_000,
            cached_input_tokens=80_000,
            output_tokens=10_000,
            reasoning_output_tokens=9_000,
            total_tokens=110_000,
        )

        estimate = overlay.estimate_api_cost(
            usage,
            overlay.DetectedModel("gpt-5.5", "test"),
        )

        self.assertEqual(estimate.uncached_input_tokens, 20_000)
        self.assertEqual(estimate.cached_input_tokens, 80_000)
        self.assertEqual(estimate.output_tokens, 10_000)
        self.assertAlmostEqual(estimate.input_cost, 0.10)
        self.assertAlmostEqual(estimate.cached_input_cost, 0.04)
        self.assertAlmostEqual(estimate.output_cost, 0.30)
        self.assertAlmostEqual(estimate.total_cost, 0.44)

    def test_reasoning_tokens_are_not_double_counted(self):
        usage = overlay.TokenUsage(
            input_tokens=0,
            cached_input_tokens=0,
            output_tokens=10_000,
            reasoning_output_tokens=10_000,
            total_tokens=10_000,
        )

        estimate = overlay.estimate_api_cost(
            usage,
            overlay.DetectedModel("gpt-5.5", "test"),
        )

        self.assertAlmostEqual(estimate.total_cost, 0.30)

    def test_pro_model_cached_tokens_use_input_rate(self):
        usage = overlay.TokenUsage(
            input_tokens=100_000,
            cached_input_tokens=80_000,
            output_tokens=10_000,
            total_tokens=110_000,
        )

        estimate = overlay.estimate_api_cost(
            usage,
            overlay.DetectedModel("gpt-5.5-pro", "test"),
        )

        self.assertAlmostEqual(estimate.input_cost, 0.60)
        self.assertAlmostEqual(estimate.cached_input_cost, 2.40)
        self.assertAlmostEqual(estimate.output_cost, 1.80)
        self.assertAlmostEqual(estimate.total_cost, 4.80)

    def test_unknown_model_has_no_cost(self):
        estimate = overlay.estimate_api_cost(
            overlay.TokenUsage(input_tokens=1_000, output_tokens=1_000, total_tokens=2_000),
            overlay.DetectedModel("gpt-future", "test"),
        )

        self.assertIsNone(estimate.pricing)
        self.assertIsNone(estimate.total_cost)
        self.assertEqual(overlay.format_api_cost_estimate(estimate), "API est. --")

    def test_formats_costs(self):
        self.assertEqual(overlay.format_api_cost(0), "$0.00")
        self.assertEqual(overlay.format_api_cost(0.0004), "$0.0004")
        self.assertEqual(overlay.format_api_cost(0.071), "$0.07")
        self.assertEqual(overlay.format_api_cost(1_234.5), "$1,234.50")

    def test_warns_when_model_changed_after_reset(self):
        estimate = overlay.estimate_api_cost(
            overlay.TokenUsage(),
            overlay.DetectedModel("gpt-5.4", "test"),
            reset_model="gpt-5.5",
        )

        self.assertIn("Model changed", estimate.warning)


class ModelDetectionTests(unittest.TestCase):
    def create_logs_db(self, path, bodies):
        connection = sqlite3.connect(path)
        try:
            connection.execute("CREATE TABLE logs (id INTEGER PRIMARY KEY AUTOINCREMENT, feedback_log_body TEXT)")
            for body in bodies:
                connection.execute("INSERT INTO logs (feedback_log_body) VALUES (?)", (body,))
            connection.commit()
        finally:
            connection.close()

    def test_detects_latest_model_from_logs(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            self.create_logs_db(
                home / "logs_2.sqlite",
                [
                    'event.name="codex.sse_event" model=gpt-5.4',
                    'session_task.turn model="gpt-5.5"',
                ],
            )
            (home / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")

            detected = overlay.detect_latest_model(home)

            self.assertEqual(detected.model, "gpt-5.5")
            self.assertEqual(detected.source, "logs_2.sqlite")

    def test_falls_back_to_config_model(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            (home / "config.toml").write_text('model = "gpt-5.4"\n', encoding="utf-8")

            detected = overlay.detect_latest_model(home)

            self.assertEqual(detected.model, "gpt-5.4")
            self.assertEqual(detected.source, "config.toml")

    def test_normalizes_model_aliases(self):
        self.assertEqual(overlay.normalize_model_key("GPT-5.5 Pro"), "gpt-5.5-pro")
        self.assertEqual(overlay.normalize_model_key("gpt_5.4"), "gpt-5.4")


class RuntimeStateTests(unittest.TestCase):
    def test_writes_expected_state_shape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = overlay.RuntimeStateStore(path=path, pid=123)
            counter = overlay.TokenCounter(reset_at=1_000)
            estimate = overlay.estimate_api_cost(
                overlay.TokenUsage(input_tokens=1_000, output_tokens=1_000, total_tokens=2_000),
                overlay.DetectedModel("gpt-5.5", "test"),
            )
            snapshot = overlay.RateSnapshot(
                timestamp="2026-06-02T17:20:43+00:00",
                primary=None,
                secondary=None,
                plan_type="prolite",
                rate_limit_reached_type=None,
                source_path="logs_2.sqlite:1",
                source_kind="logs_2.sqlite",
                source_observed_at=1780420843,
            )

            store.write(snapshot, counter, estimate)
            state = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(state["app"], overlay.APP_NAME)
            self.assertEqual(state["pid"], 123)
            self.assertIn("token_counter", state)
            self.assertIn("last_rate_snapshot", state)
            self.assertIn("api_cost_estimate", state)
            self.assertEqual(state["api_cost_estimate"]["model"], "gpt-5.5")
            self.assertEqual(state["rate_source"], "logs_2.sqlite")
            self.assertEqual(state["source_event_timestamp"], "2026-06-02T17:20:43+00:00")
            self.assertEqual(state["source_observed_at"], 1780420843)
            self.assertIn("source_age_seconds", state)
            self.assertEqual(state["last_rate_snapshot"]["source_kind"], "logs_2.sqlite")

    def test_ignores_stale_pid_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            path.write_text(json.dumps({"pid": 987654321}), encoding="utf-8")
            store = overlay.RuntimeStateStore(path=path, pid=123)

            store.cleanup_stale()

            self.assertFalse(path.exists())

    def test_deletes_state_on_normal_quit(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = overlay.RuntimeStateStore(path=path, pid=123)
            store.write(None, overlay.TokenCounter(reset_at=1_000))

            store.delete()

            self.assertFalse(path.exists())


class PositionTests(unittest.TestCase):
    def test_saved_position_inside_bounds_is_preserved(self):
        position = overlay.normalize_overlay_position([100, 200], (0, 0, 1920, 1080), (200, 50))

        self.assertEqual(position, [100, 200])

    def test_saved_position_outside_bounds_is_clamped_visible(self):
        position = overlay.normalize_overlay_position([3913, 999], (0, 0, 1920, 1080), (200, 50))

        self.assertEqual(position, [1720, 999])

    def test_invalid_position_uses_default_visible_position(self):
        position = overlay.normalize_overlay_position(None, (0, 0, 1920, 1080), (200, 50))

        self.assertEqual(position, [1708, 72])

    def test_negative_secondary_monitor_coordinates_are_supported(self):
        inside = overlay.normalize_overlay_position([-1800, 100], (-1920, 0, 3840, 1080), (200, 50))
        outside = overlay.normalize_overlay_position([-2500, -100], (-1920, 0, 3840, 1080), (200, 50))

        self.assertEqual(inside, [-1800, 100])
        self.assertEqual(outside, [-1920, 0])


class SingleInstanceLockTests(unittest.TestCase):
    def test_lock_blocks_second_live_instance(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "overlay.lock"
            first = overlay.SingleInstanceLock(path=path, pid=os.getpid())
            second = overlay.SingleInstanceLock(path=path, pid=123456)

            self.assertTrue(first.acquire())
            try:
                self.assertFalse(second.acquire())
            finally:
                first.release()

            self.assertFalse(path.exists())

    def test_lock_replaces_stale_pid_file(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "overlay.lock"
            path.write_text(json.dumps({"pid": 987654321}), encoding="utf-8")
            lock = overlay.SingleInstanceLock(path=path, pid=123)

            self.assertTrue(lock.acquire())
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(state["pid"], 123)
            finally:
                lock.release()

            self.assertFalse(path.exists())

    def test_lock_replaces_stale_heartbeat_even_if_pid_exists(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "overlay.lock"
            state_path = Path(temp_dir) / "state.json"
            path.write_text(json.dumps({"pid": 123, "created_at": 1}), encoding="utf-8")
            state_path.write_text(json.dumps({"pid": 123, "last_update_at": 1}), encoding="utf-8")
            lock = overlay.SingleInstanceLock(path=path, pid=456, state_path=state_path)
            original_process_exists = overlay.process_exists
            overlay.process_exists = lambda _pid: True
            try:
                self.assertTrue(lock.acquire())
                state = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(state["pid"], 456)
            finally:
                overlay.process_exists = original_process_exists
                lock.release()

            self.assertFalse(path.exists())


class FakeRoot:
    def __init__(self):
        self.attribute_calls = []
        self.deiconified = False
        self.withdrawn = False

    def attributes(self, *args):
        self.attribute_calls.append(args)

    def deiconify(self):
        self.deiconified = True

    def withdraw(self):
        self.withdrawn = True

    def after_idle(self, callback):
        callback()


class FakeProcessBackend:
    def __init__(self, should_show=True):
        self.should_show_result = should_show

    def should_show(self, _mode):
        return self.should_show_result


class MenuInteractionTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(overlay.OverlayApp)
        app.root = FakeRoot()
        app.settings = {"visibility_mode": "always"}
        app.process_backend = FakeProcessBackend()
        app.menu_active = True
        app.is_dragging = False
        app.needs_render_after_menu = False
        app.needs_render_after_drag = False
        app.needs_visibility_after_menu = False
        app.render_calls = 0
        app.render = lambda: setattr(app, "render_calls", app.render_calls + 1)
        return app

    def test_render_defers_while_menu_active(self):
        app = self.make_app()

        overlay.OverlayApp.request_render(app)

        self.assertTrue(app.needs_render_after_menu)
        self.assertEqual(app.render_calls, 0)

    def test_visibility_does_not_reassert_topmost_while_menu_active(self):
        app = self.make_app()

        overlay.OverlayApp.update_visibility(app)

        self.assertTrue(app.needs_visibility_after_menu)
        self.assertEqual(app.root.attribute_calls, [])

    def test_menu_command_finishes_deferred_render_and_topmost(self):
        app = self.make_app()

        overlay.OverlayApp.run_menu_command(app, lambda: overlay.OverlayApp.request_render(app))

        self.assertFalse(app.menu_active)
        self.assertFalse(app.needs_render_after_menu)
        self.assertEqual(app.render_calls, 1)
        self.assertTrue(app.root.deiconified)
        self.assertIn(("-topmost", True), app.root.attribute_calls)


class DisplaySelectionTests(unittest.TestCase):
    def test_primary_only(self):
        self.assertEqual(overlay.normalize_display_windows(["primary"]), ["primary"])

    def test_secondary_only(self):
        self.assertEqual(overlay.normalize_display_windows(["secondary"]), ["secondary"])

    def test_both(self):
        self.assertEqual(overlay.normalize_display_windows(["primary", "secondary"]), ["primary", "secondary"])

    def test_prevents_empty_display_set(self):
        self.assertEqual(overlay.normalize_display_windows([]), ["primary", "secondary"])
        self.assertEqual(overlay.normalize_display_windows(["nonsense"]), ["primary", "secondary"])

    def test_show_resets_defaults_to_false(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            settings = overlay.load_settings(Path(temp_dir) / "missing.json")

        self.assertFalse(settings["show_resets"])
        self.assertFalse(settings["show_token_counter"])
        self.assertFalse(settings["show_api_cost_estimate"])
        self.assertEqual(settings["layout_mode"], "horizontal")

    def test_invalid_layout_mode_defaults_to_horizontal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            path.write_text(json.dumps({"layout_mode": "diagonal"}), encoding="utf-8")

            settings = overlay.load_settings(path)

        self.assertEqual(settings["layout_mode"], "horizontal")

    def test_layout_positions(self):
        self.assertEqual(
            overlay.layout_positions(4, "horizontal"),
            [(0, 0), (0, 1), (0, 2), (0, 3)],
        )
        self.assertEqual(
            overlay.layout_positions(4, "vertical"),
            [(0, 0), (1, 0), (2, 0), (3, 0)],
        )
        self.assertEqual(
            overlay.layout_positions(4, "grid_2x2"),
            [(0, 0), (0, 1), (1, 0), (1, 1)],
        )

    def test_display_widget_ordering(self):
        settings = {
            "display_windows": ["primary", "secondary"],
            "show_token_counter": True,
            "show_api_cost_estimate": True,
        }

        self.assertEqual(
            overlay.active_display_widget_keys(settings),
            ["primary", "secondary", "token_counter", "api_cost"],
        )

        settings["display_windows"] = ["secondary"]
        settings["show_token_counter"] = False

        self.assertEqual(overlay.active_display_widget_keys(settings), ["secondary", "api_cost"])

    def test_reset_countdown_formats_minutes_hours_and_days(self):
        now = 1_000

        self.assertEqual(overlay.format_reset_countdown(now, now=now), "now")
        self.assertEqual(overlay.format_reset_countdown(now + 45, now=now), "1m")
        self.assertEqual(overlay.format_reset_countdown(now + (2 * 3600) + (13 * 60), now=now), "2h 13m")
        self.assertEqual(overlay.format_reset_countdown(now + (2 * 86400) + (3 * 3600), now=now), "2d 3h")

    def test_reset_countdown_handles_missing_reset(self):
        self.assertEqual(overlay.format_reset_countdown(None, now=1_000), "--")

    def test_reset_countdown_marks_stale_reset_pending(self):
        now = 1_000

        self.assertEqual(overlay.format_reset_countdown(now - 61, now=now), "pending")


if __name__ == "__main__":
    unittest.main()
