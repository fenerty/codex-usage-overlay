import importlib.machinery
import importlib.util
import json
import os
import sqlite3
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock


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


def append_logs_db(path, rows):
    connection = sqlite3.connect(path)
    try:
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

    def test_parses_current_desktop_app_rate_and_token_shape(self):
        event = json.loads(
            token_count_line(
                "2026-07-09T19:47:52.000Z",
                primary={"used_percent": 20.0, "window_minutes": 300, "resets_at": 1783645200},
                secondary={"used_percent": 12.0, "window_minutes": 10080, "resets_at": 1784246400},
                last_usage={
                    "input_tokens": 1_000,
                    "cached_input_tokens": 800,
                    "output_tokens": 200,
                    "reasoning_output_tokens": 100,
                    "total_tokens": 1_200,
                },
                total_usage={"total_tokens": 5_000},
            )
        )
        event["payload"]["rate_limits"] = event.pop("rate_limits")
        event["payload"]["rate_limits"].update(
            {
                "credits": {"has_credits": False},
                "individual_limit": None,
                "limit_name": None,
            }
        )
        line = json.dumps(event)

        snapshot = overlay.parse_rate_line(line)
        token_event = overlay.parse_token_event_line(line)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.primary.remaining_percent, 80)
        self.assertEqual(snapshot.secondary.remaining_percent, 88)
        self.assertIsNotNone(token_event)
        self.assertEqual(token_event.usage.cached_input_tokens, 800)

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

    def test_parses_current_single_weekly_window(self):
        line = token_count_line(
            "2026-07-13T15:00:00.000Z",
            primary={"used_percent": 1, "window_minutes": 10080, "resets_at": 1784563200},
            secondary=None,
        )

        snapshot = overlay.parse_rate_line(line)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.primary.label, "7d")
        self.assertEqual(snapshot.primary.remaining_percent, 99)
        self.assertIsNone(snapshot.secondary)

    def test_uses_generic_label_when_duration_is_unavailable(self):
        line = token_count_line(
            "2026-07-13T15:00:00.000Z",
            primary={"used_percent": 10},
            secondary=None,
        )

        snapshot = overlay.parse_rate_line(line)

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.primary.label, "limit")
        self.assertEqual(overlay.long_window_label(snapshot.primary), "Rate limit")

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

    def test_newer_jsonl_snapshot_wins_over_stale_sqlite(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            sessions = home / "sessions" / "2026" / "06" / "02"
            sessions.mkdir(parents=True)
            (sessions / "rollout.jsonl").write_text(
                token_count_line(
                    "2026-06-02T17:20:43Z",
                    primary={"used_percent": 15, "window_minutes": 300},
                    secondary={"used_percent": 19, "window_minutes": 10080},
                )
                + "\n",
                encoding="utf-8",
            )
            create_logs_db(
                home / "logs_2.sqlite",
                [
                    (
                        1780420000,
                        "codex_api::endpoint::responses_websocket",
                        websocket_rate_log(
                            primary={"used_percent": 21, "window_minutes": 300},
                            secondary={"used_percent": 20, "window_minutes": 10080},
                        ),
                    )
                ],
            )

            snapshot = overlay.RateLogReader(home).latest_snapshot(force_rescan=True)

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.source_kind, "session_jsonl")
            self.assertEqual(snapshot.primary.remaining_percent, 85)
            self.assertEqual(snapshot.secondary.remaining_percent, 81)

    def test_sqlite_wins_equal_timestamp_tie(self):
        observed_at = 1780420843
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            sessions = home / "sessions" / "2026" / "06" / "02"
            sessions.mkdir(parents=True)
            (sessions / "rollout.jsonl").write_text(
                token_count_line(
                    overlay.timestamp_from_epoch(observed_at),
                    primary={"used_percent": 15, "window_minutes": 300},
                    secondary={"used_percent": 19, "window_minutes": 10080},
                )
                + "\n",
                encoding="utf-8",
            )
            create_logs_db(
                home / "logs_2.sqlite",
                [
                    (
                        observed_at,
                        "codex_api::endpoint::responses_websocket",
                        websocket_rate_log(
                            primary={"used_percent": 21, "window_minutes": 300},
                            secondary={"used_percent": 20, "window_minutes": 10080},
                        ),
                    )
                ],
            )

            snapshot = overlay.RateLogReader(home).latest_snapshot(force_rescan=True)

            self.assertIsNotNone(snapshot)
            self.assertEqual(snapshot.source_kind, "logs_2.sqlite")
            self.assertEqual(snapshot.primary.remaining_percent, 79)

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


