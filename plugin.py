"""
撤回插件

提供智能撤回功能的群聊管理插件。

功能特性：
- 智能LLM判定：根据消息内容（文本/图片）判断是否需要撤回
- 灵活的撤回机制：支持立即或延时撤回
- 模板化消息：支持自定义撤回提示消息
- 参数验证：完整的 message_id 验证和错误处理
- 配置文件支持：所有设置可通过 config.toml 调整
- 权限管理：支持群组权限控制
- 事后校验：可选验证消息是否成功撤回

包含组件：
- 智能撤回Action - 基于LLM判断是否需要撤回（支持群组权限控制）
- 撤回命令Command - 手动执行撤回操作（支持用户权限控制）
"""

from typing import List, Tuple, Type, Optional, Any, Dict
import random
import asyncio
import logging
import json
from datetime import datetime, timedelta

# 导入新插件系统
from src.plugin_system.apis.plugin_register_api import register_plugin
from src.plugin_system.base.base_plugin import BasePlugin
from src.plugin_system.base.base_action import BaseAction
from src.plugin_system.base.base_command import BaseCommand
from src.plugin_system.base.component_types import ComponentInfo, ActionActivationType, ChatMode
from src.plugin_system.base.config_types import ConfigField
from src.common.logger import get_logger
from src.plugin_system import message_api

logger = get_logger("recall_plugin")

# ===== 工具函数 =====

def _is_numeric_like(s: Any) -> bool:
    """判断是否为纯数字（用于验证 QQ 平台的 message_id）"""
    try:
        return str(s).isdigit()
    except Exception:
        return False

def _safe_getattr(obj: Any, name: str, default=None):
    """安全获取属性"""
    try:
        return getattr(obj, name, default)
    except Exception:
        return default

def _dump_context(obj: Any, max_depth: int = 3) -> str:
    """将对象转为结构化摘要，用于调试"""
    def _to_tree(obj: Any, depth: int = 0):
        if depth >= max_depth or obj is None:
            return str(obj)[:50]
        if isinstance(obj, dict):
            return {k: _to_tree(v, depth + 1) for k, v in list(obj.items())[:50]}
        if isinstance(obj, (list, tuple)):
            return [_to_tree(v, depth + 1) for v in list(obj)[:50]]
        try:
            return {"__class__": obj.__class__.__name__, "__keys__": list(vars(obj).keys())[:50]}
        except Exception:
            return str(obj)[:50]
    try:
        return json.dumps(_to_tree(obj), ensure_ascii=False)
    except Exception:
        return "<unserializable>"

def _query_message_id_from_api(chat_id: str, platform: str = "qq", time_window_hours: float = 0.0167) -> Optional[str]:
    """通过 message_api 查询最近的消息 ID（默认1分钟窗口）"""
    try:
        msgs = message_api.get_recent_messages(
            chat_id=str(chat_id),
            hours=time_window_hours,  # 1分钟
            limit=1,
            limit_mode="latest",
            filter_mai=True  # 过滤机器人消息，确保获取用户发送的消息
        ) or []
        if msgs and isinstance(msgs[0], dict):
            for k in ["message_id", "platform_message_id", "id", "napcat_message_id", "msg_id", "msgId"]:
                v = msgs[0].get(k)
                if v and _is_numeric_like(v):
                    logger.debug(f"{self.log_prefix} 从 message_api.get_recent_messages 查询到 message_id：{v}, 消息内容：{msgs[0].get('content', '')}")
                    return str(v)
            logger.warning(f"{self.log_prefix} message_api 返回的消息无有效 message_id：{msgs[0]}")
        else:
            logger.warning(f"{self.log_prefix} message_api 未找到消息，chat_id={chat_id}, platform={platform}")
        return None
    except Exception as e:
        logger.error(f"{self.log_prefix} message_api 查询失败：{e}")
        return None

# ===== Action组件 =====

