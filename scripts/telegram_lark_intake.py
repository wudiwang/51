#!/usr/bin/env python3
"""Create and update Lark Base records from Telegram project discussions."""

from __future__ import annotations

import argparse
import asyncio
import json
import re
import shutil
import subprocess
import sys
import tempfile
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterable


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_CONFIG = Path("telegram_lark_intake.config.json")
DEFAULT_STATE = Path("telegram_lark_intake.state.json")
BANGKOK_TZ = timezone(timedelta(hours=7))
JIRA_RE = re.compile(r"https?://jira\.notbug\.org/browse/([A-Z]+-\d+)\??", re.I)


ISSUE_KEYWORDS = [
    "bug",
    "BUG",
    "问题",
    "异常",
    "报错",
    "错误",
    "失败",
    "无法",
    "不能",
    "不显示",
    "展示不全",
    "黑屏",
    "Build FAILURE",
    "构建失败",
    "参数错误",
    "数据不对",
    "显示无数据",
]
DEMAND_KEYWORDS = [
    "需求",
    "优化",
    "调整",
    "新增",
    "加下",
    "替换",
    "支持",
    "功能",
    "去掉",
    "保留",
    "改成",
    "入口",
    "默认",
]
STATUS_DONE_KEYWORDS = [
    "已解决",
    "已修复",
    "修复了",
    "处理好了",
    "现在好了",
    "好了",
    "测试通过",
    "已完成",
    "已上线",
    "更生产",
    "部署了",
]
STATUS_TEST_KEYWORDS = [
    "已发测试",
    "发测试",
    "测试环境发布",
    "测试环境已发布",
    "测试更好了",
]


@dataclass
class IntakeConfig:
    telegram: dict[str, Any]
    lark_cli_path: str
    base_token: str
    demand_table_id: str
    issue_table_id: str
    target_chats: list[str]
    state_path: Path
    bot_report_config_path: Path | None
    first_run_lookback_minutes: int
    max_messages_per_chat: int
    dry_run: bool = False


@dataclass
class TelegramItem:
    chat_id: str
    chat_name: str
    message_id: int
    sent_at: datetime
    sender: str
    text: str


@dataclass
class IntakeCandidate:
    kind: str
    title: str
    module: str
    sent_at: datetime
    sender: str
    jira_url: str | None
    discussion: str
    message_key: str


@dataclass
class ExistingRecord:
    table_kind: str
    record_id: str
    title: str
    status: str
    jira_url: str


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_config(path: Path) -> IntakeConfig:
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

    bot_path = raw.get("bot_report_config_path")
    return IntakeConfig(
        telegram=telegram,
        lark_cli_path=str(raw.get("lark_cli_path", "lark-cli")),
        base_token=str(raw["base_token"]),
        demand_table_id=str(raw["demand_table_id"]),
        issue_table_id=str(raw["issue_table_id"]),
        target_chats=[str(item) for item in raw.get("target_chats", [])],
        state_path=Path(raw.get("state_path", DEFAULT_STATE)),
        bot_report_config_path=Path(bot_path) if bot_path else None,
        first_run_lookback_minutes=int(raw.get("first_run_lookback_minutes", 10)),
        max_messages_per_chat=int(raw.get("max_messages_per_chat", 200)),
        dry_run=bool(raw.get("dry_run", False)),
    )


def display_name(entity: Any) -> str:
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [getattr(entity, "first_name", None), getattr(entity, "last_name", None)]
    name = " ".join(str(part) for part in parts if part).strip()
    if name:
        return name
    username = getattr(entity, "username", None)
    return str(username) if username else ""


def entity_id(entity: Any) -> str:
    value = getattr(entity, "id", "")
    return str(value) if value is not None else ""


def chat_matches_targets(chat: Any, targets: list[str]) -> bool:
    if not targets:
        return True
    normalized = {target.casefold() for target in targets}
    candidates = {display_name(chat).casefold(), entity_id(chat).casefold()}
    username = getattr(chat, "username", None)
    if username:
        candidates.add(str(username).casefold())
    return bool(candidates & normalized)


def clean_text(text: str) -> str:
    text = re.sub(r"@\w+", "", text)
    text = re.sub(r"\s+", " ", text.replace("\n", " ")).strip()
    return text


def extract_jira_url(text: str) -> str | None:
    match = JIRA_RE.search(text)
    if not match:
        return None
    return f"http://jira.notbug.org/browse/{match.group(1).upper()}"


def strip_jira(text: str) -> str:
    return JIRA_RE.sub("", text).strip(" -：:\n\t")


def contains_any(text: str, keywords: Iterable[str]) -> bool:
    return any(keyword in text for keyword in keywords)