class IncrementalSqliteReaderTests(unittest.TestCase):
    TARGET = "codex_api::endpoint::responses_websocket"

    def row(self, timestamp, used_percent, target=None, body=None):
        return (
            timestamp,
            target or self.TARGET,
            body
            or websocket_rate_log(
                primary={"used_percent": used_percent, "window_minutes": 300},
                secondary=None,
            ),
        )

    def test_unchanged_poll_does_not_reparse_historical_rows(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "logs_2.sqlite"
            create_logs_db(path, [self.row(1_000, 20)])
            reader = overlay.SqliteRateLimitReader(path)
            first = reader.latest_snapshot(force_rescan=True, now=0)

            with mock.patch.object(
                overlay,
                "parse_sqlite_rate_limit_log_body",
                wraps=overlay.parse_sqlite_rate_limit_log_body,
            ) as parse_body:
                second = reader.latest_snapshot(now=0.5)

            self.assertEqual(first, second)
            self.assertEqual(parse_body.call_count, 0)
            self.assertEqual(reader._last_row_id, 1)

    def test_appended_rows_are_consumed_once(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "logs_2.sqlite"
            create_logs_db(path, [self.row(1_000, 20)])
            reader = overlay.SqliteRateLimitReader(path)
            reader.latest_snapshot(force_rescan=True, now=0)
            append_logs_db(path, [self.row(1_001, 30)])

            with mock.patch.object(
                overlay,
                "parse_sqlite_rate_limit_log_body",
                wraps=overlay.parse_sqlite_rate_limit_log_body,
            ) as parse_body:
                updated = reader.latest_snapshot(now=0.5)
                unchanged = reader.latest_snapshot(now=1.0)

            self.assertEqual(updated.primary.remaining_percent, 70)
            self.assertEqual(unchanged, updated)
            self.assertEqual(parse_body.call_count, 1)
            self.assertEqual(reader._last_row_id, 2)

    def test_malformed_row_advances_checkpoint_and_later_valid_row_wins(self):
        malformed = (
            'stream_request: websocket event: '
            '{"type":"codex.rate_limits","rate_limits"'
        )
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "logs_2.sqlite"
            create_logs_db(path, [self.row(1_000, 20)])
            reader = overlay.SqliteRateLimitReader(path)
            original = reader.latest_snapshot(force_rescan=True, now=0)
            append_logs_db(path, [self.row(1_001, 0, body=malformed)])

            rejected = reader.latest_snapshot(now=0.5)
            checkpoint = reader._last_row_id
            append_logs_db(path, [self.row(1_002, 45)])
            updated = reader.latest_snapshot(now=1.0)

            self.assertEqual(rejected, original)
            self.assertEqual(checkpoint, 2)
            self.assertEqual(updated.primary.remaining_percent, 55)
            self.assertEqual(reader._last_row_id, 3)

    def test_irrelevant_target_advances_checkpoint_without_reprocessing(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "logs_2.sqlite"
            create_logs_db(path, [self.row(1_000, 20)])
            reader = overlay.SqliteRateLimitReader(path)
            original = reader.latest_snapshot(force_rescan=True, now=0)
            append_logs_db(
                path,
                [self.row(1_001, 90, target="codex_otel.log_only")],
            )

            with mock.patch.object(
                overlay,
                "parse_sqlite_rate_limit_log_body",
                wraps=overlay.parse_sqlite_rate_limit_log_body,
            ) as parse_body:
                rejected = reader.latest_snapshot(now=0.5)
                unchanged = reader.latest_snapshot(now=1.0)

            self.assertEqual(rejected, original)
            self.assertEqual(unchanged, original)
            self.assertEqual(reader._last_row_id, 2)
            self.assertEqual(parse_body.call_count, 0)

    def test_database_replacement_resets_checkpoint(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            directory = Path(temp_dir)
            path = directory / "logs_2.sqlite"
            replacement = directory / "replacement.sqlite"
            create_logs_db(path, [self.row(1_000, 20), self.row(1_001, 30)])
            reader = overlay.SqliteRateLimitReader(path)
            reader.latest_snapshot(force_rescan=True, now=0)
            create_logs_db(replacement, [self.row(1_002, 40)])
            os.replace(replacement, path)

            updated = reader.latest_snapshot(now=0.5)

            self.assertEqual(updated.primary.remaining_percent, 60)
            self.assertEqual(reader._last_row_id, 1)

    def test_row_id_rollback_triggers_full_recovery_scan(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "logs_2.sqlite"
            create_logs_db(
                path,
                [
                    self.row(1_000, 20),
                    self.row(1_001, 30),
                    self.row(1_002, 40),
                ],
            )
            reader = overlay.SqliteRateLimitReader(path)
            reader.latest_snapshot(force_rescan=True, now=0)
            connection = sqlite3.connect(path)
            try:
                connection.execute("DELETE FROM logs")
                connection.execute("DELETE FROM sqlite_sequence WHERE name = 'logs'")
                connection.execute(
                    "INSERT INTO logs (ts, target, feedback_log_body) VALUES (?, ?, ?)",
                    self.row(1_003, 55),
                )
                connection.commit()
            finally:
                connection.close()

            recovered = reader.latest_snapshot(now=0.5)

            self.assertEqual(recovered.primary.remaining_percent, 45)
            self.assertEqual(reader._last_row_id, 1)

    def test_sqlite_error_preserves_checkpoint_and_retries(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "logs_2.sqlite"
            create_logs_db(path, [self.row(1_000, 20)])
            reader = overlay.SqliteRateLimitReader(path)
            original = reader.latest_snapshot(force_rescan=True, now=0)
            append_logs_db(path, [self.row(1_001, 35)])
            checkpoint = reader._last_row_id

            with mock.patch.object(
                overlay.sqlite3,
                "connect",
                side_effect=sqlite3.OperationalError("busy"),
            ):
                failed = reader.latest_snapshot(now=0.5)

            recovered = reader.latest_snapshot(now=1.0)

            self.assertEqual(failed, original)
            self.assertEqual(checkpoint, 1)
            self.assertEqual(recovered.primary.remaining_percent, 65)
            self.assertEqual(reader._last_row_id, 2)

    def test_fallback_probe_detects_update_when_file_signature_is_stale(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "logs_2.sqlite"
            create_logs_db(path, [self.row(1_000, 20)])
            reader = overlay.SqliteRateLimitReader(path)
            original = reader.latest_snapshot(force_rescan=True, now=0)
            stale_signature = reader._database_signature
            append_logs_db(path, [self.row(1_001, 50)])

            with mock.patch.object(
                reader,
                "_current_database_signature",
                return_value=stale_signature,
            ):
                before_fallback = reader.latest_snapshot(now=1)
                after_fallback = reader.latest_snapshot(
                    now=overlay.SQLITE_FALLBACK_PROBE_INTERVAL_SECONDS,
                )

            self.assertEqual(before_fallback, original)
            self.assertEqual(after_fallback.primary.remaining_percent, 50)
            self.assertEqual(reader._last_row_id, 2)


class CountingRateLogReader(overlay.RateLogReader):
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self.discovery_calls = 0

    def _find_session_files(self):
        self.discovery_calls += 1
        return super()._find_session_files()


class SessionDiscoveryCacheTests(unittest.TestCase):
    def write_snapshot(self, path, timestamp, used_percent):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(
            token_count_line(
                timestamp,
                primary={"used_percent": used_percent, "window_minutes": 300},
                secondary=None,
            )
            + "\n",
            encoding="utf-8",
        )

    def test_known_file_append_does_not_repeat_recursive_discovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            path = home / "sessions" / "2026" / "06" / "01" / "rollout.jsonl"
            self.write_snapshot(path, "2026-06-01T12:00:00Z", 50)
            reader = CountingRateLogReader(home)
            first = reader.latest_snapshot(force_rescan=True, now=0)
            with path.open("a", encoding="utf-8") as handle:
                handle.write(
                    token_count_line(
                        "2026-06-01T12:00:01Z",
                        primary={"used_percent": 40, "window_minutes": 300},
                        secondary=None,
                    )
                    + "\n"
                )

            second = reader.latest_snapshot(now=1)

            self.assertEqual(first.primary.remaining_percent, 50)
            self.assertEqual(second.primary.remaining_percent, 60)
            self.assertEqual(reader.discovery_calls, 1)

    def test_hot_directory_finds_new_file_without_recursive_discovery(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            directory = home / "sessions" / "2026" / "06" / "01"
            self.write_snapshot(
                directory / "old.jsonl",
                "2026-06-01T12:00:00Z",
                50,
            )
            reader = CountingRateLogReader(home)
            reader.latest_snapshot(force_rescan=True, now=0)
            self.write_snapshot(
                directory / "new.jsonl",
                "2026-06-01T12:00:02Z",
                30,
            )

            updated = reader.latest_snapshot(now=1)

            self.assertEqual(updated.primary.remaining_percent, 70)
            self.assertEqual(reader.discovery_calls, 1)

    def test_recursive_discovery_runs_at_fallback_boundary(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            self.write_snapshot(
                home / "sessions" / "2026" / "06" / "01" / "old.jsonl",
                "2026-06-01T12:00:00Z",
                50,
            )
            reader = CountingRateLogReader(home)
            original = reader.latest_snapshot(force_rescan=True, now=0)
            self.write_snapshot(
                home / "sessions" / "2025" / "01" / "01" / "new.jsonl",
                "2026-06-01T12:00:03Z",
                25,
            )

            before_boundary = reader.latest_snapshot(
                now=overlay.SESSION_FULL_RESCAN_INTERVAL_SECONDS - 1,
            )
            after_boundary = reader.latest_snapshot(
                now=overlay.SESSION_FULL_RESCAN_INTERVAL_SECONDS,
            )

            self.assertEqual(original, before_boundary)
            self.assertEqual(after_boundary.primary.remaining_percent, 75)
            self.assertEqual(reader.discovery_calls, 2)


class HistoricalSqliteRateLimitReader(overlay.SqliteRateLimitReader):
    def latest_snapshot(self, row_limit=overlay.SQLITE_RATE_ROWS_TO_SCAN, force_rescan=False, now=None):
        return super().latest_snapshot(
            row_limit=row_limit,
            force_rescan=True,
            now=now,
        )


class HistoricalRateLogReader(overlay.RateLogReader):
    def __init__(self, codex_home):
        super().__init__(codex_home)
        self.sqlite_reader = HistoricalSqliteRateLimitReader(
            self.codex_home / "logs_2.sqlite",
        )

    def _refresh_session_files(self, force_rescan, now):
        self._last_full_session_scan_at = now
        return self._find_session_files()[:overlay.MAX_SESSION_FILES_TO_SCAN]


class TelemetryReplayEquivalenceTests(unittest.TestCase):
    @staticmethod
    def usage(total_tokens):
        return {
            "input_tokens": total_tokens - 20,
            "cached_input_tokens": 10,
            "output_tokens": 20,
            "reasoning_output_tokens": 5,
            "total_tokens": total_tokens,
        }

    @staticmethod
    def estimate(counter):
        detected_model = overlay.DetectedModel("gpt-5.5", "test")
        return overlay.estimate_api_cost(
            counter.totals,
            detected_model,
            detected_model.model,
            short_context_usage=counter.short_context_totals,
            long_context_usage=counter.long_context_totals,
            long_context_request_count=counter.long_context_request_count,
        )

    def test_historical_and_adaptive_readers_replay_identically(self):
        target = "codex_api::endpoint::responses_websocket"
        with tempfile.TemporaryDirectory() as temp_dir:
            home = Path(temp_dir)
            session_path = home / "sessions" / "2026" / "06" / "02" / "rollout.jsonl"
            session_path.parent.mkdir(parents=True)
            session_path.write_text(
                token_count_line(
                    "2026-06-02T17:20:40Z",
                    primary={"used_percent": 10, "window_minutes": 300},
                    secondary={"used_percent": 5, "window_minutes": 10080},
                    last_usage=self.usage(100),
                )
                + "\n",
                encoding="utf-8",
            )
            create_logs_db(
                home / "logs_2.sqlite",
                [
                    (
                        int(overlay.timestamp_to_epoch("2026-06-02T17:20:41Z")),
                        target,
                        websocket_rate_log(
                            primary={"used_percent": 20, "window_minutes": 300},
                            secondary={"used_percent": 6, "window_minutes": 10080},
                        ),
                    )
                ],
            )

            historical = HistoricalRateLogReader(home)
            adaptive = overlay.RateLogReader(home)
            historical_counter = overlay.TokenCounter(reset_at=0)
            adaptive_counter = overlay.TokenCounter(reset_at=0)

            def compare_poll(now, force=False):
                historical_batch = historical.read_updates(force_rescan=force, now=now)
                adaptive_batch = adaptive.read_updates(force_rescan=force, now=now)
                historical_counter.add_events(historical_batch.token_events, now=now)
                adaptive_counter.add_events(adaptive_batch.token_events, now=now)
                self.assertEqual(adaptive_batch.snapshot, historical_batch.snapshot)
                self.assertEqual(adaptive_counter.state_dict(), historical_counter.state_dict())
                self.assertEqual(
                    self.estimate(adaptive_counter),
                    self.estimate(historical_counter),
                )

            compare_poll(0, force=True)

            partial_line = token_count_line(
                "2026-06-02T17:20:42Z",
                primary={"used_percent": 30, "window_minutes": 300},
                secondary={"used_percent": 7, "window_minutes": 10080},
                last_usage=self.usage(200),
            )
            split_at = len(partial_line) // 2
            with session_path.open("a", encoding="utf-8") as handle:
                handle.write("{bad json}\n")
                handle.write(partial_line[:split_at])
            append_logs_db(
                home / "logs_2.sqlite",
                [
                    (
                        int(overlay.timestamp_to_epoch("2026-06-02T17:20:42Z")),
                        "codex_otel.log_only",
                        websocket_rate_log(
                            primary={"used_percent": 99, "window_minutes": 300},
                            secondary=None,
                        ),
                    ),
                    (
                        int(overlay.timestamp_to_epoch("2026-06-02T17:20:42Z")),
                        target,
                        'stream_request: websocket event: '
                        '{"type":"codex.rate_limits","rate_limits"',
                    ),
                ],
            )
            compare_poll(1)

            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(partial_line[split_at:] + "\n")
            append_logs_db(
                home / "logs_2.sqlite",
                [
                    (
                        int(overlay.timestamp_to_epoch("2026-06-02T17:20:43Z")),
                        target,
                        websocket_rate_log(
                            primary={"used_percent": 40, "window_minutes": 300},
                            secondary={"used_percent": 8, "window_minutes": 10080},
                        ),
                    )
                ],
            )
            compare_poll(2)

            with session_path.open("a", encoding="utf-8") as handle:
                handle.write(
                    token_count_line(
                        "2026-06-02T17:20:44Z",
                        primary={"used_percent": 50, "window_minutes": 300},
                        secondary={"used_percent": 9, "window_minutes": 10080},
                        last_usage=self.usage(300),
                    )
                    + "\n"
                )
            compare_poll(3)

            self.assertEqual(adaptive_counter.totals.total_tokens, 600)
            adaptive_snapshot = adaptive.latest_snapshot(now=4)
            historical_snapshot = historical.latest_snapshot(now=4)
            self.assertEqual(adaptive_snapshot, historical_snapshot)
            self.assertEqual(adaptive_snapshot.source_kind, "session_jsonl")


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
        self.assertEqual(counter.short_context_totals.total_tokens, 0)
        self.assertEqual(counter.long_context_totals.total_tokens, 0)
        self.assertEqual(counter.long_context_request_count, 0)
        self.assertEqual(counter.seen_events, set())

    def test_classifies_272000_as_short_and_272001_as_long_context(self):
        counter = overlay.TokenCounter(reset_at=1_000)

        counter.add_events(
            [
                self.event("1970-01-01T00:16:41Z", 272_000, fingerprint="boundary-short"),
                self.event("1970-01-01T00:16:42Z", 272_001, fingerprint="boundary-long"),
            ],
            now=1_003,
        )

        self.assertEqual(counter.short_context_totals.input_tokens, 272_000)
        self.assertEqual(counter.long_context_totals.input_tokens, 272_001)
        self.assertEqual(counter.long_context_request_count, 1)

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
        self.assertIsNone(estimate.pricing_model)
        self.assertFalse(estimate.pricing_is_proxy)
        self.assertIsNone(estimate.total_cost)
        self.assertEqual(overlay.format_api_cost_estimate(estimate), "API est. --")

    def test_uses_exact_gpt_54_mini_pricing(self):
        estimate = overlay.estimate_api_cost(
            overlay.TokenUsage(
                input_tokens=1_000_000,
                cached_input_tokens=200_000,
                output_tokens=1_000_000,
                total_tokens=2_000_000,
            ),
            overlay.DetectedModel("gpt-5.4-mini", "test"),
        )

        self.assertEqual(estimate.pricing_model, "gpt-5.4-mini")
        self.assertFalse(estimate.pricing_is_proxy)
        self.assertAlmostEqual(estimate.input_cost, 0.60)
        self.assertAlmostEqual(estimate.cached_input_cost, 0.015)
        self.assertAlmostEqual(estimate.output_cost, 4.50)
        self.assertAlmostEqual(estimate.total_cost, 5.115)

    def test_gpt_56_models_use_exact_published_standard_rates(self):
        expected = {
            "gpt-5.6-sol": (5.00, 0.50, 6.25, 30.00, 10.00, 1.00, 12.50, 45.00),
            "gpt-5.6-terra": (2.50, 0.25, 3.125, 15.00, 5.00, 0.50, 6.25, 22.50),
            "gpt-5.6-luna": (1.00, 0.10, 1.25, 6.00, 2.00, 0.20, 2.50, 9.00),
        }

        for model, rates in expected.items():
            with self.subTest(model=model):
                resolution = overlay.resolve_model_pricing(model)
                self.assertIsNotNone(resolution)
                self.assertEqual(resolution.pricing_model, model)
                self.assertFalse(resolution.is_proxy)
                pricing = resolution.pricing
                self.assertEqual(
                    (
                        pricing.input_per_million,
                        pricing.cached_input_per_million,
                        pricing.cache_write_per_million,
                        pricing.output_per_million,
                        pricing.long_context_input_per_million,
                        pricing.long_context_cached_input_per_million,
                        pricing.long_context_cache_write_per_million,
                        pricing.long_context_output_per_million,
                    ),
                    rates,
                )
                self.assertEqual(
                    pricing.long_context_threshold_tokens,
                    overlay.LONG_CONTEXT_INPUT_THRESHOLD_TOKENS,
                )

    def test_gpt_56_alias_resolves_to_sol_without_proxy(self):
        estimate = overlay.estimate_api_cost(
            overlay.TokenUsage(input_tokens=1_000, output_tokens=1_000, total_tokens=2_000),
            overlay.DetectedModel("gpt-5.6", "test"),
        )

        self.assertEqual(overlay.normalize_model_key("gpt-5.6"), "gpt-5.6-sol")
        self.assertEqual(estimate.pricing_model, "gpt-5.6-sol")
        self.assertFalse(estimate.pricing_is_proxy)

    def test_only_spark_uses_labeled_gpt_55_proxy(self):
        estimate = overlay.estimate_api_cost(
            overlay.TokenUsage(input_tokens=1_000, output_tokens=1_000, total_tokens=2_000),
            overlay.DetectedModel("gpt-5.3-codex-spark", "test"),
        )

        self.assertEqual(estimate.pricing_model, "gpt-5.5")
        self.assertTrue(estimate.pricing_is_proxy)
        self.assertIn("GPT-5.5 proxy", overlay.format_api_cost_estimate(estimate))

    def test_mixed_context_cost_uses_per_request_rate_buckets(self):
        short_usage = overlay.TokenUsage(
            input_tokens=100_000,
            cached_input_tokens=40_000,
            output_tokens=10_000,
            total_tokens=110_000,
        )
        long_usage = overlay.TokenUsage(
            input_tokens=300_000,
            cached_input_tokens=100_000,
            output_tokens=20_000,
            total_tokens=320_000,
        )
        total_usage = overlay.add_token_usage(short_usage, long_usage)

        estimate = overlay.estimate_api_cost(
            total_usage,
            overlay.DetectedModel("gpt-5.6-sol", "test"),
            short_context_usage=short_usage,
            long_context_usage=long_usage,
            long_context_request_count=1,
        )

        self.assertAlmostEqual(estimate.input_cost, 2.30)
        self.assertAlmostEqual(estimate.cached_input_cost, 0.12)
        self.assertAlmostEqual(estimate.output_cost, 1.20)
        self.assertAlmostEqual(estimate.total_cost, 3.62)
        self.assertEqual(estimate.long_context_request_count, 1)
        self.assertIsNone(estimate.cache_write_cost)

    def test_cached_tokens_are_clamped_within_each_context_bucket(self):
        short_usage = overlay.TokenUsage(input_tokens=100, cached_input_tokens=200)
        long_usage = overlay.TokenUsage(input_tokens=300_000, cached_input_tokens=400_000)

        estimate = overlay.estimate_api_cost(
            overlay.add_token_usage(short_usage, long_usage),
            overlay.DetectedModel("gpt-5.6-luna", "test"),
            short_context_usage=short_usage,
            long_context_usage=long_usage,
            long_context_request_count=1,
        )

        self.assertEqual(estimate.uncached_input_tokens, 0)
        self.assertEqual(estimate.cached_input_tokens, 300_100)
        self.assertAlmostEqual(estimate.cached_input_cost, 0.06001)

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
        self.assertEqual(overlay.normalize_model_key("gpt-5.6"), "gpt-5.6-sol")


class RuntimeStateTests(unittest.TestCase):
    def test_writes_expected_state_shape(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "state.json"
            store = overlay.RuntimeStateStore(path=path, pid=123)
            counter = overlay.TokenCounter(reset_at=1_000)
            estimate = overlay.estimate_api_cost(
                overlay.TokenUsage(input_tokens=1_000, output_tokens=1_000, total_tokens=2_000),
                overlay.DetectedModel("gpt-5.6-sol", "test"),
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

            self.assertTrue(store.write(snapshot, counter, estimate))
            state = json.loads(path.read_text(encoding="utf-8"))

            self.assertEqual(state["app"], overlay.APP_NAME)
            self.assertEqual(state["pid"], 123)
            self.assertIn("token_counter", state)
            self.assertIn("last_rate_snapshot", state)
            self.assertIn("api_cost_estimate", state)
            self.assertEqual(state["api_cost_estimate"]["model"], "gpt-5.6-sol")
            self.assertEqual(state["api_cost_estimate"]["pricing_model"], "gpt-5.6-sol")
            self.assertFalse(state["api_cost_estimate"]["pricing_is_proxy"])
            self.assertEqual(
                state["api_cost_estimate"]["pricing"]["long_context_threshold_tokens"],
                272_000,
            )
            self.assertEqual(
                state["api_cost_estimate"]["pricing"]["cache_write_per_million"],
                6.25,
            )
            self.assertEqual(
                state["api_cost_estimate"]["pricing"]["long_context_cache_write_per_million"],
                12.50,
            )
            self.assertIsNone(state["api_cost_estimate"]["cache_write_cost"])
            self.assertFalse(state["api_cost_estimate"]["cache_write_cost_included"])
            self.assertIn("do not report cache-write tokens", state["api_cost_estimate"]["cache_write_note"])
            self.assertIn("short_context_totals", state["token_counter"])
            self.assertIn("long_context_totals", state["token_counter"])
            self.assertEqual(state["token_counter"]["long_context_request_count"], 0)
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

    def test_write_failure_is_reported_for_scheduler_retry(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            blocked_parent = Path(temp_dir) / "not-a-directory"
            blocked_parent.write_text("blocked", encoding="utf-8")
            store = overlay.RuntimeStateStore(
                path=blocked_parent / "state.json",
                pid=123,
            )

            self.assertFalse(
                store.write(None, overlay.TokenCounter(reset_at=1_000))
            )


class ProcessIdentityTests(unittest.TestCase):
    def test_classifies_legacy_and_packaged_codex_processes(self):
        cases = (
            ("codex.exe", None, True),
            ("CODEX.EXE", "", True),
            (
                "ChatGPT.exe",
                r"C:\Program Files\WindowsApps\OpenAI.Codex_26.707.3563.0_x64__2p2nqsd0c76g\ChatGPT.exe",
                True,
            ),
            (
                "CHATGPT.EXE",
                r"c:\program files\windowsapps\OPENAI.CODEX_26.707.3563.0_X64__2P2NQSD0C76G\CHATGPT.EXE",
                True,
            ),
            (
                "ChatGPT.exe",
                r"C:\Program Files\WindowsApps\OpenAI.ChatGPT_2.0_x64__example\ChatGPT.exe",
                False,
            ),
            ("ChatGPT.exe", r"C:\Apps\ChatGPT.exe", False),
            (
                "notepad.exe",
                r"C:\Program Files\WindowsApps\OpenAI.Codex_26.707.3563.0_x64__2p2nqsd0c76g\notepad.exe",
                False,
            ),
        )

        for process_name, executable_path, expected in cases:
            with self.subTest(process_name=process_name, executable_path=executable_path):
                self.assertEqual(
                    overlay.is_codex_windows_process(process_name, executable_path),
                    expected,
                )

    def test_packaged_chatgpt_pid_fails_closed_when_path_is_unavailable(self):
        original_process_path = overlay.windows_process_path
        overlay.windows_process_path = lambda _pid: ""
        try:
            self.assertFalse(overlay.windows_pid_is_codex(123, "ChatGPT.exe"))
            self.assertTrue(overlay.windows_pid_is_codex(123, "codex.exe"))
        finally:
            overlay.windows_process_path = original_process_path

    def test_process_backend_routes_each_windows_visibility_mode(self):
        backend = overlay.ProcessBackend()
        calls = []
        original_system = overlay.platform.system
        original_pids = overlay.windows_codex_pids
        original_foreground = overlay.windows_foreground_is_codex
        original_visible = overlay.windows_has_visible_codex_window
        overlay.platform.system = lambda: "Windows"
        overlay.windows_codex_pids = lambda: calls.append("process") or {123}
        overlay.windows_foreground_is_codex = lambda: calls.append("foreground") or True
        overlay.windows_has_visible_codex_window = lambda: calls.append("visible_window") or True
        try:
            self.assertTrue(backend.should_show("process"))
            self.assertTrue(backend.should_show("foreground"))
            self.assertTrue(backend.should_show("visible_window"))
        finally:
            overlay.platform.system = original_system
            overlay.windows_codex_pids = original_pids
            overlay.windows_foreground_is_codex = original_foreground
            overlay.windows_has_visible_codex_window = original_visible

        self.assertEqual(calls, ["process", "foreground", "visible_window"])


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

    def test_popup_near_top_left_uses_screen_padding(self):
        position = overlay.clamp_popup_position([0, 0], (0, 0, 1920, 1080), (200, 100))

        self.assertEqual(position, [overlay.MENU_SCREEN_PADDING, overlay.MENU_SCREEN_PADDING])

    def test_popup_near_bottom_right_flips_left_and_up(self):
        position = overlay.clamp_popup_position([1900, 1070], (0, 0, 1920, 1080), (200, 100))

        self.assertEqual(position, [1700, 970])

    def test_popup_supports_negative_monitor_coordinates(self):
        position = overlay.clamp_popup_position([-20, 100], (-1920, 0, 1920, 1080), (200, 100))

        self.assertEqual(position, [-220, 100])

    def test_oversized_popup_starts_at_usable_top_left(self):
        position = overlay.clamp_popup_position([250, 180], (0, 0, 300, 200), (500, 300))

        self.assertEqual(position, [overlay.MENU_SCREEN_PADDING, overlay.MENU_SCREEN_PADDING])
        self.assertEqual(overlay.popup_max_size((0, 0, 300, 200)), (284, 184))


class MonitorAwarePositionTests(unittest.TestCase):
    def monitor(
        self,
        device_name,
        monitor_bounds,
        work_area=None,
        is_primary=False,
    ):
        return overlay.MonitorWorkArea(
            device_name=device_name,
            monitor_bounds=monitor_bounds,
            work_area=work_area or monitor_bounds,
            is_primary=is_primary,
        )

    def laptop_monitor(self):
        return self.monitor(
            r"\\.\DISPLAY1",
            (0, 0, 1920, 1200),
            (0, 0, 1920, 1152),
            is_primary=True,
        )

    def external_monitor(self):
        return self.monitor(
            r"\\.\DISPLAY2",
            (1920, 0, 2560, 1440),
            (1920, 0, 2560, 1400),
        )

    def test_stale_external_position_moves_into_laptop_work_area(self):
        position = overlay.normalize_overlay_position_for_monitors(
            [3895, 940],
            [self.laptop_monitor()],
            (190, 40),
        )

        self.assertEqual(position, [1730, 940])

    def test_valid_secondary_monitor_position_is_unchanged(self):
        monitors = [self.laptop_monitor(), self.external_monitor()]

        position = overlay.normalize_overlay_position_for_monitors(
            [3895, 940],
            monitors,
            (190, 40),
        )
        selected = overlay.choose_monitor_for_overlay([3895, 940], (190, 40), monitors)

        self.assertEqual(position, [3895, 940])
        self.assertEqual(selected.device_name, r"\\.\DISPLAY2")

    def test_position_in_virtual_screen_hole_moves_to_nearest_work_area(self):
        monitors = [
            self.laptop_monitor(),
            self.monitor(
                r"\\.\DISPLAY2",
                (1920, -1440, 2560, 1440),
                (1920, -1440, 2560, 1400),
            ),
        ]

        selected = overlay.choose_monitor_for_overlay([100, -500], (190, 40), monitors)
        position = overlay.normalize_overlay_position_for_monitors(
            [100, -500],
            monitors,
            (190, 40),
        )

        self.assertEqual(selected.device_name, r"\\.\DISPLAY1")
        self.assertEqual(position, [100, 0])

    def test_negative_secondary_monitor_coordinates_remain_valid(self):
        monitors = [
            self.laptop_monitor(),
            self.monitor(
                r"\\.\DISPLAY2",
                (-1920, 0, 1920, 1080),
                (-1920, 0, 1920, 1040),
            ),
        ]

        position = overlay.normalize_overlay_position_for_monitors(
            [-1800, 100],
            monitors,
            (190, 40),
        )

        self.assertEqual(position, [-1800, 100])

    def test_position_in_taskbar_strip_is_clamped_to_work_area(self):
        position = overlay.normalize_overlay_position_for_monitors(
            [1730, 1160],
            [self.laptop_monitor()],
            (190, 40),
        )

        self.assertEqual(position, [1730, 1112])

    def test_monitor_selection_uses_nearest_then_primary_for_ties(self):
        primary = self.monitor(r"\\.\DISPLAY1", (0, 0, 1000, 1000), is_primary=True)
        secondary = self.monitor(r"\\.\DISPLAY2", (2000, 0, 1000, 1000))
        monitors = [secondary, primary]

        nearest = overlay.choose_monitor_for_overlay([1750, 100], (100, 100), monitors)
        tied = overlay.choose_monitor_for_overlay([1450, 100], (100, 100), monitors)

        self.assertEqual(nearest.device_name, r"\\.\DISPLAY2")
        self.assertEqual(tied.device_name, r"\\.\DISPLAY1")

    def test_oversized_overlay_keeps_a_recoverable_region_visible(self):
        monitor = self.monitor(r"\\.\DISPLAY1", (0, 0, 300, 200), is_primary=True)

        position = overlay.normalize_overlay_position_for_monitors(
            [5000, 5000],
            [monitor],
            (500, 300),
        )

        visible_width = max(0, min(position[0] + 500, 300) - max(position[0], 0))
        visible_height = max(0, min(position[1] + 300, 200) - max(position[1], 0))
        self.assertGreaterEqual(visible_width, overlay.MIN_VISIBLE_PIXELS)
        self.assertGreaterEqual(visible_height, overlay.MIN_VISIBLE_PIXELS)

    def test_topology_fingerprint_is_order_independent_and_includes_work_area(self):
        primary = self.laptop_monitor()
        secondary = self.external_monitor()
        changed_work_area = self.monitor(
            secondary.device_name,
            secondary.monitor_bounds,
            (1920, 0, 2560, 1360),
        )

        original = overlay.monitor_topology_fingerprint([primary, secondary])
        reordered = overlay.monitor_topology_fingerprint([secondary, primary])
        changed = overlay.monitor_topology_fingerprint([primary, changed_work_area])

        self.assertEqual(original, reordered)
        self.assertNotEqual(original, changed)

    def test_native_message_classifier_covers_display_work_area_dpi_and_resume(self):
        self.assertTrue(overlay.is_display_reconcile_message(0x007E))
        self.assertTrue(overlay.is_display_reconcile_message(0x001A, 0x002F))
        self.assertTrue(overlay.is_display_reconcile_message(0x02E0))
        self.assertTrue(overlay.is_display_reconcile_message(0x0218, 0x0012))
        self.assertTrue(overlay.is_display_reconcile_message(0x0218, 0x0007))
        self.assertTrue(overlay.is_display_reconcile_message(0x0218, 0x0006))
        self.assertTrue(overlay.is_display_reconcile_message(0x0219, 0x0007))
        self.assertTrue(overlay.is_display_reconcile_message(0x0219, 0x0018))

        self.assertTrue(overlay.is_display_reconcile_message(0x001A, 0))
        self.assertTrue(overlay.is_display_reconcile_message(0x0219, 0x8000))
        self.assertFalse(overlay.is_display_reconcile_message(0x0218, 0x0004))
        self.assertFalse(overlay.is_display_reconcile_message(0x000F))


class NativeDisplayObserverTests(unittest.TestCase):
    class FakeCommonControls:
        def __init__(self):
            self.forwarded = []
            self.removed = []

        def DefSubclassProc(self, hwnd, message, wparam, lparam):
            self.forwarded.append((hwnd, message, wparam, lparam))
            return 73

        def RemoveWindowSubclass(self, hwnd, proc, subclass_id):
            self.removed.append((hwnd, proc, int(subclass_id.value)))
            return True

    def make_observer(self):
        observer = object.__new__(overlay.WindowsDisplayChangeObserver)
        observer._hwnd = 123
        observer._pending = set()
        observer._callback_error = None
        observer._proc = object()
        observer._comctl32 = self.FakeCommonControls()
        return observer

    def test_callback_records_only_relevant_messages_and_always_forwards(self):
        observer = self.make_observer()

        relevant_result = observer._subclass_proc(
            123,
            overlay.WM_DISPLAYCHANGE,
            0,
            0,
            overlay.WindowsDisplayChangeObserver.SUBCLASS_ID,
            0,
        )
        unrelated_result = observer._subclass_proc(
            123,
            0x000F,
            0,
            0,
            overlay.WindowsDisplayChangeObserver.SUBCLASS_ID,
            0,
        )

        self.assertEqual(relevant_result, 73)
        self.assertEqual(unrelated_result, 73)
        self.assertEqual(observer.take_pending(), {(overlay.WM_DISPLAYCHANGE, 0)})
        self.assertEqual(len(observer._comctl32.forwarded), 2)
        self.assertIsNone(observer._callback_error)

    def test_nc_destroy_removes_subclass_and_clears_wrapper_handle(self):
        observer = self.make_observer()

        result = observer._subclass_proc(
            123,
            overlay.WM_NCDESTROY,
            0,
            0,
            overlay.WindowsDisplayChangeObserver.SUBCLASS_ID,
            0,
        )

        self.assertEqual(result, 73)
        self.assertEqual(observer._hwnd, 0)
        self.assertEqual(len(observer._comctl32.removed), 1)
        self.assertEqual(len(observer._comctl32.forwarded), 1)


class FakeDisplayRoot:
    def __init__(self, position=(3895, 940), size=(190, 40), withdrawn=False):
        self.position = [int(position[0]), int(position[1])]
        self.size = size
        self.withdrawn = withdrawn
        self.geometry_calls = []
        self.deiconify_calls = 0
        self.withdraw_calls = 0
        self.attribute_calls = []
        self.events = []
        self.after_callbacks = {}
        self.after_cancel_calls = []
        self._next_after_identifier = 1
        self.idle_update_calls = 0
        self.pointer_query_calls = 0

    def update_idletasks(self):
        self.idle_update_calls += 1

    def winfo_x(self):
        return self.position[0]

    def winfo_y(self):
        return self.position[1]

    def winfo_width(self):
        return self.size[0]

    def winfo_reqwidth(self):
        return self.size[0]

    def winfo_height(self):
        return self.size[1]

    def winfo_reqheight(self):
        return self.size[1]

    def winfo_pointerx(self):
        self.pointer_query_calls += 1
        return self.position[0]

    def winfo_pointery(self):
        self.pointer_query_calls += 1
        return self.position[1]

    def geometry(self, value):
        split_at = next(index for index in range(1, len(value)) if value[index] in "+-")
        self.position = [int(value[:split_at]), int(value[split_at:])]
        self.geometry_calls.append(value)
        self.events.append(("geometry", tuple(self.position)))

    def deiconify(self):
        self.withdrawn = False
        self.deiconify_calls += 1
        self.events.append(("deiconify", None))

    def withdraw(self):
        self.withdrawn = True
        self.withdraw_calls += 1
        self.events.append(("withdraw", None))

    def attributes(self, *args):
        self.attribute_calls.append(args)

    def after(self, _delay, callback):
        identifier = f"after-{self._next_after_identifier}"
        self._next_after_identifier += 1
        self.after_callbacks[identifier] = callback
        return identifier

    def after_cancel(self, identifier):
        self.after_cancel_calls.append(identifier)
        self.after_callbacks.pop(identifier, None)


class DisplayTopologyLifecycleTests(unittest.TestCase):
    def monitor(
        self,
        device_name,
        monitor_bounds,
        work_area=None,
        is_primary=False,
    ):
        return overlay.MonitorWorkArea(
            device_name=device_name,
            monitor_bounds=monitor_bounds,
            work_area=work_area or monitor_bounds,
            is_primary=is_primary,
        )

    def laptop_monitor(self):
        return self.monitor(
            r"\\.\DISPLAY1",
            (0, 0, 1920, 1200),
            (0, 0, 1920, 1152),
            is_primary=True,
        )

    def external_monitor(self):
        return self.monitor(
            r"\\.\DISPLAY2",
            (1920, 0, 2560, 1440),
            (1920, 0, 2560, 1400),
        )

    def test_drag_motion_uses_cached_monitor_snapshot(self):
        laptop = self.laptop_monitor()
        app = self.make_app(position=(100, 100), stable_monitors=[laptop])
        app._stable_monitors = (laptop,)
        app._last_good_monitors = (laptop,)
        event = type(
            "DragEvent",
            (),
            {"x_root": 120, "y_root": 130},
        )()

        with mock.patch.object(overlay, "windows_monitor_work_areas") as enumerate_monitors:
            overlay.OverlayApp.start_drag(app, event)
            idle_updates_before_motion = app.root.idle_update_calls
            event.x_root = 220
            event.y_root = 230
            overlay.OverlayApp.drag(app, event)

        enumerate_monitors.assert_not_called()
        self.assertEqual(app.root.position, [200, 200])
        self.assertEqual(app.root.idle_update_calls, idle_updates_before_motion)
        self.assertEqual(app.root.pointer_query_calls, 0)

    def test_drag_start_refreshes_cache_when_topology_is_dirty(self):
        laptop = self.laptop_monitor()
        external = self.external_monitor()
        app = self.make_app(position=(100, 100), stable_monitors=[laptop])
        app._stable_monitors = (laptop,)
        app._last_good_monitors = (laptop,)
        app._native_display_dirty = True
        app._native_display_dirty_at = 9.0
        event = type(
            "DragEvent",
            (),
            {"x_root": 120, "y_root": 130},
        )()

        with (
            mock.patch.object(overlay.time, "monotonic", return_value=10.0),
            mock.patch.object(
                overlay,
                "windows_monitor_work_areas",
                return_value=(external,),
            ) as enumerate_monitors,
        ):
            overlay.OverlayApp.start_drag(app, event)

        enumerate_monitors.assert_called_once()
        self.assertEqual(app._drag_monitors, (external,))
        self.assertEqual(
            app._next_display_poll_at,
            10.0 + overlay.DISPLAY_TOPOLOGY_SAMPLE_SECONDS,
        )

    def test_clean_drag_release_returns_to_display_fallback_deadline(self):
        laptop = self.laptop_monitor()
        app = self.make_app(position=(100, 100), stable_monitors=[laptop])
        app._stable_monitors = (laptop,)
        app._last_good_monitors = (laptop,)
        app.is_dragging = True
        app.drag_offset = (20, 30)

        with (
            mock.patch.object(overlay.time, "monotonic", return_value=20.0),
            mock.patch.object(
                overlay,
                "windows_monitor_work_areas",
                return_value=(laptop,),
            ) as enumerate_monitors,
        ):
            overlay.OverlayApp.end_drag(app, None)

        enumerate_monitors.assert_called_once()
        self.assertEqual(
            app._next_display_poll_at,
            20.0 + overlay.DISPLAY_TOPOLOGY_POLL_INTERVAL_SECONDS,
        )

    def make_app(
        self,
        position=(3895, 940),
        stable_monitors=None,
        should_show=True,
        withdrawn=False,
    ):
        app = object.__new__(overlay.OverlayApp)
        app.root = FakeDisplayRoot(position=position, withdrawn=withdrawn)
        app.settings = {
            "position": list(position),
            "visibility_mode": "visible_window",
        }
        app.process_backend = type(
            "DisplayBackend",
            (),
            {"should_show": lambda _self, _mode: should_show},
        )()
        app.is_dragging = False
        app.drag_offset = None
        app.menu_active = False
        app.menu_window = None
        app.menu_anchor = None
        app.needs_render_after_drag = False
        app.needs_render_after_menu = False
        app.needs_visibility_after_menu = False
        app._stable_monitor_fingerprint = (
            overlay.monitor_topology_fingerprint(stable_monitors)
            if stable_monitors is not None
            else None
        )
        app._pending_monitor_fingerprint = None
        app._pending_monitor_first_seen_at = 0.0
        app._pending_monitor_last_seen_at = 0.0
        app._pending_monitor_sample_count = 0
        app._native_display_dirty = False
        app._native_display_dirty_at = 0.0
        app._display_reconcile_deferred = False
        app._display_verification_after_id = None
        app._pending_drag_position = None
        app._display_observer = None
        app._startup_complete = True
        app._last_overlay_size = (190, 40)
        app.save_calls = 0
        app.save_settings = lambda: setattr(app, "save_calls", app.save_calls + 1)
        app.request_render = lambda force=False: None
        return app

    def test_changed_topology_requires_two_samples_then_corrects_and_persists_once(self):
        laptop = self.laptop_monitor()
        app = self.make_app(stable_monitors=[laptop, self.external_monitor()])

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=(laptop,)):
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=1.0))
            self.assertEqual(app.root.position, [3895, 940])
            self.assertEqual(app.save_calls, 0)

            self.assertTrue(overlay.OverlayApp._check_display_topology(app, now=1.25))
            self.assertEqual(app.root.position, [1730, 940])
            self.assertEqual(app.settings["position"], [1730, 940])
            self.assertEqual(app.save_calls, 1)

            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=1.5))

        self.assertEqual(app.root.geometry_calls, ["+1730+940"])
        self.assertEqual(app.save_calls, 1)

    def test_native_event_burst_uses_trailing_debounce_and_one_write(self):
        class PendingObserver:
            def __init__(self):
                self.pending = {
                    (overlay.WM_DISPLAYCHANGE, 0),
                    (overlay.WM_SETTINGCHANGE, overlay.SPI_SETWORKAREA),
                    (overlay.WM_DPICHANGED, 0),
                }

            def take_pending(self):
                pending = self.pending
                self.pending = set()
                return pending

        laptop = self.laptop_monitor()
        app = self.make_app(stable_monitors=[laptop, self.external_monitor()])
        app._display_observer = PendingObserver()

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=(laptop,)):
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=10.0))
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=10.25))
            self.assertTrue(overlay.OverlayApp._check_display_topology(app, now=10.5))
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=11.0))

        self.assertEqual(app.root.geometry_calls, ["+1730+940"])
        self.assertEqual(app.settings["position"], [1730, 940])
        self.assertEqual(app.save_calls, 1)

    def test_refresh_consumer_retries_hook_and_records_callback_failure(self):
        class RecoveringObserver:
            def __init__(self):
                self._hwnd = 0
                self.install_calls = 0

            def install_if_mapped(self):
                self.install_calls += 1
                return False

            def take_callback_error(self):
                return RuntimeError("native callback test failure")

            def take_pending(self):
                return {(overlay.WM_DISPLAYCHANGE, 0)}

        app = self.make_app(stable_monitors=[self.laptop_monitor()])
        observer = RecoveringObserver()
        app._display_observer = observer
        app._native_display_observer_error = None

        self.assertTrue(overlay.OverlayApp._consume_native_display_notifications(app, 12.0))

        self.assertEqual(observer.install_calls, 1)
        self.assertTrue(app._native_display_dirty)
        self.assertEqual(app._native_display_dirty_at, 12.0)
        self.assertIn("native callback test failure", app._native_display_observer_error)

    def test_empty_topology_does_not_move_or_persist(self):
        stable_monitors = [self.laptop_monitor(), self.external_monitor()]
        app = self.make_app(stable_monitors=stable_monitors)
        stable_fingerprint = app._stable_monitor_fingerprint

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=()):
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=2.0))

        self.assertEqual(app.root.position, [3895, 940])
        self.assertEqual(app.root.geometry_calls, [])
        self.assertEqual(app.settings["position"], [3895, 940])
        self.assertEqual(app.save_calls, 0)
        self.assertEqual(app._stable_monitor_fingerprint, stable_fingerprint)

    def test_first_valid_topology_after_empty_startup_also_requires_two_samples(self):
        laptop = self.laptop_monitor()
        app = self.make_app(stable_monitors=None)

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=(laptop,)):
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=2.0))
            self.assertEqual(app.root.position, [3895, 940])
            self.assertEqual(app.save_calls, 0)

            self.assertTrue(overlay.OverlayApp._check_display_topology(app, now=2.25))

        self.assertEqual(app.root.position, [1730, 940])
        self.assertEqual(app.settings["position"], [1730, 940])
        self.assertEqual(app.save_calls, 1)

    def test_hidden_visible_window_is_corrected_without_deiconifying(self):
        laptop = self.laptop_monitor()
        app = self.make_app(
            stable_monitors=[laptop, self.external_monitor()],
            should_show=False,
            withdrawn=True,
        )

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=(laptop,)):
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=3.0))
            self.assertTrue(overlay.OverlayApp._check_display_topology(app, now=3.25))

        self.assertEqual(app.root.position, [1730, 940])
        self.assertEqual(app.settings["position"], [1730, 940])
        self.assertEqual(app.save_calls, 1)
        self.assertTrue(app.root.withdrawn)
        self.assertEqual(app.root.deiconify_calls, 0)

    def test_update_visibility_corrects_position_before_deiconifying(self):
        laptop = self.laptop_monitor()
        app = self.make_app(stable_monitors=[laptop], should_show=True, withdrawn=True)

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=(laptop,)):
            overlay.OverlayApp.update_visibility(app, force=True)

        self.assertEqual(app.root.position, [1730, 940])
        self.assertEqual(app.settings["position"], [1730, 940])
        self.assertEqual(app.save_calls, 1)
        self.assertEqual(app.root.events[:2], [("geometry", (1730, 940)), ("deiconify", None)])

    def test_display_change_during_drag_waits_for_release_and_stable_sample(self):
        laptop = self.laptop_monitor()
        app = self.make_app(stable_monitors=[laptop, self.external_monitor()])
        app.is_dragging = True
        app.drag_offset = (5, 5)

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=(laptop,)):
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=4.0))
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=4.25))
            self.assertTrue(app._display_reconcile_deferred)
            self.assertEqual(app.root.position, [3895, 940])
            self.assertEqual(app.save_calls, 0)

            with mock.patch.object(overlay.time, "monotonic", return_value=4.5):
                overlay.OverlayApp.end_drag(app, None)

            self.assertEqual(app.root.position, [1730, 940])
            self.assertEqual(app.save_calls, 0)
            self.assertEqual(app._pending_drag_position, [1730, 940])

            self.assertTrue(overlay.OverlayApp._check_display_topology(app, now=4.75))

        self.assertEqual(app.settings["position"], [1730, 940])
        self.assertEqual(app.save_calls, 1)
        self.assertIsNone(app._pending_drag_position)

    def test_repeated_same_fingerprint_reconciles_without_move_or_write(self):
        laptop = self.laptop_monitor()
        app = self.make_app(position=(100, 100), stable_monitors=[laptop])
        app._native_display_dirty = True
        app._native_display_dirty_at = 5.0

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=(laptop,)):
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=5.5))
            self.assertTrue(overlay.OverlayApp._check_display_topology(app, now=5.75))
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=6.0))

        self.assertEqual(app.root.position, [100, 100])
        self.assertEqual(app.root.geometry_calls, [])
        self.assertEqual(app.settings["position"], [100, 100])
        self.assertEqual(app.save_calls, 0)

    def test_null_position_uses_primary_work_area_without_becoming_persistent(self):
        laptop = self.laptop_monitor()
        app = self.make_app(position=(3895, 940), stable_monitors=[laptop])
        app.settings["position"] = None

        overlay.OverlayApp.reconcile_overlay_position(
            app,
            "startup",
            candidate=None,
            persist=True,
            monitors=(laptop,),
        )

        self.assertEqual(app.root.position, [1718, 72])
        self.assertIsNone(app.settings["position"])
        self.assertEqual(app.save_calls, 0)

    def test_larger_rendered_size_is_reclamped_and_persisted(self):
        laptop = self.laptop_monitor()
        app = self.make_app(position=(1730, 100), stable_monitors=[laptop])
        app.root.size = (300, 80)

        overlay.OverlayApp.reconcile_overlay_position(
            app,
            "render_size",
            persist=True,
            monitors=(laptop,),
        )

        self.assertEqual(app.root.position, [1620, 100])
        self.assertEqual(app.settings["position"], [1620, 100])
        self.assertEqual(app.save_calls, 1)

    def test_changed_topology_closes_open_menu_before_recovery(self):
        class FakeMenuWindow:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        laptop = self.laptop_monitor()
        app = self.make_app(stable_monitors=[laptop, self.external_monitor()])
        menu_window = FakeMenuWindow()
        app.menu_active = True
        app.menu_window = menu_window

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=(laptop,)):
            self.assertFalse(overlay.OverlayApp._check_display_topology(app, now=7.0))
            self.assertTrue(overlay.OverlayApp._check_display_topology(app, now=7.25))

        self.assertEqual(menu_window.close_calls, 1)
        self.assertFalse(app.menu_active)
        self.assertIsNone(app.menu_window)
        self.assertEqual(app._last_menu_close_reason, "display_change")
        self.assertEqual(app.root.position, [1730, 940])


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
        self.calls = []

    def should_show(self, mode):
        self.calls.append(mode)
        return self.should_show_result

    def is_supported(self, _mode):
        return True


