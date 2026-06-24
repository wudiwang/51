#!/usr/bin/env python3
"""Daily Lark scheduling reminders and feedback writeback."""

from __future__ import annotations

import argparse
import json
import shutil
import subprocess
import sys
import tempfile
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


BANGKOK_TZ = timezone(timedelta(hours=7))
DEFAULT_CONFIG = Path("lark_schedule_forms.config.json")


@dataclass
class ScheduleConfig:
    lark_cli_path: str
    base_token: str
    demand_table_id: str
    issue_table_id: str
    feedback_table_id: str
    form_table_id: str
    form_view_id: str
    form_base_url: str
    bot_report_config_path: Path
    telegram_chat_id: str | None
    max_items: int = 20
    dry_run: bool = False


@dataclass
class LarkRecord:
    table_kind: str
    record_id: str
    title: str
    values: dict[str, Any]


@dataclass
class MissingItem:
    record: LarkRecord
    missing: list[str]
    url: str


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def load_config(path: Path) -> ScheduleConfig:
    raw = load_json(path)
    return ScheduleConfig(
        lark_cli_path=str(raw.get("lark_cli_path", "lark-cli")),
        base_token=str(raw["base_token"]),
        demand_table_id=str(raw["demand_table_id"]),
        issue_table_id=str(raw["issue_table_id"]),
        feedback_table_id=str(raw["feedback_table_id"]),
        form_table_id=str(raw.get("form_table_id", raw["feedback_table_id"])),
        form_view_id=str(raw["form_view_id"]),
        form_base_url=str(raw["form_base_url"]),
        bot_report_config_path=Path(raw["bot_report_config_path"]),
        telegram_chat_id=str(raw["telegram_chat_id"]) if raw.get("telegram_chat_id") else None,
        max_items=int(raw.get("max_items", 20)),
        dry_run=bool(raw.get("dry_run", False)),
    )


