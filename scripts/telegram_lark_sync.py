#!/usr/bin/env python3
"""Sync Telegram group messages into a Lark Sheet."""

from __future__ import annotations

import argparse
import asyncio
import csv
import getpass
import json
import subprocess
import sys
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from io import StringIO
from pathlib import Path
from typing import Any, Iterable, Iterator


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_CONFIG = Path("telegram_lark_sync.config.json")
DEFAULT_SHEET_TOKEN = "replace-with-lark-sheet-token"
DEFAULT_SHEET_ID = "replace-with-sheet-id"
DEFAULT_LARK_CLI = "lark-cli"
HEADERS = [
    "同步时间",
    "消息时间",
    "群名称",
    "群ID",
    "发言人",
    "发言人ID",
    "消息ID",
    "消息内容",
    "是否媒体",
    "原始类型",
]


@dataclass
class LarkTarget:
    spreadsheet_token: str
    sheet_id: str
    next_row: int = 2
    cli_path: str = DEFAULT_LARK_CLI


def load_config(path: Path = DEFAULT_CONFIG) -> dict[str, Any]:
    if not path.exists():
        raise FileNotFoundError(f"Config not found: {path}. Run init first.")
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_config(config: dict[str, Any], path: Path = DEFAULT_CONFIG) -> None:
    path.write_text(
        json.dumps(config, ensure_ascii=False, indent=2) + "\n",
        encoding="utf-8",
    )


def create_config(path: Path = DEFAULT_CONFIG) -> None:
    if path.exists():
        raise FileExistsError(f"Config already exists: {path}")

    api_id = input("Telegram api_id: ").strip()
    api_hash = getpass.getpass("Telegram api_hash: ").strip()
    phone = input("Telegram phone number, with country code: ").strip()

    config = {
        "telegram": {
            "api_id": int(api_id),
            "api_hash": api_hash,
            "phone": phone,
            "session": "telegram_lark_sync",
        },
        "lark": {
            "spreadsheet_token": DEFAULT_SHEET_TOKEN,
            "sheet_id": DEFAULT_SHEET_ID,
            "next_row": 2,
            "cli_path": DEFAULT_LARK_CLI,
        },
        "sync": {
            "include_channels": False,
            "include_private": False,
            "target_chats": [],
            "max_message_chars": 30000,
        },
    }
    save_config(config, path)
    print(f"Config written: {path}")


def format_dt(value: Any) -> str:
    if not value:
        return ""
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.strftime("%Y-%m-%d %H:%M:%S")
    return str(value)


def display_name(entity: Any) -> str:
    if entity is None:
        return ""
    title = getattr(entity, "title", None)
    if title:
        return str(title)
    parts = [
        getattr(entity, "first_name", None),
        getattr(entity, "last_name", None),
    ]
    name = " ".join(str(part) for part in parts if part).strip()
    if name:
        return name
    username = getattr(entity, "username", None)
    return str(username) if username else ""


def entity_id(entity: Any) -> str:
    value = getattr(entity, "id", "")
    return str(value) if value is not None else ""


def trim_text(text: str, max_chars: int) -> str:
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 20] + "\n...[truncated]"


def is_target_chat(
    event: Any,
    *,
    include_channels: bool = False,
    include_private: bool = False,
) -> bool:
    if getattr(event, "is_group", False):
        return True
    if include_private and getattr(event, "is_private", False):
        return True
    return include_channels and getattr(event, "is_channel", False)


def chat_matches_targets(chat: Any, target_chats: list[str]) -> bool:
    if not target_chats:
        return True
    normalized_targets = {str(item).casefold() for item in target_chats}
    candidates = {
        display_name(chat).casefold(),
        entity_id(chat).casefold(),
    }
    username = getattr(chat, "username", None)
    if username:
        candidates.add(str(username).casefold())
    return bool(candidates & normalized_targets)


def message_key_from_row(row: list[Any]) -> str | None:
    if len(row) <= 6:
        return None
    chat_id = str(row[3]).strip() if row[3] is not None else ""
    message_id = str(row[6]).strip() if row[6] is not None else ""
    if not chat_id or not message_id:
        return None
    return f"{chat_id}:{message_id}"


def chunked(items: list[Any], size: int) -> Iterator[list[Any]]:
    for index in range(0, len(items), size):
        yield items[index : index + size]