class FakeSchedulerRoot(FakeRoot):
    def __init__(self):
        super().__init__()
        self.after_callbacks = {}
        self.after_delays = []
        self.after_cancel_calls = []
        self.deiconify_calls = 0
        self.withdraw_calls = 0
        self.events = []
        self.destroyed = False
        self._next_after_id = 0

    def after(self, delay, callback):
        self._next_after_id += 1
        identifier = f"after-{self._next_after_id}"
        self.after_callbacks[identifier] = callback
        self.after_delays.append(delay)
        return identifier

    def after_cancel(self, identifier):
        self.after_cancel_calls.append(identifier)
        self.after_callbacks.pop(identifier, None)

    def run_after(self, identifier):
        callback = self.after_callbacks.pop(identifier)
        callback()

    def attributes(self, *args):
        super().attributes(*args)
        self.events.append(("attributes", args))

    def deiconify(self):
        super().deiconify()
        self.deiconify_calls += 1
        self.events.append(("deiconify", None))

    def withdraw(self):
        super().withdraw()
        self.withdraw_calls += 1
        self.events.append(("withdraw", None))

    def destroy(self):
        self.destroyed = True


class FakeRefreshReader:
    def __init__(self, events=None):
        self.calls = []
        self.last_error = None
        self.events = events
        self.batch = overlay.LogReadBatch(snapshot=None, token_events=[])

    def read_updates(self, force_rescan=False, now=None):
        self.calls.append((force_rescan, now))
        if self.events is not None:
            self.events.append(("read", force_rescan))
        return self.batch