def run_lark(cli_path: str, args: list[str]) -> dict[str, Any]:
    executable = shutil.which(cli_path) or cli_path
    result = subprocess.run(
        [executable, *args, "--as", "user", "--format", "json"],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    return json.loads(result.stdout)


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


def list_records(cfg: ScheduleConfig, table_id: str) -> tuple[list[str], list[str], list[list[Any]]]:
    payload = run_lark(
        cfg.lark_cli_path,
        [
            "base",
            "+record-list",
            "--base-token",
            cfg.base_token,
            "--table-id",
            table_id,
            "--limit",
            "200",
        ],
    )
    data = payload.get("data", {})
    return data.get("fields", []), data.get("record_id_list", []), data.get("data", [])


def first_select(value: Any) -> str:
    if isinstance(value, list):
        return str(value[0]) if value else ""
    return str(value or "")


def is_blank(value: Any) -> bool:
    if value is None:
        return True
    if isinstance(value, list):
        return len(value) == 0 or all(is_blank(item) for item in value)
    return str(value).strip() == ""


def parse_date(value: Any) -> datetime | None:
    if not value:
        return None
    text = str(value)
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%d", "%Y/%m/%d"):
        try:
            return datetime.strptime(text[:19], fmt).replace(tzinfo=BANGKOK_TZ)
        except ValueError:
            continue
    return None


def pending_status(table_kind: str, status: str) -> bool:
    if table_kind == "demand":
        return status not in {"已完成"}
    return status not in {"已解决", "测试环境已解决", "二期优化"}


def records_from_table(cfg: ScheduleConfig, table_kind: str) -> list[LarkRecord]:
    table_id = cfg.demand_table_id if table_kind == "demand" else cfg.issue_table_id
    fields, record_ids, rows = list_records(cfg, table_id)
    title_field = "需求名称" if table_kind == "demand" else "标题"
    records: list[LarkRecord] = []
    for record_id, row in zip(record_ids, rows):
        values = dict(zip(fields, row))
        title = values.get(title_field)
        if title:
            records.append(LarkRecord(table_kind, str(record_id), str(title), values))
    return records


def demand_missing(record: LarkRecord) -> list[str]:
    values = record.values
    status = first_select(values.get("状态"))
    if not pending_status("demand", status):
        return []
    need = set(values.get("所需端") or [])
    missing: list[str] = []
    if "前端" in need:
        if is_blank(values.get("前端开发")):
            missing.append("缺前端负责人")
        elif is_blank(values.get("前端计划完成时间")):
            missing.append("缺前端计划完成时间")
    if "后端" in need:
        if is_blank(values.get("后端开发")):
            missing.append("缺后端负责人")
        elif is_blank(values.get("后端计划完成时间")):
            missing.append("缺后端计划完成时间")
    return missing


def issue_missing(record: LarkRecord) -> list[str]:
    values = record.values
    status = first_select(values.get("状态"))
    if not pending_status("issue", status):
        return []
    missing: list[str] = []
    if is_blank(values.get("解决人")):
        missing.append("缺解决人")
    elif is_blank(values.get("预计解决时间")):
        missing.append("缺预计解决时间")
    return missing


def form_url(cfg: ScheduleConfig, record: LarkRecord) -> str:
    issue_type = "需求" if record.table_kind == "demand" else "线上问题"
    table_id = cfg.demand_table_id if record.table_kind == "demand" else cfg.issue_table_id
    need = record.values.get("所需端") if record.table_kind == "demand" else ["线上问题"]
    params = {
        "prefill_事项标题": record.title,
        "prefill_事项类型": issue_type,
        "prefill_原表ID": table_id,
        "prefill_原记录ID": record.record_id,
        "prefill_所需端": ",".join(need or []),
        "prefill_回写状态": "待回写",
    }
    separator = "&" if "?" in cfg.form_base_url else "?"
    return cfg.form_base_url + separator + urllib.parse.urlencode(params, doseq=False)


def collect_missing_items(cfg: ScheduleConfig) -> tuple[list[MissingItem], int, int]:
    demand_records = records_from_table(cfg, "demand")
    issue_records = records_from_table(cfg, "issue")
    today = datetime.now(BANGKOK_TZ).date()
    today_demands = sum(
        1 for record in demand_records if (parse_date(record.values.get("提出时间")) or datetime.min.replace(tzinfo=BANGKOK_TZ)).date() == today
    )
    today_issues = sum(
        1 for record in issue_records if (parse_date(record.values.get("提出时间")) or datetime.min.replace(tzinfo=BANGKOK_TZ)).date() == today
    )
    missing: list[MissingItem] = []
    for record in demand_records:
        fields = demand_missing(record)
        if fields:
            missing.append(MissingItem(record, fields, form_url(cfg, record)))
    for record in issue_records:
        fields = issue_missing(record)
        if fields:
            missing.append(MissingItem(record, fields, form_url(cfg, record)))
    return missing[: cfg.max_items], today_demands, today_issues


def record_date(record: LarkRecord, field_names: list[str]) -> datetime | None:
    for field in field_names:
        value = record.values.get(field)
        parsed = parse_date(value)
        if parsed:
            return parsed
    return None


def status_counts(records: list[LarkRecord]) -> dict[str, int]:
    counts: dict[str, int] = {}
    for record in records:
        status = first_select(record.values.get("状态")) or "未填写"
        counts[status] = counts.get(status, 0) + 1
    return counts


def format_counts(counts: dict[str, int]) -> str:
    if not counts:
        return "暂无记录"
    return "，".join(f"{name} {count} 个" for name, count in sorted(counts.items()))


def daily_summary_text(
    demand_records: list[LarkRecord],
    issue_records: list[LarkRecord],
    items: list[MissingItem],
    today: str | None = None,
) -> str:
    target_date = datetime.strptime(today, "%Y-%m-%d").date() if today else datetime.now(BANGKOK_TZ).date()
    label = target_date.strftime("%m/%d")
    today_demands = [
        record
        for record in demand_records
        if (record_date(record, ["提出时间", "创建时间"]) or datetime.min.replace(tzinfo=BANGKOK_TZ)).date() == target_date
    ]
    today_issues = [
        record
        for record in issue_records
        if (record_date(record, ["提出时间", "创建时间"]) or datetime.min.replace(tzinfo=BANGKOK_TZ)).date() == target_date
    ]
    changed_demands = [
        record
        for record in demand_records
        if (record_date(record, ["更新时间", "最近修改", "修改时间"]) or datetime.min.replace(tzinfo=BANGKOK_TZ)).date() == target_date
    ]
    lines = [f"【每日项目进度总结 {label} 18:00】", ""]
    lines.append(f"1. 今天新收录需求：{len(today_demands)} 个")
    lines.append(f"2. 今天新切入问题：{len(today_issues)} 个")
    lines.append(f"3. 今天有变更的需求：{len(changed_demands)} 个")
    lines.append("")
    lines.append("4. 需求和 Bug 的处理进度")
    lines.append(f"- 需求进度：共 {len(demand_records)} 个，{format_counts(status_counts(demand_records))}")
    lines.append(f"- Bug/线上问题进度：共 {len(issue_records)} 个，{format_counts(status_counts(issue_records))}")
    lines.append("")
    if items:
        lines.append(f"5. 需要补齐排期的信息：{len(items)} 个")
        for index, item in enumerate(items[:10], 1):
            label = "需求" if item.record.table_kind == "demand" else "线上问题"
            lines.append(f"{index}. [{label}] {item.record.title}")
            lines.append(f"   缺：{'、'.join(item.missing)}")
    else:
        lines.append("5. 需要补齐排期的信息：0 个")
    lines.append("")
    lines.append("Steven 自查：今天重点看新增是否已入表、变更是否有 owner、待排期项是否已经明确下一步。")
    return "\n".join(lines)


def send_telegram(cfg: ScheduleConfig, text: str, items: list[MissingItem]) -> None:
    raw_bot = load_json(cfg.bot_report_config_path)
    bot = raw_bot.get("telegram_bot", raw_bot)
    token = bot.get("bot_token") or bot.get("token")
    chat_id = cfg.telegram_chat_id or str(bot["chat_id"])
    keyboard = []
    for item in items[:10]:
        prefix = "需求" if item.record.table_kind == "demand" else "问题"
        title = item.record.title[:24] + ("..." if len(item.record.title) > 24 else "")
        keyboard.append([{"text": f"{prefix}: {title}", "url": item.url}])
    payload = {
        "chat_id": chat_id,
        "text": text,
        "reply_markup": {"inline_keyboard": keyboard} if keyboard else None,
        "disable_web_page_preview": True,
    }
    if payload["reply_markup"] is None:
        del payload["reply_markup"]
    if cfg.dry_run:
        print(json.dumps(payload, ensure_ascii=False, indent=2))
        return
    data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    request = urllib.request.Request(
        f"https://api.telegram.org/bot{token}/sendMessage",
        data=data,
        headers={"Content-Type": "application/json; charset=utf-8"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=20) as response:
        response.read()


def reminder_text(items: list[MissingItem], today_demands: int, today_issues: int) -> str:
    now = datetime.now(BANGKOK_TZ).strftime("%m/%d")
    lines = [f"【排期确认提醒 {now}】", ""]
    lines.append(f"今日新增：需求 {today_demands} 个，线上问题/Bug {today_issues} 个。")
    lines.append("")
    if not items:
        lines.append("当前没有发现缺负责人或缺预计时间的待排期事项。")
        return "\n".join(lines)
    lines.append("以下事项需要补负责人或预计完成时间，点击按钮可直接打开表单填写：")
    for index, item in enumerate(items, 1):
        label = "需求" if item.record.table_kind == "demand" else "问题"
        lines.append(f"{index}. [{label}] {item.record.title}")
        lines.append(f"   缺：{'、'.join(item.missing)}")
    lines.append("")
    lines.append("可转发到管理群：")
    lines.append("@柯南 @Aiden @afeng 今天还有部分需求/问题缺负责人或预计完成时间，麻烦点击对应按钮补充排期信息，方便同步整体进度和风险。")
    return "\n".join(lines)


def update_record(cfg: ScheduleConfig, table_id: str, record_id: str, patch: dict[str, Any]) -> None:
    path = write_temp_json(patch)
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
            record_id,
            "--json",
            f"@{path}",
        ],
    )