def classify_module(text: str) -> str:
    lowered = text.lower()
    if any(term in text for term in ["返水", "VIP", "vip"]):
        return "VIP/返水"
    if any(term in text for term in ["充值", "提现", "财务", "钱包", "支付"]):
        return "支付/充值提现"
    if any(term in text for term in ["代理", "推广", "佣金"]):
        return "代理/推广"
    if any(term in text for term in ["报表", "仪表盘", "统计"]):
        return "报表/仪表盘"
    if any(term in text for term in ["活动", "任务", "利息宝", "积分商城", "预存"]):
        return "活动/任务中心"
    if any(term in text for term in ["游戏", "投注", "注单"]):
        return "游戏/投注/注单"
    if any(term in text for term in ["账号", "注册", "登录", "会员", "密码"]):
        return "会员/账号"
    if any(term in text for term in ["后台", "运营", "搜索框", "管理"]):
        return "后台/运营配置"
    if any(term in text for term in ["生产", "部署", "构建", "Build", "环境", "topic", "Kafka"]):
        return "发布/环境配置"
    if any(term in lowered for term in ["h5", "app"]) or any(term in text for term in ["前端", "页面", "侧边栏"]):
        return "H5/APP前端"
    return "H5/APP前端"


def title_from_text(text: str) -> str:
    without_jira = strip_jira(text)
    lines = [clean_text(line) for line in without_jira.splitlines()]
    lines = [line for line in lines if line]
    title = lines[0] if lines else clean_text(without_jira)
    title = re.sub(r"^(麻烦|帮忙|请|这个|那目前|好的[，,]?)", "", title).strip()
    if len(title) > 70:
        title = title[:67].rstrip() + "..."
    return title or "待确认事项"


def is_status_update(text: str) -> bool:
    return contains_any(text, STATUS_DONE_KEYWORDS) or contains_any(text, STATUS_TEST_KEYWORDS)


def status_from_text(text: str, table_kind: str) -> str | None:
    if contains_any(text, STATUS_TEST_KEYWORDS):
        return "测试中" if table_kind == "demand" else "测试环境已解决"
    if contains_any(text, STATUS_DONE_KEYWORDS):
        return "已完成" if table_kind == "demand" else "已解决"
    return None


def classify_message(item: TelegramItem, discussion: str) -> IntakeCandidate | None:
    text = item.text.strip()
    if not text:
        return None
    if is_status_update(text) and not (contains_any(text, ISSUE_KEYWORDS) or contains_any(text, DEMAND_KEYWORDS)):
        return None

    jira_url = extract_jira_url(text)
    issue_score = contains_any(text, ISSUE_KEYWORDS)
    demand_score = contains_any(text, DEMAND_KEYWORDS)

    if not jira_url and not issue_score and not demand_score:
        return None
    if len(clean_text(text)) < 8 and not jira_url:
        return None

    kind = "issue" if issue_score and not demand_score else "demand"
    if "Build FAILURE" in text or "构建失败" in text:
        kind = "issue"
    if any(term in text for term in ["线上", "生产"]) and issue_score:
        kind = "issue"

    title = title_from_text(text)
    return IntakeCandidate(
        kind=kind,
        title=title,
        module=classify_module(text),
        sent_at=item.sent_at,
        sender=item.sender,
        jira_url=jira_url,
        discussion=discussion,
        message_key=f"{item.chat_id}:{item.message_id}",
    )


def build_discussion(items: list[TelegramItem], index: int) -> str:
    start = max(0, index - 2)
    end = min(len(items), index + 3)
    lines: list[str] = []
    for item in items[start:end]:
        text = clean_text(item.text)
        if not text:
            continue
        label = item.sender or "群成员"
        if len(text) > 160:
            text = text[:157].rstrip() + "..."
        lines.append(f"{label}：{text}")
    return "\n".join(lines)


def normalize_for_match(text: str) -> set[str]:
    cleaned = re.sub(r"https?://\S+", "", text)
    words = re.sub(r"[\W_]+", " ", cleaned, flags=re.UNICODE)
    tokens = {token for token in words.split() if len(token) >= 2}
    tokens.update(re.findall(r"[\u4e00-\u9fff]", cleaned))
    return tokens