class FakeRuntimeStateStore:
    def __init__(self):
        self.calls = []
        self.deleted = False

    def write(self, snapshot, counter, estimate):
        self.calls.append((snapshot, counter.state_dict(), estimate))
        return True

    def delete(self):
        self.deleted = True


class AdaptiveRefreshLoopTests(unittest.TestCase):
    def make_app(self, shown=True, should_show=True):
        app = object.__new__(overlay.OverlayApp)
        app.root = FakeSchedulerRoot()
        app.settings = {"visibility_mode": "visible_window"}
        app.process_backend = FakeProcessBackend(should_show)
        app.reader = FakeRefreshReader(app.root.events)
        app.runtime_state = FakeRuntimeStateStore()
        app.token_counter = overlay.TokenCounter(reset_at=0)
        app.detected_model = overlay.DetectedModel(None, "unknown")
        app.counter_reset_model = None
        app.last_model_check_at = 10**12
        app.snapshot = None
        app.force_rescan = False
        app.menu_active = False
        app.menu_window = None
        app.menu_anchor = None
        app.is_dragging = False
        app.needs_render_after_menu = False
        app.needs_render_after_drag = False
        app.needs_visibility_after_menu = False
        app._post_menu_visibility_after_id = None
        app._last_menu_close_reason = None
        app._overlay_is_shown = shown
        app._cached_should_show = None
        app._last_visibility_check_at = 0.0
        app._next_log_poll_at = 0.0
        app._next_state_write_at = 0.0
        app._next_display_poll_at = float("inf")
        app._refresh_after_id = None
        app._quitting = False
        app._startup_complete = False
        app._display_verification_after_id = None
        app._display_verification_due_at = None
        app._display_observer = None
        app.render_calls = 0
        app.request_render = lambda force=False: setattr(
            app,
            "render_calls",
            app.render_calls + 1,
        )
        app.refresh_detected_model = lambda force=False: None
        return app

    def test_active_loop_uses_500ms_and_keeps_one_timer(self):
        app = self.make_app(shown=True, should_show=True)
        with mock.patch.object(overlay.time, "monotonic", return_value=100.0):
            app.refresh()

        self.assertEqual(len(app.reader.calls), 1)
        self.assertEqual(len(app.runtime_state.calls), 1)
        self.assertEqual(app.root.after_delays[-1], overlay.POLL_INTERVAL_MS)
        self.assertEqual(len(app.root.after_callbacks), 1)

        identifier = app._refresh_after_id
        with mock.patch.object(overlay.time, "monotonic", return_value=100.5):
            app.root.run_after(identifier)

        self.assertEqual(len(app.reader.calls), 2)
        self.assertEqual(len(app.runtime_state.calls), 1)
        self.assertEqual(len(app.root.after_callbacks), 1)

    def test_hidden_loop_uses_one_second_wake_and_five_second_data_poll(self):
        app = self.make_app(shown=False, should_show=False)
        with mock.patch.object(overlay.time, "monotonic", return_value=100.0):
            app.refresh()

        self.assertEqual(app.root.after_delays[-1], overlay.HIDDEN_POLL_INTERVAL_MS)
        self.assertEqual(len(app.reader.calls), 1)
        self.assertEqual(len(app.runtime_state.calls), 1)

        with mock.patch.object(overlay.time, "monotonic", return_value=101.0):
            app.refresh(schedule_next=False)
        self.assertEqual(len(app.reader.calls), 1)
        self.assertEqual(len(app.runtime_state.calls), 1)

        with mock.patch.object(overlay.time, "monotonic", return_value=102.0):
            app.refresh(schedule_next=False)
        self.assertEqual(len(app.reader.calls), 1)
        self.assertEqual(len(app.runtime_state.calls), 2)

        with mock.patch.object(overlay.time, "monotonic", return_value=105.0):
            app.refresh(schedule_next=False)
        self.assertEqual(len(app.reader.calls), 2)

    def test_process_visibility_is_checked_at_most_once_per_second(self):
        app = self.make_app(shown=True, should_show=True)
        app.settings["visibility_mode"] = "process"
        for current in (100.0, 100.5, 101.0):
            with mock.patch.object(overlay.time, "monotonic", return_value=current):
                app.refresh(schedule_next=False)

        self.assertEqual(app.process_backend.calls, ["process", "process"])
        self.assertEqual(len(app.reader.calls), 3)

    def test_always_visible_mode_skips_process_backend(self):
        app = self.make_app(shown=True, should_show=True)
        app.settings["visibility_mode"] = "always"
        with mock.patch.object(overlay.time, "monotonic", return_value=100.0):
            app.refresh(schedule_next=False)

        self.assertEqual(app.process_backend.calls, [])

    def test_hidden_to_visible_transition_catches_up_before_showing(self):
        app = self.make_app(shown=False, should_show=False)
        with mock.patch.object(overlay.time, "monotonic", return_value=100.0):
            app.refresh(schedule_next=False)
        app.root.events.clear()
        app.process_backend.should_show_result = True

        with (
            mock.patch.object(overlay.time, "monotonic", return_value=101.0),
            mock.patch.object(
                overlay,
                "windows_monitor_work_areas",
                return_value=(),
            ) as enumerate_monitors,
        ):
            app.refresh(schedule_next=False)

        event_names = [event[0] for event in app.root.events]
        self.assertEqual(event_names[:2], ["read", "deiconify"])
        self.assertTrue(app._overlay_is_shown)
        self.assertEqual(len(app.reader.calls), 2)
        self.assertEqual(app.render_calls, 1)
        self.assertEqual(enumerate_monitors.call_count, 1)

    def test_manual_refresh_keeps_existing_timer_and_forces_reader(self):
        app = self.make_app(shown=True, should_show=True)
        with mock.patch.object(overlay.time, "monotonic", return_value=100.0):
            app.refresh()
        existing_timer = app._refresh_after_id
        app._next_log_poll_at = 999.0

        with mock.patch.object(overlay.time, "monotonic", return_value=100.1):
            app.manual_refresh()

        self.assertEqual(app.reader.calls[-1][0], True)
        self.assertEqual(len(app.runtime_state.calls), 2)
        self.assertEqual(app._refresh_after_id, existing_timer)
        self.assertEqual(len(app.root.after_callbacks), 1)

    def test_clean_display_topology_waits_for_fallback_deadline(self):
        app = self.make_app()
        app._stable_monitor_fingerprint = (("stable",),)
        app._pending_monitor_fingerprint = None
        app._native_display_dirty = False
        app._display_reconcile_deferred = False
        app._display_verification_due_at = None
        app._next_display_poll_at = 10.0
        app._consume_native_display_notifications = mock.Mock(return_value=False)
        app._check_display_topology = mock.Mock(return_value=False)

        self.assertFalse(app._check_display_topology_if_due(5.0))
        app._check_display_topology.assert_not_called()
        self.assertFalse(app._check_display_topology_if_due(10.0))
        app._check_display_topology.assert_called_once_with(now=10.0)
        self.assertEqual(
            app._next_display_poll_at,
            10.0 + overlay.DISPLAY_TOPOLOGY_POLL_INTERVAL_SECONDS,
        )

    def test_pending_display_sample_waits_for_sample_deadline(self):
        app = self.make_app()
        app._stable_monitor_fingerprint = (("stable",),)
        app._pending_monitor_fingerprint = (("candidate",),)
        app._pending_monitor_sample_count = 1
        app._pending_monitor_last_seen_at = 10.0
        app._native_display_dirty = False
        app._display_reconcile_deferred = False
        app._display_verification_due_at = None
        app._last_display_scan_succeeded = True
        app._next_display_poll_at = (
            10.0 + overlay.DISPLAY_TOPOLOGY_SAMPLE_SECONDS
        )
        app._consume_native_display_notifications = mock.Mock(return_value=False)
        app._check_display_topology = mock.Mock(return_value=False)

        app._check_display_topology_if_due(10.1)
        app._check_display_topology.assert_not_called()
        app._check_display_topology_if_due(
            10.0 + overlay.DISPLAY_TOPOLOGY_SAMPLE_SECONDS,
        )
        app._check_display_topology.assert_called_once()

    def test_empty_display_scan_uses_bounded_retry_deadline(self):
        app = self.make_app()
        app._stable_monitor_fingerprint = (("stable",),)
        app._pending_monitor_fingerprint = None
        app._native_display_dirty = False
        app._display_reconcile_deferred = False
        app._display_verification_due_at = None
        app._next_display_poll_at = 10.0
        app._consume_native_display_notifications = mock.Mock(return_value=False)

        def failed_scan(now):
            app._last_display_scan_succeeded = False
            return False

        app._check_display_topology = mock.Mock(side_effect=failed_scan)
        app._check_display_topology_if_due(10.0)
        app._check_display_topology_if_due(10.5)
        self.assertEqual(app._check_display_topology.call_count, 1)
        app._check_display_topology_if_due(
            10.0 + overlay.DISPLAY_TOPOLOGY_RETRY_SECONDS,
        )
        self.assertEqual(app._check_display_topology.call_count, 2)

    def test_deferred_drag_reconciliation_does_not_scan_each_tick(self):
        app = self.make_app()
        app.is_dragging = True
        app._stable_monitor_fingerprint = (("stable",),)
        app._pending_monitor_fingerprint = (("candidate",),)
        app._pending_monitor_sample_count = 2
        app._pending_monitor_last_seen_at = 10.0
        app._native_display_dirty = True
        app._native_display_dirty_at = 9.0
        app._display_reconcile_deferred = True
        app._display_verification_due_at = None
        app._last_display_scan_succeeded = True
        app._next_display_poll_at = 10.0
        app._consume_native_display_notifications = mock.Mock(return_value=False)
        app._check_display_topology = mock.Mock(return_value=False)

        app._check_display_topology_if_due(10.0)
        app._check_display_topology_if_due(10.5)

        app._check_display_topology.assert_called_once()
        self.assertEqual(
            app._next_display_poll_at,
            10.0 + overlay.DISPLAY_TOPOLOGY_POLL_INTERVAL_SECONDS,
        )

    def test_runtime_heartbeat_is_inside_single_instance_stale_window(self):
        self.assertLess(
            overlay.RUNTIME_STATE_WRITE_INTERVAL_SECONDS,
            overlay.INSTANCE_LOCK_HEARTBEAT_STALE_SECONDS,
        )

    def test_failed_runtime_write_retries_on_next_wake(self):
        app = self.make_app(shown=True, should_show=True)
        app.runtime_state.write = mock.Mock(side_effect=[False, True])

        with mock.patch.object(overlay.time, "monotonic", return_value=100.0):
            app.refresh(schedule_next=False)
        self.assertEqual(app._next_state_write_at, 0.0)

        with mock.patch.object(overlay.time, "monotonic", return_value=100.5):
            app.refresh(schedule_next=False)

        self.assertEqual(app.runtime_state.write.call_count, 2)
        self.assertEqual(
            app._next_state_write_at,
            100.5 + overlay.RUNTIME_STATE_WRITE_INTERVAL_SECONDS,
        )

    def test_semantic_data_change_writes_state_before_heartbeat(self):
        app = self.make_app(shown=True, should_show=True)
        with mock.patch.object(overlay.time, "monotonic", return_value=100.0):
            app.refresh(schedule_next=False)
        self.assertEqual(len(app.runtime_state.calls), 1)

        app.reader.batch = overlay.LogReadBatch(
            snapshot=overlay.RateSnapshot(
                timestamp="2026-07-28T12:00:00Z",
                primary=overlay.RateWindow("5h", 300, 10, 90, None),
                secondary=None,
                plan_type="prolite",
                rate_limit_reached_type=None,
            ),
            token_events=[],
        )
        with mock.patch.object(overlay.time, "monotonic", return_value=100.5):
            app.refresh(schedule_next=False)

        self.assertEqual(len(app.runtime_state.calls), 2)
        self.assertEqual(
            app._next_state_write_at,
            100.5 + overlay.RUNTIME_STATE_WRITE_INTERVAL_SECONDS,
        )

    def test_five_minute_unchanged_schedule_bounds_writes_and_callbacks(self):
        app = self.make_app(shown=True, should_show=True)
        refresh_count = 5 * 60 * 1_000 // overlay.POLL_INTERVAL_MS

        for index in range(refresh_count):
            current = index * overlay.POLL_INTERVAL_MS / 1_000
            with mock.patch.object(overlay.time, "monotonic", return_value=current):
                app.refresh()

        self.assertEqual(len(app.reader.calls), refresh_count)
        self.assertLessEqual(
            len(app.runtime_state.calls),
            refresh_count // 4,
        )
        self.assertEqual(len(app.root.after_callbacks), 1)

    def test_quit_cancels_owned_refresh_timer(self):
        app = self.make_app()
        with mock.patch.object(overlay.time, "monotonic", return_value=100.0):
            app.refresh()
        identifier = app._refresh_after_id

        app.quit()

        self.assertIn(identifier, app.root.after_cancel_calls)
        self.assertEqual(app.root.after_callbacks, {})
        self.assertTrue(app.runtime_state.deleted)
        self.assertTrue(app.root.destroyed)

    def test_quit_closes_menu_and_cancels_post_menu_callback_before_destroy(self):
        class FakeMenuWindow:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        app = self.make_app()
        app.menu_active = True
        menu_window = FakeMenuWindow()
        app.menu_window = menu_window
        app._post_menu_visibility_after_id = app.root.after(250, lambda: None)
        callback_id = app._post_menu_visibility_after_id

        app.quit()

        self.assertEqual(menu_window.close_calls, 1)
        self.assertEqual(app._last_menu_close_reason, "quit")
        self.assertIn(callback_id, app.root.after_cancel_calls)
        self.assertEqual(app.root.after_callbacks, {})
        self.assertTrue(app.root.destroyed)