async def message_to_row(
    event: Any,
    *,
    synced_at: datetime | None = None,
    max_message_chars: int = 30000,
) -> list[str]:
    synced_at = synced_at or datetime.now().astimezone()
    chat = await event.get_chat()
    sender = await event.get_sender()
    message = event.message
    content = trim_text(getattr(event, "raw_text", "") or "", max_message_chars)
    media = getattr(message, "media", None)
    raw_type = type(media).__name__ if media else "text"

    return [
        format_dt(synced_at),
        format_dt(getattr(message, "date", "")),
        display_name(chat),
        entity_id(chat),
        display_name(sender),
        entity_id(sender),
        str(getattr(message, "id", "")),
        content,
        "是" if media else "否",
        raw_type,
    ]


def to_csv(rows: Iterable[Iterable[str]]) -> str:
    stream = StringIO()
    writer = csv.writer(stream, lineterminator="\n")
    writer.writerows(rows)
    return stream.getvalue()


def append_row_to_lark(row: list[str], target: LarkTarget) -> None:
    append_rows_to_lark([row], target)


def append_rows_to_lark(rows: list[list[str]], target: LarkTarget) -> None:
    if not rows:
        return
    start_cell = f"A{target.next_row}"
    subprocess.run(
        [
            target.cli_path,
            "sheets",
            "+csv-put",
            "--as",
            "user",
            "--spreadsheet-token",
            target.spreadsheet_token,
            "--sheet-id",
            target.sheet_id,
            "--start-cell",
            start_cell,
            "--csv",
            "-",
            "--format",
            "json",
        ],
        input=to_csv(rows),
        text=True,
        encoding="utf-8",
        check=True,
    )


def cell_value(cell: dict[str, Any]) -> Any:
    return cell.get("value", "")


def existing_sheet_keys(target: LarkTarget) -> set[str]:
    if target.next_row <= 2:
        return set()
    end_row = target.next_row - 1
    result = subprocess.run(
        [
            target.cli_path,
            "sheets",
            "+cells-get",
            "--as",
            "user",
            "--spreadsheet-token",
            target.spreadsheet_token,
            "--sheet-id",
            target.sheet_id,
            "--range",
            f"A2:J{end_row}",
            "--format",
            "json",
        ],
        text=True,
        encoding="utf-8",
        capture_output=True,
        check=True,
    )
    payload = json.loads(result.stdout)
    keys: set[str] = set()
    for range_data in payload.get("data", {}).get("ranges", []):
        for cells in range_data.get("cells", []):
            row = [cell_value(cell) for cell in cells]
            key = message_key_from_row(row)
            if key:
                keys.add(key)
    return keys


def target_from_config(config: dict[str, Any]) -> LarkTarget:
    lark = config["lark"]
    return LarkTarget(
        spreadsheet_token=lark["spreadsheet_token"],
        sheet_id=lark["sheet_id"],
        next_row=int(lark.get("next_row", 2)),
        cli_path=str(lark.get("cli_path", DEFAULT_LARK_CLI)),
    )


async def login(config: dict[str, Any]) -> None:
    from telethon import TelegramClient

    tg = config["telegram"]
    client = TelegramClient(tg["session"], tg["api_id"], tg["api_hash"])
    await client.start(phone=tg["phone"])
    me = await client.get_me()
    print(f"Telegram login ready: {display_name(me)} ({entity_id(me)})")
    await client.disconnect()


async def request_code(config: dict[str, Any]) -> None:
    from telethon import TelegramClient

    tg = config["telegram"]
    client = TelegramClient(tg["session"], tg["api_id"], tg["api_hash"])
    await client.connect()
    sent = await client.send_code_request(tg["phone"])
    tg["phone_code_hash"] = sent.phone_code_hash
    save_config(config)
    print("Telegram code requested.")
    await client.disconnect()


async def login_code(config: dict[str, Any], code: str) -> None:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    tg = config["telegram"]
    client = TelegramClient(tg["session"], tg["api_id"], tg["api_hash"])
    await client.connect()
    try:
        await client.sign_in(
            phone=tg["phone"],
            code=code,
            phone_code_hash=tg.get("phone_code_hash"),
        )
    except SessionPasswordNeededError:
        password = getpass.getpass("Telegram 2FA password: ")
        await client.sign_in(password=password)
    me = await client.get_me()
    print(f"Telegram login ready: {display_name(me)} ({entity_id(me)})")
    await client.disconnect()


async def row_from_message(
    message: Any,
    chat: Any,
    *,
    synced_at: datetime | None = None,
    max_message_chars: int = 30000,
) -> list[str]:
    synced_at = synced_at or datetime.now().astimezone()
    sender = await message.get_sender()
    content = trim_text(getattr(message, "raw_text", "") or "", max_message_chars)
    media = getattr(message, "media", None)
    raw_type = type(media).__name__ if media else "text"

    return [
        format_dt(synced_at),
        format_dt(getattr(message, "date", "")),
        display_name(chat),
        entity_id(chat),
        display_name(sender),
        entity_id(sender),
        str(getattr(message, "id", "")),
        content,
        "是" if media else "否",
        raw_type,
    ]


