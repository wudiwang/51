import json
import unittest
from datetime import datetime, timezone

from scripts.telegram_lark_ai_review import (
    AiAction,
    RecentMessage,
    ai_action_to_candidate,
    build_record_payload,
    build_ai_request_payload,
    compact_title,
    notification_text,
    parse_ai_response,
    should_apply_action,
)
from scripts.telegram_lark_intake import ExistingRecord


class TelegramLarkAiReviewTest(unittest.TestCase):
    def test_parse_ai_response_extracts_json_from_response_api_output(self):
        payload = {
            "output": [
                {
                    "content": [
                        {
                            "type": "output_text",
                            "text": json.dumps(
                                {
                                    "actions": [
                                        {
                                            "action": "create_issue",
                                            "confidence": 0.86,
                                            "title": "线上后台搜索框位置展示不全",
                                            "module": "后台/运营配置",
                                            "status": "待确认",
                                            "owner": "tom",
                                            "expected_time": "",
                                            "summary": "Vincent 确认生产环境搜索框展示不全，tom 跟进处理。",
                                            "matched_record_id": "",
                                            "matched_table_kind": "",
                                            "message_keys": ["4844072747:33628"],
                                            "reason": "这是生产环境 UI 展示问题，不是普通聊天。",
                                        }
                                    ]
                                },
                                ensure_ascii=False,
                            ),
                        }
                    ]
                }
            ]
        }

        actions = parse_ai_response(payload)

        self.assertEqual(len(actions), 1)
        self.assertEqual(actions[0].action, "create_issue")
        self.assertEqual(actions[0].title, "线上后台搜索框位置展示不全")
        self.assertEqual(actions[0].message_keys, ["4844072747:33628"])

    def test_should_apply_action_uses_confidence_thresholds(self):
        high = AiAction(action="create_demand", confidence=0.82, title="需求", module="代理/推广")
        mid = AiAction(action="create_issue", confidence=0.62, title="问题", module="后台/运营配置")
        low = AiAction(action="create_issue", confidence=0.41, title="闲聊", module="后台/运营配置")

        self.assertEqual(should_apply_action(high, auto_threshold=0.8, notify_threshold=0.55), "auto")
        self.assertEqual(should_apply_action(mid, auto_threshold=0.8, notify_threshold=0.55), "notify")
        self.assertEqual(should_apply_action(low, auto_threshold=0.8, notify_threshold=0.55), "ignore")

    def test_build_ai_request_payload_limits_messages_and_records(self):
        messages = [
            RecentMessage(
                message_key=f"chat:{i}",
                group="fiveone-overall",
                time="2026-06-23 20:00:00",
                sender="sender",
                text=f"消息 {i}",
            )
            for i in range(30)
        ]
        records = [
            ExistingRecord("issue", f"rec{i}", f"旧问题 {i}", "处理中", "")
            for i in range(120)
        ]

        payload = build_ai_request_payload(messages, records, max_messages=20, max_records=50)

        self.assertEqual(len(payload["messages"]), 20)
        self.assertEqual(payload["messages"][0]["message_key"], "chat:10")
        self.assertEqual(len(payload["known_records"]), 50)
        self.assertEqual(payload["known_records"][0]["record_id"], "rec70")

    def test_ai_action_to_candidate_maps_issue_to_lark_candidate(self):
        action = AiAction(
            action="create_issue",
            confidence=0.91,
            title="代理佣金明细接口请求方式异常",
            module="代理/推广",
            status="待确认",
            owner="Lanis",
            summary="Rene 贴出接口报错，Lanis 本地验证。",
            message_keys=["4844072747:33671"],
        )
        messages = [
            RecentMessage(
                message_key="4844072747:33671",
                group="fiveone-overall",
                time="2026-06-23 13:35:17",
                sender="Rene",
                text="接口报错",
            )
        ]

        candidate = ai_action_to_candidate(action, messages)

        self.assertEqual(candidate.kind, "issue")
        self.assertEqual(candidate.title, "代理佣金明细接口请求方式异常")
        self.assertEqual(candidate.module, "代理/推广")
        self.assertEqual(candidate.sender, "Rene")
        self.assertEqual(candidate.sent_at.tzinfo, timezone.utc)

    def test_notification_text_names_matched_record_for_status_update(self):
        action = AiAction(
            action="update_status",
            confidence=0.9,
            title="正式环境测试通过并已上线通知",
            module="",
            status="已解决",
            matched_record_id="rec1",
            matched_table_kind="demand",
        )
        records = [ExistingRecord("demand", "rec1", "H5&APP首页登录后定位调整", "待确认", "")]

        text = notification_text([action], [], 0, records)

        self.assertIn("更新：", text)
        self.assertIn("需求", text)
        self.assertIn("H5&APP首页登录后定位调整", text)
        self.assertIn("已解决", text)

    def test_create_payload_uses_short_title_and_detailed_description(self):
        action = AiAction(
            action="create_demand",
            confidence=0.91,
            title="H5和APP首页登录之后需要把默认定位调整到用户当前所在国家和城市",
            module="前端/H5&APP",
            status="待确认",
            summary="Ethan 要求登录后首页定位按用户当前国家和城市展示，避免默认定位不准影响入口判断。",
        )
        message = RecentMessage(
            message_key="chat:1",
            group="fiveone-overall",
            time="2026-06-24 14:00:00",
            sender="Ethan",
            text="首页登录后定位调整",
        )

        payload, _table_kind = build_record_payload(action, [message])

        self.assertLessEqual(len(payload["需求名称"]), 16)
        self.assertEqual(payload["详细描述"], action.summary)
        self.assertIn("讨论摘要", payload)

    def test_compact_title_keeps_medium_title_unchanged(self):
        self.assertEqual(compact_title("首页登录定位调整"), "首页登录定位调整")


if __name__ == "__main__":
    unittest.main()