class RecallAction(BaseAction):
    """智能撤回Action - 基于LLM智能判断是否需要撤回消息"""

    activation_type = ActionActivationType.LLM_JUDGE
    parallel_action = True
    action_name = "recall"
    action_description = "撤回严重违规的消息（文本或图片）"
    action_parameters = {
        "target_message_id": "目标消息的ID（必填，QQ平台需为纯数字）",
        "reason": "撤回原因（可选，默认为‘违反群规’）"
    }
    action_require = [
        "当消息包含违反了公序良俗的内容（色情、暴力、政治敏感等）",
        "当消息包含明显违规图片（如裸露、涉未成年人、暴恐、违法内容等）",
        "恶意攻击他人或群组管理，例如辱骂他人，并非单纯的粗口",
    ]
    associated_types = ["text", "image", "command"]
    mode_enable = ChatMode.ALL
    llm_judge_prompt = (
        "你是群聊安全助手。仅在以下严重情形下回答“是”，否则回答“否”（只输出一个字）：\n"
        "A. 当消息包含违反了公序良俗的内容（色情、暴力、政治敏感等）；\n"
        "B. 消息包含明显违规图片（裸露、涉未成年人、暴恐、违法内容等）；\n"
        "C. 恶意攻击他人或群组管理，例如辱骂他人，并非单纯的粗口；\n"
        "若不满足A/B/C任一情形，或证据不足/语义模糊，请回答“否”。\n"
    )

    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        self._recent_recalls: Dict[str, datetime] = {}  # 缓存最近撤回的 message_id 和时间

    def _check_group_permission(self) -> Tuple[bool, Optional[str]]:
        """检查当前群是否有撤回动作权限"""
        if not self.is_group:
            return False, "撤回动作只能在群聊中使用"

        allowed_groups = self.get_config("permissions.allowed_groups", [])
        if not allowed_groups:
            logger.info(f"{self.log_prefix} 群组权限未配置，允许所有群使用撤回动作")
            return True, None

        current_group_key = f"{self.platform}:{self.group_id}"
        if current_group_key in allowed_groups:
            logger.info(f"{self.log_prefix} 群组 {current_group_key} 有撤回动作权限")
            return True, None

        logger.warning(f"{self.log_prefix} 群组 {current_group_key} 没有撤回动作权限")
        return False, "当前群组没有使用撤回动作的权限"

    def _extract_target_id_from_context(self) -> Optional[str]:
        """
        从 action_data / action_message / message.chat_stream / message 等上下文中尽力抽取目标 message_id。
        返回：message_id 字符串或 None。
        """
        KEY_CANDIDATES = [
            "target_message_id", "message_id", "platform_message_id", "id", "napcat_message_id",
            "reply", "reply_id", "reply_to", "reply_message_id", "replied_message_id",
            "quote", "quote_id", "quoted", "quoted_id", "quoted_message_id",
            "source", "source_id", "source_message_id",
            "message_ref_id", "reference_message_id", "refer_message_id",
            "seq", "msgSeq", "msg_id", "msgId", "origin_message_id",
        ]
        # 1) action_message.message_id
        if self.action_message and hasattr(self.action_message, "message_id"):
            mid = self.action_message.message_id
            logger.debug(f"{self.log_prefix} 检查 action_message.message_id：{mid}")
            if _is_numeric_like(mid):
                return str(mid)

        # 2) action_data
        args = getattr(self, "action_data", None) or {}
        for k in KEY_CANDIDATES:
            if isinstance(args, dict) and args.get(k) and _is_numeric_like(args[k]):
                logger.debug(f"{self.log_prefix} 从 action_data.{k} 提取 message_id：{args[k]}")
                return str(args[k])

        # 3) action_message
        am = getattr(self, "action_message", None)
        if isinstance(am, dict):
            for k in KEY_CANDIDATES:
                if am.get(k) and _is_numeric_like(am[k]):
                    logger.debug(f"{self.log_prefix} 从 action_message.{k} 提取 message_id：{am[k]}")
                    return str(am[k])
        else:
            for k in KEY_CANDIDATES:
                v = _safe_getattr(am, k, None)
                if v and _is_numeric_like(v):
                    logger.debug(f"{self.log_prefix} 从 action_message.{k} 提取 message_id：{v}")
                    return str(v)

        # 4) 深度扫描
        hit = _deep_find_key(am, KEY_CANDIDATES, "action_message")
        if not hit:
            msg = _safe_getattr(self, "message", None)
            cs = _safe_getattr(msg, "chat_stream", None)
            hit = _deep_find_key(cs, KEY_CANDIDATES, "message.chat_stream") or \
                  _deep_find_key(msg, KEY_CANDIDATES, "message")
        if hit:
            _, value = hit
            if _is_numeric_like(value):
                logger.debug(f"{self.log_prefix} 从深度扫描提取 message_id：{value}")
                return str(value)

        # 5) 调试日志
        logger.debug(f"{self.log_prefix} 未找到有效 message_id，上下文：action_message={_dump_context(am)}, action_data={_dump_context(args)}, message.chat_stream={_dump_context(cs)}")

        # 6) 备选：通过 message_api 查询
        platform = _safe_getattr(self, "platform", "unknown")
        chat_id = self._pick_chat_id()
        if platform == "qq" and chat_id:
            message_id = _query_message_id_from_api(chat_id, platform)
            if message_id:
                logger.debug(f"{self.log_prefix} 从 message_api 提取 message_id：{message_id}")
                return message_id

        return None

    def _pick_chat_id(self) -> Optional[str]:
        """尝试抽取当前会话 id 用于校验"""
        CHAT_ID_CANDIDATE_KEYS = ["chat_id", "group_id", "conversation_id", "peer_id", "channel_id"]
        for k in CHAT_ID_CANDIDATE_KEYS:
            v = _safe_getattr(self, k, None)
            if v:
                return str(v)
        return None

    async def _post_verify(self, target_mid: str) -> Tuple[bool, str]:
        """事后校验消息是否已撤回"""
        if not self.get_config("verify.enabled", False):
            return True, "skip"

        cid = self._pick_chat_id()
        if not cid:
            logger.info(f"{self.log_prefix} 无法找到 chat_id，跳过校验")
            return True, "skip_no_chat_id"

        try:
            await asyncio.sleep(self.get_config("verify.delay_ms", 500) / 1000.0)
            for n in range(self.get_config("verify.attempts", 2)):
                msgs = message_api.get_recent_messages(
                    chat_id=str(cid),
                    hours=1.0,
                    limit=200,
                    limit_mode="latest",
                    filter_mai=True
                ) or []
                exists = False
                for m in msgs:
                    if isinstance(m, dict):
                        for k in ["message_id", "platform_message_id", "id", "napcat_message_id", "msg_id", "msgId"]:
                            if str(m.get(k, "")) == str(target_mid):
                                exists = True
                                break
                    if exists:
                        break
                if not exists:
                    logger.debug(f"{self.log_prefix} 校验成功：消息 {target_mid} 已不存在")
                    self._recent_recalls[target_mid] = datetime.now()
                    return True, "not_found_after_delete"
                await asyncio.sleep(0.2)
            logger.warning(f"{self.log_prefix} 校验失败：消息 {target_mid} 仍存在")
            return False, "still_exists"
        except Exception as e:
            logger.warning(f"{self.log_prefix} 校验错误：{e}")
            return True, "verify_error"

    async def execute(self) -> Tuple[bool, str]:
        """执行智能撤回动作"""
        logger.info(f"{self.log_prefix} 执行智能撤回动作")

        # 检查最近撤回缓存
        target_mid = self._extract_target_id_from_context()
        if target_mid in self._recent_recalls:
            if (datetime.now() - self._recent_recalls[target_mid]).total_seconds() < 300:  # 5分钟
                logger.info(f"{self.log_prefix} 消息 {target_mid} 已最近撤回，跳过")
                return True, f"消息 {target_mid} 已撤回，跳过"

        # 检查权限
        has_permission, permission_error = self._check_group_permission()
        if not has_permission:
            await self.send_text(f"❌ {permission_error}")
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"[撤回] 无权限：{permission_error}",
                action_done=False
            )
            return False, permission_error

        # 检查 message_id
        if not target_mid:
            error_msg = random.choice(self.get_config("messages.error_messages", ["未找到目标消息，请回复目标消息或提供 message_id"]))
            logger.warning(f"{self.log_prefix} 未找到目标 message_id")
            await self.send_text(f"❌ {error_msg}")
            return False, "未找到可撤回的目标消息"

        platform = _safe_getattr(self, "platform", "unknown")
        if platform == "qq" and not _is_numeric_like(target_mid):
            error_msg = random.choice(self.get_config("messages.error_messages", ["无效的 message_id，请确保格式正确"]))
            logger.error(f"{self.log_prefix} 无效的 QQ message_id：{target_mid}")
            await self.send_text(f"❌ {error_msg}")
            return False, "无效的 QQ message_id（必须为纯数字）"

        logger.debug(f"{self.log_prefix} 找到目标 message_id：{target_mid}")

        # 检查 LLM 判定结果
        llm_result = getattr(self, "llm_judge_result", "是")
        logger.debug(f"{self.log_prefix} LLM 判定结果：{llm_result}")
        if llm_result != "是":
            logger.info(f"{self.log_prefix} LLM 判定为‘否’，跳过撤回")
            return True, "LLM 判定无需撤回"

        display = self.get_config("messages.recall_display", "🗑️ 正在撤回一条消息（严重违规）…")
        delay_ms = int(self.get_config("behavior.recall_delay_ms", 0))
        if delay_ms <= 0:
            ok, used_cmd, raw, note = await _try_delete_with_fallbacks(self, target_mid, display)
            v_ok, v_note = await self._post_verify(target_mid)
            logger.debug(f"{self.log_prefix} 校验：ok={v_ok}, note={v_note}")
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"[撤回]{'✅' if ok else '❌'} message_id={target_mid}",
                action_done=ok
            )
            if not ok and note:
                await self.send_text(f"❌ 撤回失败：{note}")
            return (True, f"已请求撤回 message_id={target_mid}") if ok else (False, f"撤回失败 message_id={target_mid} ({note})")
        else:
            async def _delayed():
                await asyncio.sleep(delay_ms / 1000.0)
                ok, _, _, note = await _try_delete_with_fallbacks(self, target_mid, display)
                await self._post_verify(target_mid)
                if not ok and note:
                    await self.send_text(f"❌ 撤回失败：{note}")
            task = asyncio.create_task(_delayed())
            if hasattr(self, "plugin"):
                self.plugin._track_task(task)
            logger.info(f"{self.log_prefix} 已计划撤回：message_id={target_mid}, delay={delay_ms}ms")
            await self.store_action_info(
                action_build_into_prompt=True,
                action_prompt_display=f"[撤回·已计划] message_id={target_mid} after {delay_ms}ms",
                action_done=True
            )
            return True, f"已计划撤回 ({delay_ms}ms 后) message_id={target_mid}"

