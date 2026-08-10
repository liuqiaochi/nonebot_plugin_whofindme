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

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, Event as OBEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.exception import FinishedException, PausedException, RejectedException

from .config import (
    BOT_QQ,
    DB_PATH,
    KEEP_DAYS,
    MAX_RESULTS,
    QUERY_HOURS,
    USE_FORWARD,
)
from .db import Database

db = Database(DB_PATH)
driver = get_driver()

# 触发查询的指令（别名）
COMMANDS = {"谁@我", "谁艾特我", "谁找我"}


# --------------------------------------------------------------------------- #
# 启动 / 关闭
# --------------------------------------------------------------------------- #
@driver.on_startup
async def _startup() -> None:
    await db.init()
    asyncio.create_task(_cleanup_loop())


@driver.on_shutdown
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
        # NapCat/NTQQ 有时把 url/file 包成 dict，取出其中的字符串地址；
        # 只保留字符串，避免把 dict/非字符串写进 images，导致后续 json.loads 失败。
        if isinstance(url, dict):
            url = url.get("url") or url.get("file")
        if isinstance(url, str) and url:
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
    try:
        since = int(time.time()) - QUERY_HOURS * 3600

        total = await db.count(event.group_id, event.user_id, since)
        if total == 0:
            await who_matcher.finish(f"最近 {QUERY_HOURS} 小时内，本群没有人 @ 你~")

        # 取最近的 MAX_RESULTS 条（db.query 已按时间倒序）
        rows: List[Tuple[str, int, str, str, int]] = await db.query(
            event.group_id, event.user_id, since, limit=MAX_RESULTS
        )

        bot_qq = BOT_QQ if BOT_QQ is not None else event.self_id

        # 汇总文案
        summary = f"最近 {QUERY_HOURS} 小时内，本群共有 {total} 条 @ 你的消息"
        if total > len(rows):
            summary += f"（以下仅显示最近的 {len(rows)} 条）"

        if USE_FORWARD:
            try:
                nodes = _build_nodes(bot_qq, summary, rows)
                await _send_forward(bot, event.group_id, nodes)
                return
            except Exception as exc:  # 合并转发失败，回退纯文本
                print(f"[whofindme] 合并转发失败，回退纯文本消息: {exc}")

        # 回退：单条纯文本消息（图片以链接形式附带，避免图片下载导致超时）
        await who_matcher.finish(_build_plain(summary, rows))
    except (FinishedException, PausedException, RejectedException):
        raise  # finish/pause/reject 是正常的控制流信号，必须放行，否则会重复发送
    except Exception as exc:  # 其它意外才优雅降级，绝不裸崩
        print(f"[whofindme] 查询异常，已降级: {exc}")
        await who_matcher.finish("查询时出现异常，请稍后重试。")


def _safe_images(images_json: str) -> List[str]:
    """防崩解析 images 字段。

    兼容以下情况，确保任何脏数据都不会让查询崩溃：
      - 合法 JSON 列表：["https://..."]
      - 历史遗留的 Python 列表写法（单引号）：['https://...']
      - 直接存了一个 URL 字符串
      - 空值 / 非法内容 -> 返回 []
    """
    if not images_json:
        return []
    try:
        data = json.loads(images_json)
    except (json.JSONDecodeError, TypeError, ValueError):
        try:
            import ast

            data = ast.literal_eval(images_json)
        except Exception:
            return []
    if isinstance(data, list):
        return [str(u) for u in data if u]
    if isinstance(data, str) and data:
        return [data]
    return []


def _build_nodes(
    bot_qq: int, summary: str, rows: List[Tuple[str, int, str, str, int]]
) -> List[MessageSegment]:
    """构造合并转发节点列表。"""
    nodes: List[MessageSegment] = [
        MessageSegment.node_custom(user_id=bot_qq, nickname="查询汇总", content=summary)
    ]
    for sender_name, sender_id, text, images_json, created_at in rows:
        t = time.strftime("%m-%d %H:%M", time.localtime(created_at))
        content = MessageSegment.text(f"[{t}] {sender_name}({sender_id}) @了你\n")
        if text:
            content += MessageSegment.text(text + "\n")
        for url in _safe_images(images_json):
            # 合并转发中只渲染 http(s) 图片，本地路径/异常地址无法在转发里展示
            if url.startswith(("http://", "https://")):
                content += MessageSegment.image(url)
        nodes.append(
            MessageSegment.node_custom(
                user_id=sender_id, nickname=sender_name, content=content
            )
        )
    return nodes


def _build_plain(
    summary: str, rows: List[Tuple[str, int, str, str, int]]
) -> Message:
    """构造纯文本回退消息：单条、图片以链接形式展示，避免触发图片下载超时。"""
    msg = MessageSegment.text(summary + "\n\n")
    for idx, (sender_name, sender_id, text, images_json, created_at) in enumerate(
        rows, 1
    ):
        t = time.strftime("%m-%d %H:%M", time.localtime(created_at))
        msg += MessageSegment.text(f"{idx}. [{t}] {sender_name}({sender_id}) @了你\n")
        if text:
            msg += MessageSegment.text(f"    内容：{text}\n")
        for url in _safe_images(images_json):
            msg += MessageSegment.text(f"    [图片] {url}\n")
        msg += MessageSegment.text("\n")
    return msg
