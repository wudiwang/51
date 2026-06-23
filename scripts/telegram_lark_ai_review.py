#!/usr/bin/env python3
"""AI-assisted Telegram discussion review for Lark Base intake."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any

try:
    from telegram_lark_intake import (
        BANGKOK_TZ,
        ExistingRecord,
        IntakeCandidate,
        clean_text,
        display_name,
        entity_id,
        extract_jira_url,
        find_matching_record,
        load_json,
        parse_records,
        record_list,
        run_lark,
        send_bot_message,
    )
except ModuleNotFoundError:
    from scripts.telegram_lark_intake import (
        BANGKOK_TZ,
        ExistingRecord,
        IntakeCandidate,
        clean_text,
        display_name,
        entity_id,
        extract_jira_url,
        find_matching_record,
        load_json,
        parse_records,
        record_list,
        run_lark,
        send_bot_message,
    )


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


ACTION_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "properties": {
        "actions": {
            "type": "array",
            "items": {
                "type": "object",
                "additionalProperties": False,
                "properties": {
                    "action": {
                        "type": "string",
                        "enum": [
                            "create_demand",
                            "create_issue",
                            "update_status",
                            "notify_only",
                            "ignore",
                        ],
                    },
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "title": {"type": "string"},
                    "module": {"type": "string"},
                    "status": {"type": "string"},
                    "owner": {"type": "string"},
                    "expected_time": {"type": "string"},
                    "summary": {"type": "string"},
                    "matched_record_id": {"type": "string"},
                    "matched_table_kind": {"type": "string", "enum": ["", "demand", "issue"]},
                    "message_keys": {"type": "array", "items": {"type": "string"}},
                    "reason": {"type": "string"},
                },
                "required": [
                    "action",
                    "confidence",
                    "title",
                    "module",
                    "status",
                    "owner",
                    "expected_time",
                    "summary",
                    "matched_record_id",
                    "matched_table_kind",
                    "message_keys",
                    "reason",
                ],
            },
        }
    },
    "required": ["actions"],
}


SYSTEM_PROMPT = """你是 51 重构项目的项目管理自动登记助手。
只根据输入的 Telegram 群消息和已知 Lark 记录做判断，不要编造。

目标：
1. 判断消息是否是新需求、线上问题/Bug、已有事项状态更新、仅需提醒 Steven、或普通聊天。
2. 需求和问题必须区分：
   - 需求：功能新增、调整、去掉、改口径、交互优化、Jira 需求。
   - 线上问题/Bug：生产/测试异常、报错、数据不对、展示错误、构建失败、发布风险。
   - 状态更新：已解决、测试环境已发布、已部署、现在好了、待测试等，并且能匹配已有事项。
