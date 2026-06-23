import sys
import unittest
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import patch


sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from telegram_lark_intake import (
    ExistingRecord,
    IntakeConfig,
    TelegramItem,
    build_discussion,
    classify_message,
    extract_jira_url,
    find_matching_record,
    notification_text,
    process_items,
    status_from_text,
    title_from_text,
)


class TelegramLarkIntakeTest(unittest.TestCase):
    def test_extract_jira_url_normalizes_trailing_question_mark(self):
        self.assertEqual(
            extract_jira_url("http://jira.notbug.org/browse/FIVEONE-1298?"),
            "http://jira.notbug.org/browse/FIVEONE-1298",
        )

    def test_title_from_text_prefers_non_jira_text(self):
        title = title_from_text(
            "http://jira.notbug.org/browse/FIVEONE-1298?\n"
            "H5&APP侧边栏功能任务赚钱替换为积分商城"
        )

        self.assertEqual(title, "H5&APP侧边栏功能任务赚钱替换为积分商城")

    def test_classify_message_routes_frontend_adjustment_to_demand(self):
        item = TelegramItem(
            chat_id="1",
            chat_name="fiveone-overall",
            message_id=10,
            sent_at=datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc),
            sender="LINA",
            text="H5&APP侧边栏功能任务赚钱替换为积分商城",
        )

        candidate = classify_message(item, "LINA：H5&APP侧边栏功能任务赚钱替换为积分商城")

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.kind, "demand")
        self.assertEqual(candidate.module, "活动/任务中心")

    def test_classify_message_routes_production_ui_problem_to_issue(self):
        item = TelegramItem(
            chat_id="1",
            chat_name="fiveone-overall",
            message_id=11,
            sent_at=datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc),
            sender="Vincent",
            text="@tomkk520 帮忙看下线上后台搜索框位置展示不全",
        )

        candidate = classify_message(item, "Vincent：帮忙看下线上后台搜索框位置展示不全")

        self.assertIsNotNone(candidate)
        self.assertEqual(candidate.kind, "issue")
        self.assertEqual(candidate.module, "后台/运营配置")

    def test_status_from_text_maps_test_and_done_by_table_kind(self):
        self.assertEqual(status_from_text("测试环境发布了", "issue"), "测试环境已解决")
        self.assertEqual(status_from_text("测试环境发布了", "demand"), "测试中")
        self.assertEqual(status_from_text("现在好了", "issue"), "已解决")
        self.assertEqual(status_from_text("现在好了", "demand"), "已完成")

    def test_find_matching_record_matches_jira_or_title_overlap(self):
        records = [
            ExistingRecord(
                table_kind="demand",
                record_id="rec1",
                title="H5&APP 注册时需要默认为铭文密码展示",
                status="待确认",
                jira_url="[http://jira.notbug.org/browse/FIVEONE-1295](http://jira.notbug.org/browse/FIVEONE-1295)",
            ),
            ExistingRecord(
                table_kind="issue",
                record_id="rec2",
                title="线上后台搜索框位置展示不全",
                status="待确认",
                jira_url="",
            ),
        ]

        self.assertEqual(
            find_matching_record("http://jira.notbug.org/browse/FIVEONE-1295?", records).record_id,
            "rec1",
        )
        self.assertEqual(
            find_matching_record("生产后台搜索框位置展示不全，现在好了", records).record_id,
            "rec2",
        )

    def test_build_discussion_uses_nearby_messages_without_source_labels(self):
        items = [
            TelegramItem("1", "g", 1, datetime.now(timezone.utc), "A", "前一句"),
            TelegramItem("1", "g", 2, datetime.now(timezone.utc), "B", "H5页面新增字段"),
            TelegramItem("1", "g", 3, datetime.now(timezone.utc), "C", "收到"),
        ]

        discussion = build_discussion(items, 1)

        self.assertIn("A：前一句", discussion)
        self.assertIn("B：H5页面新增字段", discussion)
        self.assertNotIn("来源群", discussion)
        self.assertNotIn("消息范围", discussion)

    def test_process_items_creates_candidate_and_marks_message_processed(self):
        cfg = IntakeConfig(
            telegram={},
            lark_cli_path="lark-cli",
            base_token="base",
            demand_table_id="demand",
            issue_table_id="issue",
            target_chats=["fiveone-overall"],
            state_path=Path("state.json"),
            bot_report_config_path=None,
            first_run_lookback_minutes=10,
            max_messages_per_chat=200,
            dry_run=True,
        )
        items = [
            TelegramItem(
                chat_id="1",
                chat_name="fiveone-overall",
                message_id=1,
                sent_at=datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc),
                sender="LINA",
                text="利息宝这个字段帮忙加下",
            )
        ]
        state = {}

        created, updated = process_items(cfg, items, [], state)

        self.assertEqual(len(created), 1)
        self.assertEqual(created[0].kind, "demand")
        self.assertEqual(updated, [])
        self.assertIn("1:1", state["processed_message_keys"])

    def test_process_items_updates_matching_record_status(self):
        cfg = IntakeConfig(
            telegram={},
            lark_cli_path="lark-cli",
            base_token="base",
            demand_table_id="demand",
            issue_table_id="issue",
            target_chats=["fiveone-overall"],
            state_path=Path("state.json"),
            bot_report_config_path=None,
            first_run_lookback_minutes=10,
            max_messages_per_chat=200,
            dry_run=False,
        )
        record = ExistingRecord(
            table_kind="issue",
            record_id="rec1",
            title="线上后台搜索框位置展示不全",
            status="待确认",
            jira_url="",
        )
        items = [
            TelegramItem(
                chat_id="1",
                chat_name="fiveone-overall",
                message_id=2,
                sent_at=datetime(2026, 6, 23, 8, 0, tzinfo=timezone.utc),
                sender="tom",
                text="线上后台搜索框位置展示不全，现在好了",
            )
        ]

        with patch("telegram_lark_intake.update_record_status") as update:
            created, updated = process_items(cfg, items, [record], {})

        self.assertEqual(created, [])
        self.assertEqual(len(updated), 1)
        self.assertEqual(updated[0][1], "已解决")
        update.assert_called_once()

    def test_notification_text_summarizes_changes(self):
        candidate = TelegramItem(
            chat_id="1",
            chat_name="fiveone-overall",
            message_id=1,
            sent_at=datetime.now(timezone.utc),
            sender="A",
            text="",
        )
        # Use a tiny object with the same attributes the formatter reads.
        candidate.kind = "issue"
        candidate.title = "线上后台搜索框位置展示不全"

        text = notification_text([candidate], [])

        self.assertIn("新增待确认", text)
        self.assertIn("线上后台搜索框位置展示不全", text)


if __name__ == "__main__":
    unittest.main()
