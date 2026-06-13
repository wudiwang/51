#!/usr/bin/env python3
"""Keep a second Telegram account lightly active with its own Telethon session."""

from __future__ import annotations

import argparse
import asyncio
import getpass
import json
import sys
from datetime import datetime
from pathlib import Path
from typing import Any


if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    sys.stderr.reconfigure(encoding="utf-8", errors="replace")


DEFAULT_CONFIG = Path("telegram_keepalive_alt.config.json")
SOURCE_CONFIG = Path("telegram_lark_sync.config.json")


def read_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def write_json(path: Path, data: dict[str, Any]) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def init_config(path: Path) -> None:
    if path.exists():
        raise FileExistsError(f"Config already exists: {path}")
    if not SOURCE_CONFIG.exists():
        raise FileNotFoundError(f"Missing {SOURCE_CONFIG}; cannot reuse api_id/api_hash.")

    source = read_json(SOURCE_CONFIG)
    tg = source["telegram"]
    phone = input("Second Telegram phone number, with country code: ").strip()
    session = input("Session name [telegram_keepalive_alt]: ").strip() or "telegram_keepalive_alt"

    config = {
        "telegram": {
            "api_id": tg["api_id"],
            "api_hash": tg["api_hash"],
            "phone": phone,
            "session": session,
        }
    }
    write_json(path, config)
    print(f"Config written: {path}")


async def request_code(path: Path) -> None:
    from telethon import TelegramClient

    config = read_json(path)
    tg = config["telegram"]
    client = TelegramClient(tg["session"], tg["api_id"], tg["api_hash"])
    await client.connect()
    sent = await client.send_code_request(tg["phone"])
    config["telegram"]["phone_code_hash"] = sent.phone_code_hash
    write_json(path, config)
    await client.disconnect()
    print("Code requested. Check Telegram/SMS for the login code.")


async def login_code(path: Path, code: str) -> None:
    from telethon import TelegramClient
    from telethon.errors import SessionPasswordNeededError

    config = read_json(path)
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
    await client.disconnect()
    print(f"Login ready: {getattr(me, 'username', None) or getattr(me, 'id', '')}")


async def keepalive(path: Path) -> None:
    from telethon import TelegramClient, functions

    config = read_json(path)
    tg = config["telegram"]
    client = TelegramClient(tg["session"], tg["api_id"], tg["api_hash"])
    await client.connect()
    if not await client.is_user_authorized():
        await client.disconnect()
        raise RuntimeError("Not authorized. Run request-code and login-code first.")

    await client(functions.account.UpdateStatusRequest(offline=False))
    me = await client.get_me()
    print(
        f"{datetime.now().strftime('%Y-%m-%d %H:%M:%S')} keepalive ok: "
        f"{getattr(me, 'username', None) or getattr(me, 'id', '')}"
    )
    await client.disconnect()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("init")
    sub.add_parser("request-code")
    login = sub.add_parser("login-code")
    login.add_argument("--code", required=True)
    sub.add_parser("keepalive")
    args = parser.parse_args()

    if args.command == "init":
        init_config(args.config)
    elif args.command == "request-code":
        asyncio.run(request_code(args.config))
    elif args.command == "login-code":
        asyncio.run(login_code(args.config, args.code))
    elif args.command == "keepalive":
        asyncio.run(keepalive(args.config))


if __name__ == "__main__":
    main()