class RenderAndVisibilityGatingTests(unittest.TestCase):
    class FakeLabel:
        instances = []

        def __init__(self, *_args, **_kwargs):
            self.destroyed = False
            self.__class__.instances.append(self)

        def grid(self, **_kwargs):
            pass

        def destroy(self):
            self.destroyed = True

    def make_render_app(self):
        app = object.__new__(overlay.OverlayApp)
        app.settings = {"layout_mode": "horizontal"}
        app.labels = []
        app.container = object()
        app._force_render_requested = False
        app._last_render_signature = None
        app._last_overlay_size = (190, 40)
        app._startup_complete = False
        app.menu_active = False
        app.is_dragging = False
        app.needs_render_after_menu = False
        app.needs_render_after_drag = False
        app.display_widgets = lambda: [
            overlay.DisplayWidget("primary", "5h 90%", overlay.COLOR_GREEN)
        ]
        app.current_window_size = lambda: (190, 40)
        app._bind_window_events = lambda _widget: None
        return app

    def test_identical_render_signature_reuses_existing_labels(self):
        app = self.make_render_app()
        self.FakeLabel.instances = []
        with mock.patch.object(overlay.tk, "Label", self.FakeLabel):
            app.render()
            first_label = app.labels[0]
            app.render()

        self.assertEqual(len(self.FakeLabel.instances), 1)
        self.assertIs(app.labels[0], first_label)
        self.assertFalse(first_label.destroyed)

    def test_force_render_rebuilds_labels_and_menu_defers_only_changes(self):
        app = self.make_render_app()
        self.FakeLabel.instances = []
        with mock.patch.object(overlay.tk, "Label", self.FakeLabel):
            app.render()
            first_label = app.labels[0]
            app.menu_active = True
            app.request_render()
            self.assertFalse(app.needs_render_after_menu)
            app.menu_active = False
            app.request_render(force=True)

        self.assertTrue(first_label.destroyed)
        self.assertEqual(len(self.FakeLabel.instances), 2)

    def test_visibility_calls_tk_only_on_transitions(self):
        app = object.__new__(overlay.OverlayApp)
        app.root = FakeSchedulerRoot()
        app.settings = {"visibility_mode": "visible_window"}
        app.process_backend = FakeProcessBackend()
        app.menu_active = False
        app.needs_visibility_after_menu = False
        app._overlay_is_shown = False
        app._cached_should_show = None
        app._last_visibility_check_at = 0.0
        app._stable_monitor_fingerprint = None
        app.reconcile_overlay_position = lambda *_args, **_kwargs: False

        with mock.patch.object(overlay, "windows_monitor_work_areas", return_value=()):
            app.update_visibility(should_show=False)
            app.update_visibility(should_show=False)
            app.update_visibility(should_show=True)
            app.update_visibility(should_show=True)
            app.update_visibility(force=True, should_show=True)
            app.update_visibility(should_show=False)
            app.update_visibility(should_show=False)

        self.assertEqual(app.root.deiconify_calls, 1)
        self.assertEqual(app.root.withdraw_calls, 1)
        self.assertEqual(
            app.root.attribute_calls.count(("-topmost", True)),
            1,
        )