# ===== Command组件 =====

class RecallCommand(BaseCommand):
    """撤回命令 - 手动执行撤回操作"""

    command_name = "recall"
    command_description = "撤回指定消息"
    command_pattern = r"^/(?:撤回|recall)(?:\s+(?P<message_id>\S+))?$"
    intercept_message = True

    async def execute(self) -> Tuple[bool, Optional[str], bool]:
        logger.info(f"{self.log_prefix} 执行手动撤回命令")

        allowed_groups = self.get_config("permissions.allowed_groups", [])
        platform = _safe_getattr(self, "platform", "unknown")
        group_id = _safe_getattr(self, "group_id", None)
        if allowed_groups:
            current_key = f"{platform}:{group_id}"
            if current_key not in allowed_groups:
                logger.warning(f"{self.log_prefix} 群组 {current_key} 没有权限")
                await self.send_text(f"❌ 当前群组没有使用撤回命令的权限")
                return False, "当前群组没有使用撤回命令的权限", True

        mid = self.matched_groups.get("message_id")
        if not mid:
            mid = self._extract_target_id_from_context()
        if not mid:
            error_msg = random.choice(self.get_config("messages.error_messages", ["请回复目标消息或提供 /recall <message_id>"]))
            logger.warning(f"{self.log_prefix} 未找到目标 message_id")
            await self.send_text(f"❌ {error_msg}")
            return False, "请回复目标消息或提供 /recall <message_id>", True

        if platform == "qq" and not _is_numeric_like(mid):
            error_msg = random.choice(self.get_config("messages.error_messages", ["无效的 message_id，请确保格式正确"]))
            logger.error(f"{self.log_prefix} 无效的 QQ message_id：{mid}")
            await self.send_text(f"❌ {error_msg}")
            return False, "无效的 QQ message_id（必须为纯数字）", True

        logger.debug(f"{self.log_prefix} 找到目标 message_id：{mid}")

        display = self.get_config("messages.recall_display", "🗑️ 正在撤回（严重违规）…")
        delay_ms = int(self.get_config("behavior.recall_delay_ms", 0))
        if delay_ms <= 0:
            ok, _, _, note = await _try_delete_with_fallbacks(self, mid, display)
            v_ok, v_note = await RecallAction._post_verify(self, mid)
            logger.debug(f"{self.log_prefix} 校验：ok={v_ok}, note={v_note}")
            if not ok and note:
                await self.send_text(f"❌ 撤回失败：{note}")
            return (True, f"已请求撤回 message_id={mid}", True) if ok else (False, f"撤回失败 message_id={mid} ({note})", True)
        else:
            async def _delayed():
                await asyncio.sleep(delay_ms / 1000.0)
                ok, _, _, note = await _try_delete_with_fallbacks(self, mid, display)
                await RecallAction._post_verify(self, mid)
                if not ok and note:
                    await self.send_text(f"❌ 撤回失败：{note}")
            task = asyncio.create_task(_delayed())
            if hasattr(self, "plugin"):
                self.plugin._track_task(task)
            logger.info(f"{self.log_prefix} 已计划撤回：message_id={mid}, delay={delay_ms}ms")
            return True, f"已计划撤回 ({delay_ms}ms 后) message_id={mid}", True