def find_matching_record(
    text: str,
    records: list[ExistingRecord],
    table_kind: str | None = None,
) -> ExistingRecord | None:
    jira = extract_jira_url(text)
    candidates = [record for record in records if table_kind is None or record.table_kind == table_kind]
    if jira:
        for record in candidates:
            if jira in record.jira_url:
                return record

    text_tokens = normalize_for_match(text)
    if not text_tokens:
        return None
    best: tuple[float, ExistingRecord] | None = None
    for record in candidates:
        if record.title and (record.title in text or text in record.title):
            return record
        title_tokens = normalize_for_match(record.title)
        if not title_tokens:
            continue
        overlap = len(text_tokens & title_tokens)
        score = overlap / max(1, min(len(text_tokens), len(title_tokens)))
        if score >= 0.35 and (best is None or score > best[0]):
            best = (score, record)
    return best[1] if best else None


def run_lark(cli_path: str, args: list[str], input_text: str | None = None) -> dict[str, Any]:
    executable = shutil.which(cli_path) or cli_path
    result = subprocess.run(
        [executable, *args, "--as", "user", "--format", "json"],
        input=input_text,
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


def record_list(cli_path: str, base_token: str, table_id: str, limit: int = 200) -> dict[str, Any]:
    return run_lark(
        cli_path,
        [
            "base",
            "+record-list",
            "--base-token",
            base_token,
            "--table-id",
            table_id,
            "--limit",
            str(limit),
        ],
    )


def parse_records(payload: dict[str, Any], table_kind: str) -> list[ExistingRecord]:
    data = payload.get("data", {})
    fields = data.get("fields", [])
    rows = data.get("data", [])
    record_ids = data.get("record_id_list", [])
    records: list[ExistingRecord] = []
    title_field = "需求名称" if table_kind == "demand" else "标题"
    for record_id, row in zip(record_ids, rows):
        values = dict(zip(fields, row))
        title = values.get(title_field)
        if not title:
            continue
        status = values.get("状态")
        if isinstance(status, list):
            status_text = str(status[0]) if status else ""
        else:
            status_text = str(status or "")
        records.append(
            ExistingRecord(
                table_kind=table_kind,
                record_id=str(record_id),
                title=str(title),
                status=status_text,
                jira_url=str(values.get("jira地址") or ""),
            )
        )
    return records


def write_temp_json(payload: dict[str, Any]) -> str:
    handle = tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        suffix=".json",
        delete=False,
        dir=".",
    )
    with handle:
        json.dump(payload, handle, ensure_ascii=False, separators=(",", ":"))
    return Path(handle.name).name


def create_record(cfg: IntakeConfig, candidate: IntakeCandidate) -> str | None:
    if cfg.dry_run:
        return None
    if candidate.kind == "demand":
        table_id = cfg.demand_table_id
        payload = {
            "需求名称": candidate.title,
            "所属月份": f"{candidate.sent_at.astimezone(BANGKOK_TZ).month}月",
            "备注": "待确认",
            "状态": "待确认",
            "所需端": ["前端"] if candidate.module == "H5/APP前端" else ["后端", "前端"],
            "提出时间": candidate.sent_at.astimezone(BANGKOK_TZ).strftime("%Y-%m-%d 00:00:00"),
            "讨论摘要": candidate.discussion,
        }
        if candidate.jira_url:
            payload["jira地址"] = f"[{candidate.jira_url}]({candidate.jira_url})"
    else:
        table_id = cfg.issue_table_id
        payload = {
            "标题": candidate.title,
            "模块": candidate.module,
            "提出时间": candidate.sent_at.astimezone(BANGKOK_TZ).strftime("%Y-%m-%d %H:%M:%S"),
            "状态": "待确认",
            "备注": "待确认",
            "讨论摘要": candidate.discussion,
        }
    path = write_temp_json(payload)
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


def update_record_status(cfg: IntakeConfig, record: ExistingRecord, status: str, discussion: str) -> None:
    if cfg.dry_run:
        return
    table_id = cfg.demand_table_id if record.table_kind == "demand" else cfg.issue_table_id
    payload = {"状态": status, "讨论摘要": discussion}
    path = write_temp_json(payload)
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


def send_bot_message(config_path: Path | None, text: str) -> None:
    if not config_path or not config_path.exists() or not text.strip():
        return
    config = load_json(config_path)
    token = config["bot_token"]
    chat_id = config["chat_id"]
    payload = json.dumps(
        {"chat_id": chat_id, "text": text},
        ensure_ascii=False,
    ).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=payload,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def notification_text(created: list[IntakeCandidate], updated: list[tuple[ExistingRecord, str]]) -> str:
    if not created and not updated:
        return ""
    lines = ["Lark 自动巡检有更新："]
    if created:
        lines.append("")
        lines.append("新增待确认：")
        for candidate in created[:10]:
            label = "需求" if candidate.kind == "demand" else "线上问题"
            lines.append(f"- [{label}] {candidate.title}")
    if updated:
        lines.append("")
        lines.append("状态更新：")
        for record, status in updated[:10]:
            lines.append(f"- {record.title} -> {status}")
    return "\n".join(lines)