class MenuInteractionTests(unittest.TestCase):
    def make_app(self):
        app = object.__new__(overlay.OverlayApp)
        app.root = FakeSchedulerRoot()
        app.settings = {"visibility_mode": "always"}
        app.process_backend = FakeProcessBackend()
        app.menu_active = True
        app.menu_window = None
        app.menu_anchor = (100, 100)
        app.is_dragging = False
        app.needs_render_after_menu = False
        app.needs_render_after_drag = False
        app.needs_visibility_after_menu = False
        app._post_menu_visibility_after_id = None
        app._last_menu_close_reason = None
        app._overlay_is_shown = True
        app._quitting = False
        app._cached_should_show = True
        app._last_visibility_check_at = 0.0
        app.render_calls = 0
        app.render = lambda: setattr(app, "render_calls", app.render_calls + 1)
        return app

    def test_context_menu_binds_to_button_release(self):
        class FakeBindingWidget:
            def __init__(self):
                self.bindings = {}

            def bind(self, event_name, callback):
                self.bindings[event_name] = callback

        app = self.make_app()
        widget = FakeBindingWidget()

        overlay.OverlayApp._bind_window_events(app, widget)

        self.assertIn("<ButtonRelease-3>", widget.bindings)
        self.assertIn("<ButtonRelease-2>", widget.bindings)
        self.assertNotIn("<Button-3>", widget.bindings)
        self.assertNotIn("<Button-2>", widget.bindings)

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
        self.assertEqual(app._last_menu_close_reason, "command")
        self.assertEqual(app.root.after_delays[-1], overlay.POST_MENU_VISIBILITY_DELAY_MS)
        self.assertNotIn(("-topmost", True), app.root.attribute_calls)

        app.root.run_after(app._post_menu_visibility_after_id)

        self.assertTrue(app._overlay_is_shown)
        self.assertIn(("-topmost", True), app.root.attribute_calls)

    def test_foreground_visibility_is_leased_until_delayed_reconcile(self):
        app = self.make_app()
        app.settings["visibility_mode"] = "foreground"
        app.process_backend = FakeProcessBackend(False)

        overlay.OverlayApp.update_visibility(app)
        self.assertEqual(app.root.withdraw_calls, 0)

        overlay.OverlayApp.finish_menu_interaction(app, "escape")
        self.assertEqual(app.root.withdraw_calls, 0)

        app.root.run_after(app._post_menu_visibility_after_id)

        self.assertEqual(app.root.withdraw_calls, 1)
        self.assertFalse(app._overlay_is_shown)

    def test_finish_is_idempotent_and_cancels_replaced_reconcile(self):
        class FakeMenuWindow:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        app = self.make_app()
        menu_window = FakeMenuWindow()
        app.menu_window = menu_window

        self.assertTrue(overlay.OverlayApp.finish_menu_interaction(app, "outside_click"))
        first_after_id = app._post_menu_visibility_after_id
        self.assertFalse(overlay.OverlayApp.finish_menu_interaction(app, "focus_loss"))

        self.assertEqual(menu_window.close_calls, 1)
        self.assertEqual(app._last_menu_close_reason, "outside_click")
        self.assertIn(first_after_id, app.root.after_callbacks)

        overlay.OverlayApp.begin_menu_interaction(app)

        self.assertIn(first_after_id, app.root.after_cancel_calls)
        self.assertNotIn(first_after_id, app.root.after_callbacks)
        self.assertTrue(app.menu_active)

    def test_popup_focus_churn_requires_two_confirmations(self):
        class FakePopupWindow(FakeSchedulerRoot):
            def focus_displayof(self):
                return None

        reasons = []
        menu = object.__new__(overlay.ContextMenuWindow)
        menu.app = type(
            "App",
            (),
            {"finish_menu_interaction": lambda _self, reason: reasons.append(reason)},
        )()
        menu.window = FakePopupWindow()
        menu.closed = False
        menu.focus_dismiss_enabled = True
        menu._focus_arm_after_id = None
        menu._focus_dismiss_after_id = None
        menu._focus_loss_confirmations = 0

        overlay.ContextMenuWindow._schedule_focus_dismiss(menu)
        first_check = menu._focus_dismiss_after_id
        menu.window.run_after(first_check)
        transient_second_check = menu._focus_dismiss_after_id

        self.assertEqual(reasons, [])
        overlay.ContextMenuWindow._cancel_focus_dismiss(menu)
        self.assertIn(transient_second_check, menu.window.after_cancel_calls)

        overlay.ContextMenuWindow._schedule_focus_dismiss(menu)
        menu.window.run_after(menu._focus_dismiss_after_id)
        menu.window.run_after(menu._focus_dismiss_after_id)

        self.assertEqual(reasons, ["focus_loss"])

    def test_popup_close_cancels_callbacks_before_destroy(self):
        class FakePopupWindow(FakeSchedulerRoot):
            def __init__(self):
                super().__init__()
                self.grab_released = False

            def grab_current(self):
                return self

            def grab_release(self):
                self.grab_released = True

        menu = object.__new__(overlay.ContextMenuWindow)
        menu.window = FakePopupWindow()
        menu.closed = False
        menu._focus_loss_confirmations = 1
        menu._focus_arm_after_id = menu.window.after(150, lambda: None)
        menu._focus_dismiss_after_id = menu.window.after(100, lambda: None)
        owned_ids = {
            menu._focus_arm_after_id,
            menu._focus_dismiss_after_id,
        }

        overlay.ContextMenuWindow.close(menu)

        self.assertTrue(menu.closed)
        self.assertEqual(set(menu.window.after_cancel_calls), owned_ids)
        self.assertEqual(menu.window.after_callbacks, {})
        self.assertTrue(menu.window.grab_released)
        self.assertTrue(menu.window.destroyed)

    def test_menu_replacement_closes_only_the_popup(self):
        class FakeMenuWindow:
            instances = []

            def __init__(self, _app=None, rows=None):
                self.rows = rows
                self.close_calls = 0
                self.show_calls = []
                self.__class__.instances.append(self)

            def close(self):
                self.close_calls += 1

            def show(self, x, y):
                self.show_calls.append((x, y))

        app = self.make_app()
        old_window = FakeMenuWindow()
        app.menu_window = old_window

        with mock.patch.object(overlay, "ContextMenuWindow", FakeMenuWindow):
            overlay.OverlayApp.replace_menu_window(
                app,
                [overlay.MenuRow.disabled("Details")],
                (25, 35),
            )

        self.assertEqual(old_window.close_calls, 1)
        self.assertTrue(app.menu_active)
        self.assertEqual(app._last_menu_close_reason, "replacement")
        self.assertEqual(app.menu_window.show_calls, [(25, 35)])

    def test_repeated_right_click_closes_previous_interaction_before_reopening(self):
        class FakeMenuWindow:
            def __init__(self):
                self.close_calls = 0

            def close(self):
                self.close_calls += 1

        app = self.make_app()
        old_window = FakeMenuWindow()
        app.menu_window = old_window
        replacements = []
        app.build_menu_rows = lambda: []
        app.replace_menu_window = lambda rows, anchor: replacements.append((rows, anchor))
        event = type("Event", (), {"x_root": 400, "y_root": 500})()

        overlay.OverlayApp.show_menu(app, event)

        self.assertEqual(old_window.close_calls, 1)
        self.assertEqual(app._last_menu_close_reason, "reopen")
        self.assertTrue(app.menu_active)
        self.assertEqual(replacements, [([], (400, 500))])

    def test_escape_and_outside_click_report_distinct_close_reasons(self):
        reasons = []
        menu = object.__new__(overlay.ContextMenuWindow)
        menu.app = type(
            "App",
            (),
            {"finish_menu_interaction": lambda _self, reason: reasons.append(reason)},
        )()
        menu._point_inside = lambda _x, _y: False
        event = type("Event", (), {"x_root": 10, "y_root": 20})()

        self.assertEqual(overlay.ContextMenuWindow._dismiss(menu), "break")
        self.assertEqual(
            overlay.ContextMenuWindow._dismiss_if_outside(menu, event),
            "break",
        )

        self.assertEqual(reasons, ["escape", "outside_click"])