def dialog_is_target(
    dialog: Any,
    *,
    include_channels: bool = False,
    include_private: bool = False,
) -> bool:
    if getattr(dialog, "is_group", False):
        return True
    if include_private and getattr(dialog, "is_user", False):
        return True
    return include_channels and getattr(dialog, "is_channel", False)


async def backfill(config_path: Path, days: int, batch_size: int) -> None:
    from telethon import TelegramClient

    config = load_config(config_path)
    tg = config["telegram"]
    sync_cfg = config.get("sync", {})
    include_channels = bool(sync_cfg.get("include_channels", False))
    include_private = bool(sync_cfg.get("include_private", False))
    target_chats = [str(item) for item in sync_cfg.get("target_chats", [])]
    max_chars = int(sync_cfg.get("max_message_chars", 30000))
    cutoff = datetime.now(timezone.utc) - timedelta(days=days)
    target = target_from_config(config)
    seen = existing_sheet_keys(target)
    total_written = 0

    client = TelegramClient(tg["session"], tg["api_id"], tg["api_hash"])
    await client.start(phone=tg["phone"])
    async for dialog in client.iter_dialogs():
        if not dialog_is_target(
            dialog,
            include_channels=include_channels,
            include_private=include_private,
        ):
            continue
        rows: list[list[str]] = []
        chat = dialog.entity
        if not chat_matches_targets(chat, target_chats):
            continue
        print(f"Scanning: {display_name(chat)}")
        async for message in client.iter_messages(chat):
            message_date = getattr(message, "date", None)
            if not message_date:
                continue
            if message_date < cutoff:
                break
            row = await row_from_message(
                message,
                chat,
                max_message_chars=max_chars,
            )
            key = message_key_from_row(row)
            if key and key in seen:
                continue
            if key:
                seen.add(key)
            rows.append(row)
        rows.sort(key=lambda row: row[1])
        print(f"  rows to write: {len(rows)}")
        for batch in chunked(rows, batch_size):
            config = load_config(config_path)
            target = target_from_config(config)
            append_rows_to_lark(batch, target)
            config["lark"]["next_row"] = target.next_row + len(batch)
            save_config(config, config_path)
            total_written += len(batch)
            print(f"  wrote rows {target.next_row}-{target.next_row + len(batch) - 1}")

    await client.disconnect()
    print(f"Backfill complete. Total rows written: {total_written}")


async def run_sync(config_path: Path) -> None:
    from telethon import TelegramClient, events

    config = load_config(config_path)
    tg = config["telegram"]
    sync_cfg = config.get("sync", {})
    include_channels = bool(sync_cfg.get("include_channels", False))
    include_private = bool(sync_cfg.get("include_private", False))
    target_chats = [str(item) for item in sync_cfg.get("target_chats", [])]
    max_chars = int(sync_cfg.get("max_message_chars", 30000))

    client = TelegramClient(tg["session"], tg["api_id"], tg["api_hash"])
    await client.start(phone=tg["phone"])

    @client.on(events.NewMessage)
    async def handler(event: Any) -> None:
        if not is_target_chat(
            event,
            include_channels=include_channels,
            include_private=include_private,
        ):
            return
        chat = await event.get_chat()
        if not chat_matches_targets(chat, target_chats):
            return
        config = load_config(config_path)
        target = target_from_config(config)
        row = await message_to_row(event, max_message_chars=max_chars)
        append_row_to_lark(row, target)
        config["lark"]["next_row"] = target.next_row + 1
        save_config(config, config_path)
        print(f"synced row {target.next_row}: {row[2]} #{row[6]}")

    print("Sync is running. Press Ctrl+C to stop.")
    await client.run_until_disconnected()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("login")
    sub.add_parser("request-code")
    login_code_parser = sub.add_parser("login-code")
    login_code_parser.add_argument("--code", required=True)
    backfill_parser = sub.add_parser("backfill")
    backfill_parser.add_argument("--days", type=int, default=7)
    backfill_parser.add_argument("--batch-size", type=int, default=100)
    sub.add_parser("run")
    args = parser.parse_args()

    if args.command == "init":
        create_config(args.config)
    elif args.command == "login":
        asyncio.run(login(load_config(args.config)))
    elif args.command == "request-code":
        asyncio.run(request_code(load_config(args.config)))
    elif args.command == "login-code":
        asyncio.run(login_code(load_config(args.config), args.code))
    elif args.command == "backfill":
        asyncio.run(backfill(args.config, args.days, args.batch_size))
    elif args.command == "run":
        asyncio.run(run_sync(args.config))


if __name__ == "__main__":
    main()
