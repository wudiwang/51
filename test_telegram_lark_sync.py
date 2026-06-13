import unittest
import json
import sys
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from telegram_lark_sync import (
    LarkTarget,
    chat_matches_targets,
    chunked,
    existing_sheet_keys,
    is_target_chat,
    message_key_from_row,
    message_to_row,
    to_csv,
    trim_text,
)


class Entity:
    def __init__(self, **kwargs):
        self.__dict__.update(kwargs)


class Event:
    def __init__(self):
        self.message = Entity(
            id=42,
            date=datetime(2026, 6, 4, 8, 30, tzinfo=timezone.utc),
            media=None,
        )
        self.raw_text = 'hello, "Lark"\nnext line'
        self.chat = Entity(id=-1001, title="Project Group")
        self.sender = Entity(id=77, first_name="Ethan", last_name="W")

    async def get_chat(self):
        return self.chat

    async def get_sender(self):
        return self.sender


class TelegramLarkSyncTest(unittest.IsolatedAsyncioTestCase):
    async def test_message_to_row_maps_telegram_message_to_sheet_columns(self):
        row = await message_to_row(
            Event(),
            synced_at=datetime(2026, 6, 4, 9, 0, tzinfo=timezone.utc),
        )

        self.assertEqual(row[0], "2026-06-04 09:00:00")
        self.assertEqual(row[2], "Project Group")
        self.assertEqual(row[3], "-1001")
        self.assertEqual(row[4], "Ethan W")
        self.assertEqual(row[6], "42")
        self.assertEqual(row[7], 'hello, "Lark"\nnext line')
        self.assertEqual(row[8], "否")
        self.assertEqual(row[9], "text")

    def test_to_csv_quotes_commas_quotes_and_newlines(self):
        csv_text = to_csv([["a,b", '"quoted"', "line\nbreak"]])

        self.assertEqual(csv_text, '"a,b","""quoted""","line\nbreak"\n')

    def test_trim_text_keeps_short_text_and_truncates_long_text(self):
        self.assertEqual(trim_text("short", 10), "short")
        self.assertEqual(trim_text("abcdefghijklmnopqrstuvwxyz", 25), "abcde\n...[truncated]")

    def test_is_target_chat_includes_private_only_when_enabled(self):
        private_event = Entity(is_group=False, is_channel=False, is_private=True)
        channel_event = Entity(is_group=False, is_channel=True, is_private=False)
        group_event = Entity(is_group=True, is_channel=False, is_private=False)

        self.assertTrue(is_target_chat(group_event, include_channels=False, include_private=False))
        self.assertFalse(is_target_chat(private_event, include_channels=False, include_private=False))
        self.assertTrue(is_target_chat(private_event, include_channels=False, include_private=True))
        self.assertFalse(is_target_chat(channel_event, include_channels=False, include_private=True))

    def test_message_key_from_row_uses_chat_and_message_id(self):
        row = ["", "", "chat", "-1001", "sender", "77", "42", "text", "否", "text"]

        self.assertEqual(message_key_from_row(row), "-1001:42")
        self.assertIsNone(message_key_from_row(row[:6]))
        self.assertIsNone(message_key_from_row(["", "", "", "", "", "", ""]))

    def test_chunked_splits_rows_into_fixed_size_batches(self):
        self.assertEqual(list(chunked([1, 2, 3, 4, 5], 2)), [[1, 2], [3, 4], [5]])

    def test_chat_matches_targets_by_name_or_id(self):
        chat = Entity(id=4844072747, title="fiveone-overall")

        self.assertTrue(chat_matches_targets(chat, ["fiveone-overall"]))
        self.assertTrue(chat_matches_targets(chat, ["4844072747"]))
        self.assertFalse(chat_matches_targets(chat, ["other-group"]))
        self.assertTrue(chat_matches_targets(chat, []))

    def test_existing_sheet_keys_reads_recent_dedupe_window_only(self):
        payload = {
            "data": {
                "ranges": [
                    {
                        "cells": [
                            [{}, {}, {}, {"value": "chat-1"}, {}, {}, {"value": "99"}]
                        ]
                    }
                ]
            }
        }

        with patch("telegram_lark_sync.subprocess.run") as run:
            run.return_value.stdout = json.dumps(payload)

            keys = existing_sheet_keys(
                LarkTarget(
                    spreadsheet_token="sheet",
                    sheet_id="tab",
                    next_row=2327,
                    cli_path="lark-cli",
                    dedupe_scan_rows=1000,
                )
            )

        self.assertEqual(keys, {"chat-1:99"})
        ranges = [call.args[0][call.args[0].index("--range") + 1] for call in run.call_args_list]
        self.assertEqual(
            ranges,
            [
                "A1327:J1526",
                "A1527:J1726",
                "A1727:J1926",
                "A1927:J2126",
                "A2127:J2326",
            ],
        )


if __name__ == "__main__":
    unittest.main()
