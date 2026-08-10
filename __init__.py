"""nonebot_plugin_whofindme —— 记录群内 @ 消息，并支持查询“谁 @ 了我”。

功能：
  - 仅在群聊中生效；某用户 @ 另一用户时，记录该消息。
  - 记录内容：群号、发送者 QQ、发送者昵称、被 @ 者 QQ、文本、图片、时间。
  - 忽略机器人自身的 QQ（机器人 QQ 自动检测，无需配置；发送者或被 @ 者等于机器人均不记录）。
  - 查询指令：谁@我 / 谁艾特我 / 谁找我，仅返回「当前群」「最近 24 小时」内 @ 了自己的消息。
  - 超过 7 天的记录自动清理。
"""
import asyncio
import json
import time
from typing import List, Tuple

from nonebot import on_message, on_shutdown, on_startup
from nonebot.adapters.onebot.v11 import Bot, Event as OBEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment

from .config import BOT_QQ, DB_PATH, KEEP_DAYS, MAX_RESULTS, QUERY_HOURS
from .db import Database

db = Database(DB_PATH)

# 触发查询的指令（别名）
COMMANDS = {"谁@我", "谁艾特我", "谁找我"}


# --------------------------------------------------------------------------- #
# 启动 / 关闭
# --------------------------------------------------------------------------- #
@on_startup
async def _startup() -> None:
    await db.init()
    asyncio.create_task(_cleanup_loop())


@on_shutdown
async def _shutdown() -> None:
    await db.close()


async def _cleanup_loop() -> None:
    """每小时清理一次超过保留天数的记录。"""
    while True:
        await asyncio.sleep(3600)
        try:
            before = int(time.time()) - KEEP_DAYS * 86400
            await db.delete_old(before)
        except Exception as exc:  # noqa: BLE001
            print(f"[whofindme] 清理旧记录失败: {exc}")


# --------------------------------------------------------------------------- #
# 记录 @ 消息
# --------------------------------------------------------------------------- #
record_matcher = on_message(priority=5, block=False)


@record_matcher.handle()
async def _record(event: GroupMessageEvent) -> None:
    if event.message_type != "group":
        return
    # 机器人 QQ：未配置时自动取本连接自身的 self_id，无需手动设置
    bot_qq = BOT_QQ if BOT_QQ is not None else event.self_id
    # 忽略机器人自己发的消息
    if event.user_id == bot_qq:
        return

    at_segments = event.message["at"]
    if not at_segments:
        return

    targets: set[int] = set()
    for seg in at_segments:
        qq = seg.data.get("qq")
        if not qq:
            continue
        try:
            qq = int(qq)
        except (TypeError, ValueError):
            continue
        # 忽略 @全体 与 @机器人
        if qq != bot_qq:
            targets.add(qq)
    if not targets:
        return

    text = event.message.extract_plain_text().strip()
    images: List[str] = []
    for seg in event.message["image"]:
        url = seg.data.get("url") or seg.data.get("file")
        if url:
            images.append(url)

    sender_name = event.sender.card or event.sender.nickname or str(event.user_id)
    now = int(time.time())
    for target in targets:
        await db.add(
            group_id=event.group_id,
            sender_id=event.user_id,
            sender_name=sender_name,
            target_id=target,
            text=text,
            images=json.dumps(images, ensure_ascii=False),
            created_at=now,
        )


# --------------------------------------------------------------------------- #
# 查询：谁 @ 我
# --------------------------------------------------------------------------- #
def _is_query(event: OBEvent) -> bool:
    if getattr(event, "message_type", None) != "group":
        return False
    text = event.message.extract_plain_text().strip()
    return text in COMMANDS


who_matcher = on_message(rule=_is_query, priority=10, block=True)


async def _send_forward(bot: Bot, group_id: int, nodes: List[MessageSegment]) -> None:
    """合并转发发送：优先 send_group_forward_msg，兼容 send_forward_msg。"""
    last_exc: Exception | None = None
    for action in ("send_group_forward_msg", "send_forward_msg"):
        try:
            await bot.call_api(action, group_id=group_id, messages=Message(nodes))
            return
        except Exception as exc:  # noqa: BLE001
            last_exc = exc
    if last_exc is not None:
        raise last_exc


@who_matcher.handle()
async def _who(bot: Bot, event: GroupMessageEvent) -> None:
    if event.message_type != "group":
        return
    since = int(time.time()) - QUERY_HOURS * 3600

    total = await db.count(event.group_id, event.user_id, since)
    if total == 0:
        await who_matcher.finish(f"最近 {QUERY_HOURS} 小时内，本群没有人 @ 你~")

    # 取最近的 MAX_RESULTS 条（db.query 已按时间倒序）
    rows: List[Tuple[str, int, str, str, int]] = await db.query(
        event.group_id, event.user_id, since, limit=MAX_RESULTS
    )

    bot_qq = BOT_QQ if BOT_QQ is not None else event.self_id

    # 汇总节点
    summary = f"最近 {QUERY_HOURS} 小时内，本群共有 {total} 条 @ 你的消息"
    if total > len(rows):
        summary += f"（以下仅显示最近的 {len(rows)} 条）"
    nodes: List[MessageSegment] = [
        MessageSegment.node_custom(user_id=bot_qq, nickname="查询汇总", content=summary)
    ]

    for sender_name, sender_id, text, images_json, created_at in rows:
        t = time.strftime("%m-%d %H:%M", time.localtime(created_at))
        content = MessageSegment.text(f"[{t}] {sender_name}({sender_id}) @了你\n")
        if text:
            content += MessageSegment.text(text + "\n")
        for url in json.loads(images_json):
            content += MessageSegment.image(url)
        nodes.append(
            MessageSegment.node_custom(
                user_id=sender_id, nickname=sender_name, content=content
            )
        )

    try:
        # 合并转发：将多条记录合并为一条转发消息，避免刷屏
        await _send_forward(bot, event.group_id, nodes)
    except Exception as exc:  # noqa: BLE001
        # 客户端不支持合并转发时，回退为单条普通消息
        print(f"[whofindme] 合并转发失败，回退普通消息: {exc}")
        plain = MessageSegment.text(summary + "\n\n")
        for idx, (sender_name, sender_id, text, images_json, created_at) in enumerate(
            rows, 1
        ):
            t = time.strftime("%m-%d %H:%M", time.localtime(created_at))
            plain += MessageSegment.text(
                f"{idx}. [{t}] {sender_name}({sender_id}) @了你\n"
            )
            if text:
                plain += MessageSegment.text(f"    内容：{text}\n")
            for url in json.loads(images_json):
                plain += MessageSegment.image(url)
            plain += MessageSegment.text("\n")
        await who_matcher.send(plain)

    await who_matcher.finish()
