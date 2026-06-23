import sys
import unittest
from pathlib import Path


sys.path.insert(0, str(Path(__file__).resolve().parent / "scripts"))

from lark_schedule_forms import (
    LarkRecord,
    ScheduleConfig,
    demand_missing,
    form_url,
    issue_missing,
    reminder_text,
)


def cfg():
    return ScheduleConfig(
        lark_cli_path="lark-cli",
        base_token="base",
        demand_table_id="tblDemand",
        issue_table_id="tblIssue",
        feedback_table_id="tblFeedback",
        form_table_id="tblFeedback",
        form_view_id="vewForm",
        form_base_url="https://example.larksuite.com/wiki/baseNode",
        bot_report_config_path=Path("bot.json"),
        telegram_chat_id=None,
        dry_run=True,
    )


class ScheduleFormsTest(unittest.TestCase):
    def test_demand_missing_frontend_owner_and_backend_time(self):
        record = LarkRecord(
            table_kind="demand",
            record_id="rec1",
            title="利息宝新增字段",
            values={
                "状态": ["进行中"],
                "所需端": ["前端", "后端"],
                "后端开发": ["aiden"],
                "后端计划完成时间": None,
            },
        )

        self.assertEqual(
            demand_missing(record),
            ["缺前端负责人", "缺后端计划完成时间"],
        )

    def test_done_demand_is_not_reported(self):
        record = LarkRecord(
            table_kind="demand",
            record_id="rec1",
            title="已完成需求",
            values={"状态": ["已完成"], "所需端": ["前端"]},
        )

        self.assertEqual(demand_missing(record), [])

    def test_issue_missing_owner_before_time(self):
        record = LarkRecord(
            table_kind="issue",
            record_id="rec2",
            title="线上搜索框展示不全",
            values={"状态": ["处理中"], "解决人": None, "预计解决时间": None},
        )

        self.assertEqual(issue_missing(record), ["缺解决人"])

    def test_issue_missing_time_after_owner_exists(self):
        record = LarkRecord(
            table_kind="issue",
            record_id="rec2",
            title="线上搜索框展示不全",
            values={"状态": ["处理中"], "解决人": "tom", "预计解决时间": None},
        )

        self.assertEqual(issue_missing(record), ["缺预计解决时间"])

    def test_form_url_prefills_record_identity(self):
        record = LarkRecord(
            table_kind="demand",
            record_id="rec123",
            title="H5&APP侧边栏功能任务赚钱替换为积分商城",
            values={"所需端": ["前端"]},
        )

        url = form_url(cfg(), record)

        self.assertIn("table=tblFeedback", url)
        self.assertIn("view=vewForm", url)
        self.assertIn("prefill_%E5%8E%9F%E8%AE%B0%E5%BD%95ID=rec123", url)
        self.assertIn("prefill_%E4%BA%8B%E9%A1%B9%E7%B1%BB%E5%9E%8B=%E9%9C%80%E6%B1%82", url)

    def test_reminder_text_contains_forwardable_group_prompt(self):
        record = LarkRecord(
            table_kind="issue",
            record_id="rec2",
            title="线上搜索框展示不全",
            values={},
        )
        text = reminder_text([], 2, 3)
        self.assertIn("需求 2 个", text)

        text = reminder_text(
            [
                type(
                    "Missing",
                    (),
                    {
                        "record": record,
                        "missing": ["缺解决人"],
                        "url": "https://example.com",
                    },
                )()
            ],
            1,
            1,
        )
        self.assertIn("@柯南 @Aiden @afeng", text)
        self.assertIn("点击按钮", text)


if __name__ == "__main__":
    unittest.main()