class MenuRowTests(unittest.TestCase):
    def test_clickable_row_calls_command_exactly_once(self):
        calls = []
        row = overlay.MenuRow.command("Do thing", lambda: calls.append("called"))

        self.assertTrue(row.invoke())

        self.assertEqual(calls, ["called"])

    def test_disabled_row_does_not_call_command(self):
        calls = []
        row = overlay.MenuRow("disabled", "Do not call", lambda: calls.append("called"))

        self.assertFalse(row.invoke())

        self.assertEqual(calls, [])


class MenuModelTests(unittest.TestCase):
    def make_app(self, **settings):
        app = object.__new__(overlay.OverlayApp)
        app.settings = {
            "visibility_mode": "always",
            "display_windows": ["primary", "secondary"],
            "show_resets": False,
            "show_token_counter": True,
            "show_api_cost_estimate": False,
            "layout_mode": "grid_2x2",
        }
        app.settings.update(settings)
        app.reader = type("Reader", (), {"last_error": None})()
        app.process_backend = FakeProcessBackend()
        app.snapshot = None
        app.token_counter = overlay.TokenCounter(reset_at=1_000)
        app.detected_model = overlay.DetectedModel(None, "test")
        app.counter_reset_model = None
        app.menu_active = True
        app.menu_window = None
        app.needs_render_after_menu = False
        app.needs_render_after_drag = False
        app.needs_visibility_after_menu = False
        app.is_dragging = False
        app.root = FakeRoot()
        app.render_calls = 0
        app.save_calls = 0
        app.render = lambda: setattr(app, "render_calls", app.render_calls + 1)
        app.save_settings = lambda: setattr(app, "save_calls", app.save_calls + 1)
        return app

    def labels(self, app):
        return [row.label for row in overlay.OverlayApp.build_menu_rows(app) if row.label]

    def test_menu_item_labels_reflect_current_settings(self):
        app = self.make_app()

        labels = self.labels(app)

        self.assertIn("(*) Always", labels)
        self.assertIn("[x] Show Token Counter", labels)
        self.assertIn("[ ] Show API Cost Estimate", labels)
        self.assertIn("(*) Horizontal", labels)
        self.assertIn("( ) Vertical", labels)
        self.assertFalse(any("Grid" in label for label in labels))
        self.assertIn("Rate windows: waiting for data", labels)

    def test_main_menu_keeps_commands_and_moves_diagnostics_to_details(self):
        app = self.make_app()

        labels = self.labels(app)
        detail_labels = [
            row.label for row in overlay.OverlayApp.build_detail_menu_rows(app) if row.label
        ]

        self.assertIn("Details...", labels)
        self.assertIn("Reset Token Counter", labels)
        self.assertIn("Refresh", labels)
        self.assertIn("Reset position", labels)
        self.assertIn("Quit", labels)
        self.assertNotIn("Waiting for Codex rate data", labels)
        self.assertNotIn("Token Counter", labels)
        self.assertNotIn("API Estimate", labels)

        self.assertIn("Back to menu", detail_labels)
        self.assertIn("Waiting for Codex rate data", detail_labels)
        self.assertIn("Token Counter", detail_labels)
        self.assertIn(app.token_counter.display_text(), detail_labels)
        self.assertIn("API Estimate", detail_labels)

    def test_details_label_gpt_56_pricing_as_exact(self):
        app = self.make_app(show_api_cost_estimate=True)
        app.detected_model = overlay.DetectedModel("gpt-5.6-sol", "logs_2.sqlite")
        app.counter_reset_model = "gpt-5.6-sol"
        rows = []

        overlay.OverlayApp.add_api_estimate_menu_rows(app, rows)
        labels = [row.label for row in rows if row.label]

        self.assertIn("$0.00 API est.", labels)
        self.assertIn("Detected model: gpt-5.6-sol (logs_2.sqlite)", labels)
        self.assertIn("Pricing model: GPT-5.6-SOL; tier: Standard (assumed)", labels)
        self.assertIn(
            "Rates /1M (short): input $5.00, cached $0.50, write $6.25, output $30.00",
            labels,
        )
        self.assertIn(
            "Rates /1M (>272k input): input $10.00, cached $1.00, write $12.50, output $45.00",
            labels,
        )
        self.assertIn(overlay.CACHE_WRITE_TELEMETRY_NOTE, labels)

    def test_layout_command_changes_mode_on_first_invocation(self):
        app = self.make_app(layout_mode="horizontal")
        rows = overlay.OverlayApp.build_menu_rows(app)
        vertical_row = next(row for row in rows if row.label == "( ) Vertical")

        self.assertTrue(vertical_row.invoke())

        self.assertEqual(app.settings["layout_mode"], "vertical")
        self.assertEqual(app.save_calls, 1)
        self.assertEqual(app.render_calls, 1)
        self.assertFalse(app.menu_active)

    def test_menu_lists_only_available_rate_windows(self):
        app = self.make_app(layout_mode="horizontal")
        app.snapshot = overlay.RateSnapshot(
            timestamp="2026-07-13T15:00:00Z",
            primary=overlay.RateWindow("7d", 10080, 1.0, 99, 1784563200),
            secondary=None,
            plan_type="prolite",
            rate_limit_reached_type=None,
        )

        labels = self.labels(app)

        self.assertIn("[x] 7-day limit", labels)
        self.assertFalse(any("5-hour limit" in label for label in labels))
        self.assertNotIn("Rate windows: waiting for data", labels)