def process_feedback(cfg: ScheduleConfig) -> dict[str, int]:
    fields, record_ids, rows = list_records(cfg, cfg.feedback_table_id)
    updated = 0
    failed = 0
    for feedback_id, row in zip(record_ids, rows):
        values = dict(zip(fields, row))
        state = first_select(values.get("回写状态"))
        if state and state != "待回写":
            continue
        table_id = str(values.get("原表ID") or "")
        record_id = str(values.get("原记录ID") or "")
        item_type = first_select(values.get("事项类型"))
        if not table_id or not record_id:
            update_record(cfg, cfg.feedback_table_id, feedback_id, {"回写状态": "回写失败", "回写说明": "缺原表ID或原记录ID"})
            failed += 1
            continue
        patch: dict[str, Any] = {}
        if item_type == "需求":
            for field in ["前端开发", "前端计划完成时间", "后端开发", "后端计划完成时间"]:
                if not is_blank(values.get(field)):
                    patch[field] = values[field]
        else:
            for field in ["解决人", "预计解决时间"]:
                if not is_blank(values.get(field)):
                    patch[field] = values[field]
        if not patch:
            update_record(cfg, cfg.feedback_table_id, feedback_id, {"回写状态": "回写失败", "回写说明": "未填写可回写字段"})
            failed += 1
            continue
        try:
            update_record(cfg, table_id, record_id, patch)
            update_record(cfg, cfg.feedback_table_id, feedback_id, {"回写状态": "已回写", "回写说明": "已同步到原记录"})
            updated += 1
        except subprocess.CalledProcessError as exc:
            update_record(cfg, cfg.feedback_table_id, feedback_id, {"回写状态": "回写失败", "回写说明": exc.stderr[:300]})
            failed += 1
    return {"updated": updated, "failed": failed}


def run_reminder(config_path: Path) -> None:
    cfg = load_config(config_path)
    items, today_demands, today_issues = collect_missing_items(cfg)
    demand_records = records_from_table(cfg, "demand")
    issue_records = records_from_table(cfg, "issue")
    text = daily_summary_text(demand_records, issue_records, items)
    send_telegram(cfg, text, items)
    print(json.dumps({"missing": len(items), "today_demands": today_demands, "today_issues": today_issues}, ensure_ascii=False))


def run_writeback(config_path: Path) -> None:
    cfg = load_config(config_path)
    result = process_feedback(cfg)
    print(json.dumps(result, ensure_ascii=False))


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    parser.add_argument("command", choices=["remind", "writeback"])
    args = parser.parse_args()
    if args.command == "remind":
        run_reminder(args.config)
    else:
        run_writeback(args.config)


if __name__ == "__main__":
    main()
