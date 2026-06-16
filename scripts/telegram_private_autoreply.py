#!/usr/bin/env python3
"""Constrained Telegram private auto-reply helper."""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any


DEFAULT_CONFIG = Path("telegram_private_autoreply.config.json")
DEFAULT_STATE = Path("telegram_private_autoreply.state.json")


@dataclass
class AutoReplyConfig:
    telegram: dict[str, Any]
    target_username: str
    reply_text: str
    fallback_reply_text: str
    notify_saved_messages: bool
    max_replies_per_day: int
    state_path: Path


def load_json(path: Path) -> dict[str, Any]:
    return json.loads(path.read_text(encoding="utf-8-sig"))


def save_json(path: Path, data: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def load_config(path: Path) -> AutoReplyConfig:
    raw = load_json(path)
    return AutoReplyConfig(
        telegram=raw["telegram"],
        target_username=str(raw["target_username"]).lstrip("@"),
        reply_text=str(raw["reply_text"]),
        fallback_reply_text=str(raw.get("fallback_reply_text", raw["reply_text"])),
        notify_saved_messages=bool(raw.get("notify_saved_messages", True)),
        max_replies_per_day=int(raw.get("max_replies_per_day", 1)),
        state_path=Path(raw.get("state_path", DEFAULT_STATE)),
    )


def today_key(now: datetime | None = None) -> str:
    now = now or datetime.now(timezone.utc)
    return now.astimezone(timezone(timedelta(hours=7))).strftime("%Y-%m-%d")


def can_reply(state: dict[str, Any], cfg: AutoReplyConfig) -> bool:
    day = today_key()
    day_state = state.setdefault(day, {})
    return int(day_state.get("reply_count", 0)) < cfg.max_replies_per_day


def mark_replied(state: dict[str, Any], message_id: int, reply_id: int) -> None:
    day = today_key()
    day_state = state.setdefault(day, {})
    day_state["reply_count"] = int(day_state.get("reply_count", 0)) + 1
    day_state["last_incoming_message_id"] = message_id
    day_state["last_reply_message_id"] = reply_id
    day_state["last_replied_at"] = datetime.now(timezone.utc).isoformat()


def should_ignore_text(text: str) -> bool:
    stripped = text.strip()
    if not stripped:
        return True
    lower = stripped.lower()
    risky_terms = [
        "密码",
        "账号",
        "token",
        "secret",
        "api_hash",
        "银行卡",
        "转账",
        "借钱",
        "合同",
        "离职",
        "工资",
    ]
    return any(term in lower for term in risky_terms)


def choose_reply(text: str, cfg: AutoReplyConfig) -> str:
    """Small bounded reply policy for familiar coworker/friend scheduling chat."""
    stripped = text.strip()
    lower = stripped.lower()

    if any(term in stripped for term in ["没空", "忙", "改天", "下次", "最近不行"]):
        return "没事，你先忙。等你这阵过了我们再约，最近确实都被事情推着走。"

    if any(term in stripped for term in ["可以", "行", "好啊", "有空", "约", "吃", "饭"]):
        return "行，那你看今晚或者明晚哪个方便？不用太正式，简单吃点聊聊就好。"

    if any(term in stripped for term in ["什么时候", "哪天", "时间", "几点", "哪里", "去哪"]):
        return "我这边今晚或明晚都可以，地方你定也行；不想折腾的话就找个近点的。"

    if any(term in stripped for term in ["聊啥", "什么事", "咋了", "怎么了"]):
        return "没啥特别正式的，就是最近项目和这边情况有点多，想听听你怎么看，也顺便放松下。"

    if len(stripped) <= 4:
        return "哈哈，那我晚点看下时间，你方便的话我们就简单约一顿。"

    if any(term in lower for term in ["ok", "yes", "sure"]):
        return "OK，那你看今晚或者明晚哪个方便，我们简单吃点聊聊。"

    return cfg.fallback_reply_text


async def run_listener(config_path: Path) -> None:
    from telethon import TelegramClient, events

    cfg = load_config(config_path)
    state = load_json(cfg.state_path) if cfg.state_path.exists() else {}
    client = TelegramClient(
        cfg.telegram["session"],
        cfg.telegram["api_id"],
        cfg.telegram["api_hash"],
    )
    await client.start(phone=cfg.telegram.get("phone"))
    target = await client.get_entity(cfg.target_username)

    @client.on(events.NewMessage(incoming=True, from_users=target))
    async def handler(event: events.NewMessage.Event) -> None:
        nonlocal state
        text = event.raw_text or ""
        if should_ignore_text(text):
            if cfg.notify_saved_messages:
                await client.send_message(
                    "me",
                    f"收到 @{cfg.target_username} 的消息，因涉及敏感/复杂内容未自动回复：\n{text[:1000]}",
                )
            return
        if not can_reply(state, cfg):
            if cfg.notify_saved_messages:
                await client.send_message(
                    "me",
                    f"收到 @{cfg.target_username} 的消息，但今日自动回复额度已用完：\n{text[:1000]}",
                )
            return
        reply_text = choose_reply(text, cfg)
        reply = await event.reply(reply_text)
        mark_replied(state, event.message.id, reply.id)
        save_json(cfg.state_path, state)
        if cfg.notify_saved_messages:
            await client.send_message(
                "me",
                "已自动回复 @{user}\n\n对方消息：\n{incoming}\n\n自动回复：\n{reply}".format(
                    user=cfg.target_username,
                    incoming=text[:1000],
                    reply=reply_text,
                ),
            )

    print(f"Listening for @{cfg.target_username}; max {cfg.max_replies_per_day} replies/day")
    await client.run_until_disconnected()


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=DEFAULT_CONFIG)
    args = parser.parse_args()
    asyncio.run(run_listener(args.config))


if __name__ == "__main__":
    main()