class DisplaySelectionTests(unittest.TestCase):
    def snapshot(self, primary=True, secondary=True):
        return overlay.RateSnapshot(
            timestamp="2026-07-13T15:00:00Z",
            primary=(
                overlay.RateWindow("7d", 10080, 1.0, 99, 1784563200)
                if primary
                else None
            ),
            secondary=(
                overlay.RateWindow("5h", 300, 10.0, 90, 1784000000)
                if secondary
                else None
            ),
            plan_type="prolite",
            rate_limit_reached_type=None,
        )

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

    def test_legacy_grid_layout_migrates_to_horizontal(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "settings.json"
            path.write_text(json.dumps({"layout_mode": "grid_2x2"}), encoding="utf-8")

            settings = overlay.load_settings(path)

        self.assertEqual(settings["layout_mode"], "horizontal")
        self.assertNotIn("grid_2x2", overlay.VALID_LAYOUT_MODES)

    def test_layout_positions(self):
        self.assertEqual(
            overlay.layout_positions(4, "horizontal"),
            [(0, 0), (0, 1), (0, 2), (0, 3)],
        )
        self.assertEqual(
            overlay.layout_positions(4, "vertical"),
            [(0, 0), (1, 0), (2, 0), (3, 0)],
        )
        self.assertEqual(overlay.layout_positions(3, "horizontal"), [(0, 0), (0, 1), (0, 2)])
        self.assertEqual(overlay.layout_positions(3, "vertical"), [(0, 0), (1, 0), (2, 0)])
        self.assertEqual(
            overlay.layout_positions(4, "grid_2x2"),
            [(0, 0), (0, 1), (0, 2), (0, 3)],
        )

    def test_display_widget_ordering(self):
        settings = {
            "display_windows": ["primary", "secondary"],
            "show_token_counter": True,
            "show_api_cost_estimate": True,
        }

        self.assertEqual(
            overlay.active_display_widget_keys(settings, self.snapshot()),
            ["primary", "secondary", "token_counter", "api_cost"],
        )

        settings["display_windows"] = ["secondary"]
        settings["show_token_counter"] = False

        self.assertEqual(
            overlay.active_display_widget_keys(settings, self.snapshot()),
            ["secondary", "api_cost"],
        )

    def test_stale_saved_window_selection_falls_back_to_available_window(self):
        settings = {
            "display_windows": ["secondary"],
            "show_token_counter": False,
            "show_api_cost_estimate": False,
        }

        self.assertEqual(
            overlay.effective_display_windows(settings, self.snapshot(primary=True, secondary=False)),
            ["primary"],
        )
        self.assertEqual(
            overlay.active_display_widget_keys(settings, self.snapshot(primary=True, secondary=False)),
            ["primary"],
        )

    def test_no_rate_data_has_one_waiting_widget_key(self):
        settings = {
            "display_windows": ["primary", "secondary"],
            "show_token_counter": False,
            "show_api_cost_estimate": False,
        }

        self.assertEqual(overlay.active_display_widget_keys(settings, None), ["rate_waiting"])

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


class DisplayWidgetTests(unittest.TestCase):
    def make_app(self, snapshot, display_windows=None, show_resets=False):
        app = object.__new__(overlay.OverlayApp)
        app.settings = {
            "display_windows": display_windows or ["primary", "secondary"],
            "show_resets": show_resets,
            "show_token_counter": False,
            "show_api_cost_estimate": False,
        }
        app.snapshot = snapshot
        app.token_counter = overlay.TokenCounter(reset_at=1_000)
        app.detected_model = overlay.DetectedModel("gpt-5.6-sol", "test")
        app.counter_reset_model = "gpt-5.6-sol"
        return app

    def snapshot(self, primary=None, secondary=None):
        return overlay.RateSnapshot(
            timestamp="2026-07-13T15:00:00Z",
            primary=primary,
            secondary=secondary,
            plan_type="prolite",
            rate_limit_reached_type=None,
        )

    def test_current_single_weekly_window_never_renders_duplicate_placeholder(self):
        snapshot = self.snapshot(
            primary=overlay.RateWindow("7d", 10080, 1.0, 99, 1784563200),
            secondary=None,
        )

        widgets = overlay.OverlayApp.display_widgets(self.make_app(snapshot))

        self.assertEqual([(widget.key, widget.text) for widget in widgets], [("primary", "7d 99%")])
        self.assertFalse(any("--" in widget.text for widget in widgets))

    def test_legacy_five_hour_and_weekly_windows_render_both(self):
        snapshot = self.snapshot(
            primary=overlay.RateWindow("5h", 300, 10.0, 90, 1784000000),
            secondary=overlay.RateWindow("7d", 10080, 20.0, 80, 1784563200),
        )

        widgets = overlay.OverlayApp.display_widgets(self.make_app(snapshot))

        self.assertEqual(
            [(widget.key, widget.text) for widget in widgets],
            [("primary", "5h 90%"), ("secondary", "7d 80%")],
        )

    def test_stale_missing_slot_selection_renders_available_window(self):
        snapshot = self.snapshot(
            primary=overlay.RateWindow("7d", 10080, 1.0, 99, 1784563200),
            secondary=None,
        )

        widgets = overlay.OverlayApp.display_widgets(
            self.make_app(snapshot, display_windows=["secondary"])
        )

        self.assertEqual([(widget.key, widget.text) for widget in widgets], [("primary", "7d 99%")])

    def test_no_rate_data_renders_one_waiting_message(self):
        widgets = overlay.OverlayApp.display_widgets(self.make_app(None))

        self.assertEqual(len(widgets), 1)
        self.assertEqual(widgets[0].key, "rate_waiting")
        self.assertEqual(widgets[0].text, "Waiting for Codex rate data")

    def test_available_window_without_percentage_uses_telemetry_label(self):
        snapshot = self.snapshot(
            primary=overlay.RateWindow("limit", None, None, None, None),
            secondary=None,
        )

        widgets = overlay.OverlayApp.display_widgets(self.make_app(snapshot, show_resets=True))

        self.assertEqual(widgets[0].text, "limit -- reset --")


if __name__ == "__main__":
    unittest.main()