async def fetch_new_items(cfg: IntakeConfig, state: dict[str, Any]) -> list[TelegramItem]:
    from telethon import TelegramClient

    tg = cfg.telegram
    client = TelegramClient(tg["session"], tg["api_id"], tg["api_hash"])
    await client.start(phone=tg.get("phone"))
    state_chats = state.setdefault("chats", {})
    all_items: list[TelegramItem] = []
    now = datetime.now(timezone.utc)
    first_cutoff = now - timedelta(minutes=cfg.first_run_lookback_minutes)

    async for dialog in client.iter_dialogs():
        chat = dialog.entity
        if not getattr(dialog, "is_group", False) and not getattr(dialog, "is_channel", False):
            continue
        if not chat_matches_targets(chat, cfg.target_chats):
            continue

        chat_id = entity_id(chat)
        chat_name = display_name(chat)
        last_seen = int(state_chats.get(chat_id, {}).get("last_seen_message_id", 0))
        new_items: list[TelegramItem] = []
        max_seen = last_seen
        async for message in client.iter_messages(chat, limit=cfg.max_messages_per_chat):
            msg_id = int(getattr(message, "id", 0) or 0)
            msg_date = getattr(message, "date", None)
            if msg_id <= last_seen:
                break
            if last_seen == 0 and msg_date and msg_date < first_cutoff:
                break
            sender = await message.get_sender()
            text = getattr(message, "raw_text", "") or ""
            new_items.append(
                TelegramItem(
                    chat_id=chat_id,
                    chat_name=chat_name,
                    message_id=msg_id,
                    sent_at=msg_date or now,
                    sender=display_name(sender),
                    text=text,
                )
            )
            max_seen = max(max_seen, msg_id)
        if max_seen > last_seen:
            state_chats[chat_id] = {"name": chat_name, "last_seen_message_id": max_seen}
        all_items.extend(reversed(new_items))

    await client.disconnect()
    return sorted(all_items, key=lambda item: (item.chat_id, item.message_id))


def process_items(
    cfg: IntakeConfig,
    items: list[TelegramItem],
    existing_records: list[ExistingRecord],
    state: dict[str, Any],
) -> tuple[list[IntakeCandidate], list[tuple[ExistingRecord, str]]]:
    processed = set(state.setdefault("processed_message_keys", []))
    created: list[IntakeCandidate] = []
    updated: list[tuple[ExistingRecord, str]] = []

    for index, item in enumerate(items):
        key = f"{item.chat_id}:{item.message_id}"
        if key in processed:
            continue
        discussion = build_discussion(items, index)
        text = item.text
        if is_status_update(text):
            record = find_matching_record(text, existing_records)
            if record:
                status = status_from_text(text, record.table_kind)
                if status and status != record.status:
                    update_record_status(cfg, record, status, discussion)
                    record.status = status
                    updated.append((record, status))
                    processed.add(key)
                    continue

        candidate = classify_message(item, discussion)
        if not candidate:
            processed.add(key)
            continue
        duplicate = find_matching_record(candidate.title + " " + (candidate.jira_url or ""), existing_records, candidate.kind)
        if duplicate:
            processed.add(key)
            continue
        create_record(cfg, candidate)
        created.append(candidate)
        existing_records.append(
            ExistingRecord(
                table_kind=candidate.kind,
                record_id="",
                title=candidate.title,
                status="待确认",
                jira_url=candidate.jira_url or "",
            )
        )
        processed.add(key)

    state["processed_message_keys"] = list(processed)[-1000:]
    return created, updated


async def run_once(config_path: Path) -> None:
    cfg = load_config(config_path)
    state = load_json(cfg.state_path) if cfg.state_path.exists() else {}
    items = await fetch_new_items(cfg, state)
    demand_records = parse_records(
        record_list(cfg.lark_cli_path, cfg.base_token, cfg.demand_table_id),
        "demand",
    )
    issue_records = parse_records(
        record_list(cfg.lark_cli_path, cfg.base_token, cfg.issue_table_id),
        "issue",
    )
    created, updated = process_items(cfg, items, demand_records + issue_records, state)
    save_json(cfg.state_path, state)
    message = notification_text(created, updated)
    if message and not cfg.dry_run:
        send_bot_message(cfg.bot_report_config_path, message)
    print(json.dumps({"messages": len(items), "created": len(created), "updated": len(updated)}, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("command", choices=["run-once"])
    args = parser.parse_args()
    if args.command == "run-once":
        asyncio.run(run_once(args.config))


if __name__ == "__main__":
    main()
