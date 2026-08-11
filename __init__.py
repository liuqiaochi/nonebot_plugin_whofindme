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
import re
import time
from typing import List, Optional, Tuple

from nonebot import get_driver, on_message
from nonebot.adapters.onebot.v11 import Bot, Event as OBEvent
from nonebot.adapters.onebot.v11 import GroupMessageEvent, Message, MessageSegment
from nonebot.exception import FinishedException, PausedException, RejectedException

from .config import (
    BOT_QQ,
    DATA_DIR,
    KEEP_DAYS,
    MAX_RESULTS,
    QUERY_HOURS,
    USE_FORWARD,
)
from .db import Database

db = Database(DATA_DIR)
driver = get_driver()

# 触发查询的指令（别名）
COMMANDS = {"谁@我", "谁艾特我", "谁找我"}

# 从 raw_message 兜底提取 @ 的 QQ 号。
# 背景：NapCat/NTQQ 在「引用 + @」场景下，常把 at 留在原始 CQ 串里
#（形如 [CQ:at,qq=123456]），却不把它解析成 event.message 的 at 段；
# 此时仅从 event.message["at"] 取会漏掉，需用正则从 raw_message 兜底。
_CQ_AT_RE = re.compile(r"\[CQ:at,qq=(\d+)\]")


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
def _extract_reply(reply_obj) -> Optional[Tuple[int, str, str]]:
    """从 event.reply 提取 (sender_id, sender_name, content_json)。

    被引用消息本体的段结构序列化为 content_json：
      [{"t":"text","v":"..."}, {"t":"image"}, {"t":"video"}, {"t":"other","v":"record"}]
    图片/视频/其他仅记类型（不存 URL，避免查询时触发下载），
    文本记原始内容。无法获取引用内容时返回 None。
    """
    if reply_obj is None:
        return None
    sender = getattr(reply_obj, "sender", None)
    sender_id = getattr(sender, "user_id", None) if sender else None
    sender_name = (
        getattr(sender, "card", None) or getattr(sender, "nickname", None)
        if sender
        else None
    )
    reply_msg = getattr(reply_obj, "message", None)
    if reply_msg is None:
        return None
    seg_info: List[dict] = []
    try:
        segs = list(reply_msg)
    except Exception:  # noqa: BLE001
        segs = []
    for seg in segs:
        stype = getattr(seg, "type", None)
        if stype == "text":
            seg_info.append({"t": "text", "v": seg.data.get("text", "")})
        elif stype == "image":
            seg_info.append({"t": "image"})
        elif stype == "video":
            seg_info.append({"t": "video"})
        else:
            seg_info.append({"t": "other", "v": stype or "unknown"})
    return (sender_id, sender_name, json.dumps(seg_info, ensure_ascii=False))


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

    # 被引用的那条消息（引用/回复事件）
    reply_obj = getattr(event, "reply", None)
    # 兼容部分 adapter 把 reply 段留在 event.message 而非 event.reply 属性的情况
    reply_seg_list = list(event.message["reply"])
    has_reply = reply_obj is not None or bool(reply_seg_list)

    def _qq_of(segs) -> List[int]:
        out: List[int] = []
        for seg in segs:
            qq = seg.data.get("qq")
            if not qq:
                continue
            try:
                out.append(int(qq))
            except (TypeError, ValueError):
                continue
        return out

    # @ 来源1：当前消息里直接带的 @ 段（标准情况）
    msg_at_qq = _qq_of(event.message["at"])

    # @ 来源2：从 raw_message 兜底提取。
    # NapCat/NTQQ 在「引用 + @」场景下，常把 at 留在原始 CQ 串里
    #（[CQ:at,qq=数字]），却不解析成 event.message 的 at 段；
    # 正则兜底确保一定能拿到被 @ 的 QQ（@全体 为 qq=all/0，正则 \d+ 不匹配，自动忽略）。
    raw = getattr(event, "raw_message", "") or ""
    raw_at_qq = [int(m) for m in _CQ_AT_RE.findall(raw)]

    # @ 来源3：被引用消息里包含的 @ 段（用户要求：引用事件若含 @ 也记录）
    reply_at_qq: List[int] = []
    if reply_obj is not None:
        reply_msg = getattr(reply_obj, "message", None)
        if reply_msg is not None:
            try:
                reply_at_qq = _qq_of(reply_msg["at"])
            except Exception:  # noqa: BLE001
                reply_at_qq = []

    # 调试：把真实结构摊开，便于定位 NapCat 把 at/reply 放在哪
    seg_types = [s.type for s in event.message]
    print(
        f"[whofindme][record] group={event.group_id} sender={event.user_id} "
        f"has_reply={has_reply} msg_at={len(msg_at_qq)} raw_at={len(raw_at_qq)} "
        f"reply_at={len(reply_at_qq)}"
    )
    print(f"[whofindme][record] message段类型列表={seg_types}")
    print(f"[whofindme][record] raw_message(前120)={raw[:120]!r}")

    # 合并三个来源的 @ 对象，忽略 @全体 与 @机器人
    targets: set[int] = set()
    for qq in msg_at_qq + raw_at_qq + reply_at_qq:
        if qq != bot_qq:
            targets.add(qq)
    src_parts = []
    if msg_at_qq:
        src_parts.append(f"msg_seg={msg_at_qq}")
    if raw_at_qq:
        src_parts.append(f"raw={raw_at_qq}")
    if reply_at_qq:
        src_parts.append(f"reply={reply_at_qq}")
    print(
        f"[whofindme][record] 合并 @对象 targets={targets} "
        f"（来源：{', '.join(src_parts) or '无'}）"
    )
    if not targets:
        print("[whofindme][record] 无有效 @对象，跳过")
        return

    # 仅记录发送人当前携带的内容（文本 + 图片）
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

    # 单独保存被引用消息本体的段结构（图片/视频/文本/其他），供查询时呈现。
    # 这是"被引用消息"的内容，与上面"发送人当前消息"是两条独立数据，互不混入。
    reply_sender_id = reply_sender_name = reply_content = None
    if reply_obj is not None:
        _r = _extract_reply(reply_obj)
        if _r is not None:
            reply_sender_id, reply_sender_name, reply_content = _r

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
            reply_sender_id=reply_sender_id,
            reply_sender_name=reply_sender_name,
            reply_content=reply_content,
        )
    print(
        f"[whofindme][record] 已记录 {len(targets)} 条 "
        f"(text_len={len(text)}, img_count={len(images)}, "
        f"引用内容={'已保存' if reply_content else '无'})"
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
            # 第一次：原图合并转发
            try:
                nodes = _build_nodes(bot_qq, summary, rows, simplify=False)
                await _send_forward(bot, event.group_id, nodes)
                return
            except Exception as exc:  # 原图转发失败（多半是图片下载超时）
                print(f"[whofindme] 合并转发(原图)失败，尝试以[图片]文本简化转发: {exc}")
                # 第二次：把图片降级为纯文本 "[图片]" 再试一次合并转发
                try:
                    nodes2 = _build_nodes(bot_qq, summary, rows, simplify=True)
                    await _send_forward(bot, event.group_id, nodes2)
                    return
                except Exception as exc2:
                    print(f"[whofindme] 合并转发(简化)失败，回退纯文本消息: {exc2}")

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


def _render_reply(reply_sender_name: Optional[str], reply_content: Optional[str]) -> str:
    """渲染被引用消息：图片->[图片] 视频->[视频] 文本->内容 其他->[其他]。

    被引用消息本体已序列化为段结构（见 _extract_reply），这里按规则转成文本。
    引用内容本身不存 URL，渲染也不触发任何下载，故与 simplify 无关。
    """
    if not reply_content:
        return ""
    try:
        seg_info = json.loads(reply_content)
    except Exception:  # noqa: BLE001
        return ""
    if not isinstance(seg_info, list):
        return ""
    parts: List[str] = []
    for s in seg_info:
        t = s.get("t")
        if t == "text":
            parts.append(s.get("v", ""))
        elif t == "image":
            parts.append("[图片]")
        elif t == "video":
            parts.append("[视频]")
        else:
            parts.append("[其他]")
    body = "".join(parts)
    who = reply_sender_name or "某人"
    return f"└ 引用了 {who} 的消息：{body}\n"


def _build_nodes(
    bot_qq: int,
    summary: str,
    rows: List[Tuple],
    simplify: bool = False,
) -> List[MessageSegment]:
    """构造合并转发节点列表。

    simplify=True 时：发送人当前消息中的图片以纯文本 "[图片]" 呈现（不触发图片
    下载，规避 NapCat/NTQQ 合并转发下载图片超时），文本照常。用于合并转发原图
    失败后的二次尝试。被引用消息的渲染（图片->[图片] 等）始终是文本化，不下载。
    """
    nodes: List[MessageSegment] = [
        MessageSegment.node_custom(user_id=bot_qq, nickname="查询汇总", content=summary)
    ]
    for (
        sender_name,
        sender_id,
        text,
        images_json,
        created_at,
        reply_sender_id,
        reply_sender_name,
        reply_content,
    ) in rows:
        t = time.strftime("%m-%d %H:%M", time.localtime(created_at))
        content = MessageSegment.text(f"[{t}] {sender_name}({sender_id}) @了你\n")
        if text:
            content += MessageSegment.text(text + "\n")
        for url in _safe_images(images_json):
            if simplify:
                # 纯文本占位，不下载图片，避免转发时图片下载超时
                content += MessageSegment.text("[图片]\n")
            elif url.startswith(("http://", "https://")):
                content += MessageSegment.image(url)
        reply_line = _render_reply(reply_sender_name, reply_content)
        if reply_line:
            content += MessageSegment.text(reply_line)
        nodes.append(
            MessageSegment.node_custom(
                user_id=sender_id, nickname=sender_name, content=content
            )
        )
    return nodes


def _build_plain(
    summary: str, rows: List[Tuple]
) -> Message:
    """构造纯文本回退消息：单条、图片以链接形式展示，避免触发图片下载超时。"""
    msg = MessageSegment.text(summary + "\n\n")
    for idx, (
        sender_name,
        sender_id,
        text,
        images_json,
        created_at,
        reply_sender_id,
        reply_sender_name,
        reply_content,
    ) in enumerate(rows, 1):
        t = time.strftime("%m-%d %H:%M", time.localtime(created_at))
        msg += MessageSegment.text(f"{idx}. [{t}] {sender_name}({sender_id}) @了你\n")
        if text:
            msg += MessageSegment.text(f"    内容：{text}\n")
        for url in _safe_images(images_json):
            msg += MessageSegment.text(f"    [图片] {url}\n")
        reply_line = _render_reply(reply_sender_name, reply_content)
        if reply_line:
            msg += MessageSegment.text("    " + reply_line)
        msg += MessageSegment.text("\n")
    return msg