3. 输出必须是 JSON，严格符合 schema。
4. 标题要像项目管理记录，不要直接复制“这个也有问题”“我本地试试”等聊天短句。
5. 只有有明确事项价值时才输出 create/update；普通确认、感谢、1、闲聊输出 ignore 或不输出。
6. 如果不确定但可能重要，输出 notify_only，confidence 在 0.55-0.79。
7. 高置信度 action 才应超过 0.8。
8. 不要暴露任何密钥、手机号、token、session 等敏感信息。
"""


@dataclass
class AiReviewConfig:
    telegram: dict[str, Any]
    lark_cli_path: str
    base_token: str
    demand_table_id: str
    issue_table_id: str
    target_chats: list[str]
    state_path: Path
    bot_report_config_path: Path | None
    openai_config_path: Path
    model: str = "gpt-4.1-mini"
    lookback_minutes: int = 35
    max_messages_per_chat: int = 300
    max_messages_for_ai: int = 120
    max_records_for_ai: int = 120
    auto_threshold: float = 0.8
    notify_threshold: float = 0.55
    dry_run: bool = False


@dataclass
class RecentMessage:
    message_key: str
    group: str
    time: str
    sender: str
    text: str


@dataclass
class AiAction:
    action: str
    confidence: float
    title: str
    module: str
    status: str = ""
    owner: str = ""
    expected_time: str = ""
    summary: str = ""
    matched_record_id: str = ""
    matched_table_kind: str = ""
    message_keys: list[str] = field(default_factory=list)
    reason: str = ""


def load_config(path: Path) -> AiReviewConfig:
    raw = load_json(path)
    telegram = raw.get("telegram")
    telegram_config_path = raw.get("telegram_config_path")
    if telegram is None and telegram_config_path:
        source_path = Path(telegram_config_path)
        telegram = load_json(source_path)["telegram"]
        session = telegram.get("session")
        if session and not Path(str(session)).is_absolute():
            telegram["session"] = str(source_path.parent / str(session))
    if telegram is None:
        raise ValueError("config must include telegram or telegram_config_path")
    return AiReviewConfig(
        telegram=telegram,
        lark_cli_path=str(raw.get("lark_cli_path", "lark-cli")),
        base_token=str(raw["base_token"]),
        demand_table_id=str(raw["demand_table_id"]),
        issue_table_id=str(raw["issue_table_id"]),
        target_chats=[str(item) for item in raw.get("target_chats", [])],
        state_path=Path(raw.get("state_path", "secrets/telegram_lark_ai_review.state.json")),
        bot_report_config_path=Path(raw["bot_report_config_path"]) if raw.get("bot_report_config_path") else None,
        openai_config_path=Path(raw.get("openai_config_path", "secrets/openai.config.json")),
        model=str(raw.get("model", "gpt-4.1-mini")),
        lookback_minutes=int(raw.get("lookback_minutes", 35)),
        max_messages_per_chat=int(raw.get("max_messages_per_chat", 300)),
        max_messages_for_ai=int(raw.get("max_messages_for_ai", 120)),
        max_records_for_ai=int(raw.get("max_records_for_ai", 120)),
        auto_threshold=float(raw.get("auto_threshold", 0.8)),
        notify_threshold=float(raw.get("notify_threshold", 0.55)),
        dry_run=bool(raw.get("dry_run", False)),
    )


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def chat_matches_targets(chat: Any, targets: list[str]) -> bool:
    if not targets:
        return True
    normalized = {target.casefold() for target in targets}
    candidates = {display_name(chat).casefold(), entity_id(chat).casefold()}
    username = getattr(chat, "username", None)
    if username:
        candidates.add(str(username).casefold())
    return bool(candidates & normalized)


async def fetch_recent_messages(cfg: AiReviewConfig) -> list[RecentMessage]:
    from telethon import TelegramClient

    cutoff = datetime.now(timezone.utc) - timedelta(minutes=cfg.lookback_minutes)
    tg = cfg.telegram
    client = TelegramClient(tg["session"], int(tg["api_id"]), tg["api_hash"])
    await client.start(phone=tg.get("phone"))
    messages: list[RecentMessage] = []
    async for dialog in client.iter_dialogs():
        chat = dialog.entity
        if not getattr(dialog, "is_group", False) and not getattr(dialog, "is_channel", False):
            continue
        if not chat_matches_targets(chat, cfg.target_chats):
            continue
        group = display_name(chat)
        chat_id = entity_id(chat)
        async for message in client.iter_messages(chat, limit=cfg.max_messages_per_chat):
            sent_at = getattr(message, "date", None)
            if sent_at and sent_at < cutoff:
                break
            text = clean_text(getattr(message, "raw_text", "") or "")
            if not text or len(text) < 2:
                continue
            sender = await message.get_sender()
            message_id = int(getattr(message, "id", 0) or 0)
            messages.append(
                RecentMessage(
                    message_key=f"{chat_id}:{message_id}",
                    group=group,
                    time=(sent_at or datetime.now(timezone.utc))
                    .astimezone(BANGKOK_TZ)
                    .strftime("%Y-%m-%d %H:%M:%S"),
                    sender=display_name(sender),
                    text=text[:1200],
                )
            )
    await client.disconnect()
    return sorted(messages, key=lambda item: (item.time, item.group, item.message_key))


def build_ai_request_payload(
    messages: list[RecentMessage],
    records: list[ExistingRecord],
    max_messages: int = 120,
    max_records: int = 120,
) -> dict[str, Any]:
    return {
        "task": "review_telegram_messages_for_lark_intake",
        "messages": [message.__dict__ for message in messages[-max_messages:]],
        "known_records": [
            {
                "table_kind": record.table_kind,
                "record_id": record.record_id,
                "title": record.title,
                "status": record.status,
                "jira_url": record.jira_url,
            }
            for record in records[-max_records:]
        ],
    }


def openai_api_key(config_path: Path) -> str:
    config = load_json(config_path)
    key = config.get("api_key") or config.get("OPENAI_API_KEY") or os.environ.get("OPENAI_API_KEY")
    if not key:
        raise ValueError("OpenAI API key missing")
    return str(key)


def call_openai(cfg: AiReviewConfig, payload: dict[str, Any]) -> dict[str, Any]:
    body = json.dumps(
        {
            "model": cfg.model,
            "input": [
                {"role": "system", "content": SYSTEM_PROMPT},
                {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
            ],
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "telegram_lark_actions",
                    "schema": ACTION_SCHEMA,
                    "strict": True,
                }
            },
        },
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        "https://api.openai.com/v1/responses",
        data=body,
        headers={
            "Authorization": f"Bearer {openai_api_key(cfg.openai_config_path)}",
            "Content-Type": "application/json; charset=utf-8",
        },
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=60) as response:
        return json.loads(response.read().decode("utf-8"))


def parse_ai_response(response: dict[str, Any]) -> list[AiAction]:
    text_parts: list[str] = []
    if response.get("output_text"):
        text_parts.append(str(response["output_text"]))
    for item in response.get("output", []):
        for content in item.get("content", []):
            if "text" in content:
                text_parts.append(str(content["text"]))
    raw_text = "\n".join(text_parts).strip()
    if not raw_text:
        raise ValueError("OpenAI response did not include output text")
    parsed = json.loads(raw_text)
    actions: list[AiAction] = []
    for item in parsed.get("actions", []):
        actions.append(
            AiAction(
                action=str(item.get("action", "ignore")),
                confidence=float(item.get("confidence", 0)),
                title=str(item.get("title", "")).strip(),
                module=str(item.get("module", "")).strip(),
                status=str(item.get("status", "")).strip(),
                owner=str(item.get("owner", "")).strip(),
                expected_time=str(item.get("expected_time", "")).strip(),
                summary=str(item.get("summary", "")).strip(),
                matched_record_id=str(item.get("matched_record_id", "")).strip(),
                matched_table_kind=str(item.get("matched_table_kind", "")).strip(),
                message_keys=[str(key) for key in item.get("message_keys", [])],
                reason=str(item.get("reason", "")).strip(),
            )
        )
    return actions


def should_apply_action(action: AiAction, auto_threshold: float, notify_threshold: float) -> str:
    if action.action == "ignore" or action.confidence < notify_threshold:
        return "ignore"
    if action.confidence >= auto_threshold and action.action in {"create_demand", "create_issue", "update_status"}:
        return "auto"
    return "notify"


def parse_bangkok_time(value: str) -> datetime:
    local = datetime.strptime(value, "%Y-%m-%d %H:%M:%S").replace(tzinfo=BANGKOK_TZ)
    return local.astimezone(timezone.utc)


def ai_action_to_candidate(action: AiAction, messages: list[RecentMessage]) -> IntakeCandidate:
    by_key = {message.message_key: message for message in messages}
    source = by_key.get(action.message_keys[0]) if action.message_keys else None
    if source is None and messages:
        source = messages[-1]
    sent_at = parse_bangkok_time(source.time) if source else datetime.now(timezone.utc)
    text_blob = "\n".join(by_key[key].text for key in action.message_keys if key in by_key)
    return IntakeCandidate(
        kind="demand" if action.action == "create_demand" else "issue",
        title=action.title,
        module=action.module or "H5/APP前端",
        sent_at=sent_at,
        sender=source.sender if source else "",
        jira_url=extract_jira_url(text_blob),
        discussion=action.summary,
        message_key=",".join(action.message_keys),
    )


def write_json_file(payload: dict[str, Any]) -> str:
    handle = tempfile.NamedTemporaryFile("w", encoding="utf-8", suffix=".json", delete=False, dir=".")
    with handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    return Path(handle.name).name


def upsert_action_record(cfg: AiReviewConfig, action: AiAction, messages: list[RecentMessage]) -> str | None:
    candidate = ai_action_to_candidate(action, messages)
    table_id = cfg.demand_table_id if candidate.kind == "demand" else cfg.issue_table_id
    if candidate.kind == "demand":
        payload: dict[str, Any] = {
            "需求名称": candidate.title,
            "所属月份": f"{candidate.sent_at.astimezone(BANGKOK_TZ).month}月",
            "备注": action.reason or "AI识别，待确认",
            "状态": action.status or "待确认",
            "所需端": ["前端"] if "前端" in (action.module + candidate.title) else ["后端", "前端"],
            "提出时间": candidate.sent_at.astimezone(BANGKOK_TZ).strftime("%Y-%m-%d 00:00:00"),
            "讨论摘要": candidate.discussion,
        }
        if action.owner:
            payload["前端开发"] = action.owner
        if action.expected_time:
            payload["前端计划完成时间"] = action.expected_time
        if candidate.jira_url:
            payload["jira地址"] = f"[{candidate.jira_url}]({candidate.jira_url})"
    else:
        payload = {
            "标题": candidate.title,
            "模块": candidate.module,
            "提出时间": candidate.sent_at.astimezone(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "状态": action.status or "待确认",
            "备注": action.reason or "AI识别，待确认",
            "讨论摘要": candidate.discussion,
        }
        if action.owner:
            payload["解决人"] = action.owner
        if action.expected_time:
            payload["预计解决时间"] = action.expected_time
    if cfg.dry_run:
        return None
    path = write_json_file(payload)
    try:
        result = run_lark(
            cfg.lark_cli_path,
            [
                "base",
                "+record-upsert",
                "--base-token",
                cfg.base_token,
                "--table-id",
                table_id,
                "--json",
                f"@{path}",
            ],
        )
        return result.get("data", {}).get("record", {}).get("record_id")
    finally:
        Path(path).unlink(missing_ok=True)


def update_status_from_action(cfg: AiReviewConfig, action: AiAction, records: list[ExistingRecord]) -> bool:
    record = None
    if action.matched_record_id:
        record = next((item for item in records if item.record_id == action.matched_record_id), None)
    if record is None:
        record = find_matching_record(action.title + " " + action.summary, records, action.matched_table_kind or None)
    if record is None:
        return False
    table_id = cfg.demand_table_id if record.table_kind == "demand" else cfg.issue_table_id
    payload = {"状态": action.status or "待确认", "讨论摘要": action.summary}
    if cfg.dry_run:
        return True
    path = write_json_file(payload)
    try:
        run_lark(
            cfg.lark_cli_path,
            [
                "base",
                "+record-upsert",
                "--base-token",
                cfg.base_token,
                "--table-id",
                table_id,
                "--record-id",
                record.record_id,
                "--json",
                f"@{path}",
            ],
        )
        return True
    finally:
        Path(path).unlink(missing_ok=True)


def notification_text(auto_actions: list[AiAction], notify_actions: list[AiAction], ignored_count: int) -> str:
    if not auto_actions and not notify_actions:
        return ""
    lines = ["AI 复核 Telegram 群消息结果："]
    if auto_actions:
        lines.append("")
        lines.append("已自动写入/更新：")
        for action in auto_actions[:10]:
            lines.append(f"- {action.title}（{action.action}，置信度 {action.confidence:.2f}）")
    if notify_actions:
        lines.append("")
        lines.append("需要 Steven 确认：")
        for action in notify_actions[:10]:
            lines.append(f"- {action.title}（{action.action}，置信度 {action.confidence:.2f}）")
            if action.reason:
                lines.append(f"  原因：{action.reason}")
    lines.append("")
    lines.append(f"本轮忽略/低置信度：{ignored_count} 条")
    return "\n".join(lines)


async def run_once(config_path: Path) -> None:
    cfg = load_config(config_path)
    state = load_json(cfg.state_path) if cfg.state_path.exists() else {}
    reviewed = set(state.get("reviewed_message_keys", []))
    messages = [message for message in await fetch_recent_messages(cfg) if message.message_key not in reviewed]
    demand_records = parse_records(record_list(cfg.lark_cli_path, cfg.base_token, cfg.demand_table_id), "demand")
    issue_records = parse_records(record_list(cfg.lark_cli_path, cfg.base_token, cfg.issue_table_id), "issue")
    records = demand_records + issue_records
    if not messages:
        print(json.dumps({"messages": 0, "actions": 0, "auto": 0, "notify": 0}, ensure_ascii=False))
        return
    payload = build_ai_request_payload(messages, records, cfg.max_messages_for_ai, cfg.max_records_for_ai)
    actions = parse_ai_response(call_openai(cfg, payload))
    auto_actions: list[AiAction] = []
    notify_actions: list[AiAction] = []
    ignored = 0
    for action in actions:
        decision = should_apply_action(action, cfg.auto_threshold, cfg.notify_threshold)
        if decision == "ignore":
            ignored += 1
            continue
        if decision == "notify":
            notify_actions.append(action)
            continue
        if action.action in {"create_demand", "create_issue"}:
            duplicate = find_matching_record(action.title + " " + action.summary, records, "demand" if action.action == "create_demand" else "issue")
            if duplicate:
                ignored += 1
                continue
            upsert_action_record(cfg, action, messages)
            auto_actions.append(action)
        elif action.action == "update_status":
            if update_status_from_action(cfg, action, records):
                auto_actions.append(action)
            else:
                notify_actions.append(action)
    for message in messages:
        reviewed.add(message.message_key)
    state["reviewed_message_keys"] = list(reviewed)[-2000:]
    save_json(cfg.state_path, state)
    text = notification_text(auto_actions, notify_actions, ignored)
    if text and not cfg.dry_run:
        send_bot_message(cfg.bot_report_config_path, text)
    print(json.dumps({"messages": len(messages), "actions": len(actions), "auto": len(auto_actions), "notify": len(notify_actions)}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("telegram_lark_ai_review.config.json"))
    parser.add_argument("command", choices=["run-once"])
    args = parser.parse_args()
    if args.command == "run-once":
        asyncio.run(run_once(args.config))


if __name__ == "__main__":
    main()
