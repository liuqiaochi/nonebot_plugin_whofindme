"""nonebot_plugin_whofindme 配置读取。

所有配置项均可在 NoneBot 的全局配置（.env / 环境变量 / 项目 config.py）中覆盖，
未配置时使用下方默认值。
"""
from typing import Optional

from nonebot import get_driver

_config = get_driver().config

# 机器人自身 QQ。默认 None = 自动检测（使用 event.self_id），无需任何配置。
# 如需强制指定（例如多 bot 场景），可填具体数字，相关消息将不记录。
_raw = getattr(_config, "plugin_whofindme_bot_qq", None)
BOT_QQ: Optional[int] = int(_raw) if _raw not in (None, "") else None

# SQLite 数据库文件路径（目录会自动创建）
DB_PATH: str = getattr(_config, "plugin_whofindme_data_path", "./data/whofindme.db")

# 记录保留天数，超过则自动删除（默认 7 天）
KEEP_DAYS: int = int(getattr(_config, "plugin_whofindme_keep_days", 7))

# 查询时回溯的小时数（"谁@我" 只查这么久以内的消息，默认 24 小时）
QUERY_HOURS: int = int(getattr(_config, "plugin_whofindme_query_hours", 24))

# 合并转发最多展示最近多少条（默认 50，防止单条回复过长刷屏）
MAX_RESULTS: int = int(getattr(_config, "plugin_whofindme_max_results", 50))

# 是否使用合并转发形式回复（默认 False）。
# 当前主流的 NapCat + NTQQ 组合在发送合并转发时会失败（retcode=1200 / 发送超时），
# 因此默认关闭，直接以单条纯文本消息回复（图片以链接形式附带，最稳）。
# 若你的 OneBot 实现支持合并转发（如 go-cqhttp），可将其设为 true 启用。
USE_FORWARD: bool = str(getattr(_config, "plugin_whofindme_use_forward", "false")).lower() in (
    "1",
    "true",
    "yes",
    "on",
)