# ===== Plugin 主类 =====

@register_plugin
class RecallPlugin(BasePlugin):
    """撤回插件"""

    plugin_name = "recall_manager_plugin"
    enable_plugin = True
    config_file_name = "config.toml"

    dependencies: List[str] = []
    python_dependencies: List[str] = []

    def __init__(self, *args, **kwargs) -> None:
        super().__init__(*args, **kwargs)
        self._recall_tasks: List[asyncio.Task] = []

    def _track_task(self, task: asyncio.Task):
        def _remove(_):
            try:
                self._recall_tasks.remove(task)
            except ValueError:
                pass
        task.add_done_callback(_remove)
        self._recall_tasks.append(task)

    async def terminate(self):
        for t in list(self._recall_tasks):
            t.cancel()
        await asyncio.gather(*self._recall_tasks, return_exceptions=True)
        self._recall_tasks.clear()

    config_section_descriptions = {
        "plugin": "插件基本信息配置",
        "components": "组件启用控制",
        "permissions": "权限管理配置（群组白名单）",
        "messages": "展示与提示语配置",
        "verify": "撤回后事后校验相关配置",
        "behavior": "行为与时序控制",
        "logging": "日志记录相关配置",
    }

    config_schema: Dict[str, Dict[str, ConfigField]] = {
        "plugin": {
            "enabled": ConfigField(type=bool, default=True, description="是否启用插件"),
            "version": ConfigField(type=str, default="1.0.0", description="插件版本"),
        },
        "components": {
            "enable_smart_recall": ConfigField(type=bool, default=True, description="启用智能撤回 Action"),
            "enable_recall_command": ConfigField(type=bool, default=True, description="启用手动撤回命令"),
        },
        "permissions": {
            "allowed_groups": ConfigField(
                type=list, default=["qq:123456"],
                description="允许执行撤回的群组白名单（为空=不限制）。格式：['qq:123456']"
            ),
        },
        "messages": {
            "recall_display": ConfigField(type=str, default="🗑️ 正在撤回一条消息（严重违规）…", description="平台可见的撤回提示文案"),
            "error_messages": ConfigField(
                type=list,
                default=[
                    "未找到目标消息，请回复目标消息或提供 message_id",
                    "无效的 message_id，请确保格式正确",
                    "撤回失败，请稍后重试",
                ],
                description="撤回失败时的随机错误消息",
            ),
        },
        "verify": {
            "enabled": ConfigField(type=bool, default=True, description="是否在撤回后做一次‘消息仍存在’的简单校验"),
            "delay_ms": ConfigField(type=int, default=500, description="校验前等待的毫秒数"),
            "attempts": ConfigField(type=int, default=2, description="最大校验次数（每次间隔约0.2秒）"),
        },
        "behavior": {
            "recall_delay_ms": ConfigField(type=int, default=0, description="撤回延迟毫秒；0=立即撤回"),
        },
        "logging": {
            "level": ConfigField(type=str, default="DEBUG", description="日志级别: DEBUG/INFO/WARNING/ERROR"),
            "prefix": ConfigField(type=str, default="[RecallPlugin]", description="日志前缀"),
            "include_user_info": ConfigField(type=bool, default=True, description="日志中是否包含用户信息"),
            "include_duration_info": ConfigField(type=bool, default=True, description="日志中是否包含‘时长信息’（本插件默认为 N/A）"),
        },
    }

    def get_plugin_components(self) -> List[Tuple[ComponentInfo, Type]]:
        comps = []
        if self.get_config("components.enable_smart_recall", True):
            comps.append((RecallAction.get_action_info(), RecallAction))
        if self.get_config("components.enable_recall_command", True):
            comps.append((RecallCommand.get_command_info(), RecallCommand))
        return comps

# =================
# 适配器调用封装
# =================

async def _try_delete_with_fallbacks(self, mid: str, display: str) -> Tuple[bool, str, Any, str]:
    """
    依次尝试多个命令名，返回 (ok, used_cmd, raw_result, note)
    """
    note = ""
    DELETE_COMMAND_CANDIDATES = ["DELETE_MSG", "delete_msg", "RECALL_MSG", "recall_msg"]
    for cmd in DELETE_COMMAND_CANDIDATES:
        try:
            res = await self.send_command(
                cmd,
                {"message_id": str(mid)},
                display_message=display,
                storage_message=False,
            )
            ok = False
            if isinstance(res, bool):
                ok = res
            elif isinstance(res, dict):
                if str(res.get("status", "")).lower() in ("ok", "success") or res.get("retcode") == 0 or res.get("code") == 0:
                    ok = True
            if ok:
                return True, cmd, res, note
            if isinstance(res, dict):
                msg = str(res.get("msg") or res.get("message") or "")
                if any(k in msg.lower() for k in ["permission", "admin"]):
                    note = "可能权限不足（Bot非管理员或无法撤回他人消息）"
                elif any(k in msg.lower() for k in ["time", "expired"]):
                    note = "可能超出撤回时间窗口"
        except Exception as e:
            logger.error(f"{self.log_prefix} 撤回异常：cmd={cmd}, error={e}")
            continue
    return False, DELETE_COMMAND_CANDIDATES[-1], None, note or "撤回失败"

def _deep_find_key(obj: Any, keys: List[str], path: str = "", max_depth: int = 4) -> Optional[Tuple[str, Any]]:
    if obj is None or max_depth < 0:
        return None
    if isinstance(obj, dict):
        for k in keys:
            if k in obj and obj[k] is not None:
                return (f"{path}.{k}" if path else k, obj[k])
        for k, v in list(obj.items())[:50]:
            p = f"{path}.{k}" if path else str(k)
            hit = _deep_find_key(v, keys, p, max_depth - 1)
            if hit:
                return hit
        return None
    try:
        d = vars(obj)
        for k in keys:
            if k in d and d[k] is not None:
                return (f"{path}.{k}" if path else k, d[k])
        for k, v in list(d.items())[:50]:
            p = f"{path}.{k}" if path else str(k)
            hit = _deep_find_key(v, keys, p, max_depth - 1)
            if hit:
                return hit
    except Exception:
        pass
    if isinstance(obj, (list, tuple)):
        for idx, v in enumerate(list(obj)[:50]):
            p = f"{path}[{idx}]"
            hit = _deep_find_key(v, keys, p, max_depth - 1)
            if hit:
                return hit
    return None