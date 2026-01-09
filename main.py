# --- START OF FILE main.py ---

import asyncio
import copy
import json
import re
import time
import os
import uuid
import random
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter as event_filter, MessageEventResult, ResultContentType
from astrbot.api.provider import LLMResponse

# --- 存储架构配置 ---
HOT_STORAGE_DIR = Path("data/cot_os_logs/sessions")
COLD_ARCHIVE_DIR = Path("data/cot_os_logs/daily_archive")

HOT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
COLD_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# --- HTML 渲染模板 (Classicism HD Version) ---
LOG_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <style>
        /* 古典主义风格 - 高清优化版 */
        body {
            font-family: 'Noto Serif CJK SC', 'Source Han Serif SC', 'Songti SC', 'SimSun', 'Times New Roman', serif;
            background-color: #f4f1ea; /* 羊皮纸色调 */
            color: #2b2b2b; /* 墨色 */
            margin: 0;
            padding: 60px; /* 增加留白 */
            display: inline-block;
            width: 100%;
            box-sizing: border-box;
        }
        
        .container {
            width: 100%;
            max-width: 1000px; /* 拓宽容器以适配高清渲染 */
            margin: 0 auto;
        }

        .card {
            background: #fdfbf7;
            border: 1px solid #dcd6cc;
            /* 纸张立体感阴影 */
            box-shadow: 
                0 2px 5px rgba(0,0,0,0.05),
                0 20px 40px rgba(0,0,0,0.03),
                inset 0 0 80px rgba(255,255,255,0.5);
            padding: 70px;
            position: relative;
        }
        
        /* 装饰性内边框 */
        .card::before {
            content: "";
            position: absolute;
            top: 20px; left: 20px; right: 20px; bottom: 20px;
            border: 2px solid #e8e4db;
            pointer-events: none;
        }

        .header {
            text-align: center;
            margin-bottom: 50px;
            border-bottom: 2px solid #2b2b2b;
            padding-bottom: 25px;
            position: relative;
            z-index: 1;
        }

        .title {
            font-size: 42px; /* 增大标题字号 */
            font-weight: 700;
            letter-spacing: 4px;
            text-transform: uppercase;
            margin-bottom: 15px;
            display: block;
            color: #1a1a1a;
            text-shadow: 0 1px 2px rgba(0,0,0,0.1);
        }

        .badge {
            font-size: 18px;
            font-weight: 400;
            color: #666;
            font-style: italic;
            font-family: 'Georgia', serif;
            background: transparent;
            padding: 0;
            border-radius: 0;
            backdrop-filter: none;
        }

        .content {
            font-size: 28px; /* 正文字号显著提升 */
            line-height: 1.8;
            color: #333;
            white-space: pre-wrap;
            text-align: justify;
            font-weight: 400;
            margin-bottom: 50px;
            z-index: 1;
            position: relative;
        }

        .footer {
            text-align: center;
            font-size: 16px;
            color: #888;
            border-top: 1px solid #e8e4db;
            padding-top: 25px;
            font-family: 'Georgia', serif;
            letter-spacing: 2px;
            text-transform: uppercase;
        }

        strong { color: #8b4513; font-weight: 700; } /* 赭石色强调 */
        em { 
            color: #556b2f; /* 橄榄绿强调 */
            font-style: italic;
            background: transparent;
            padding: 0;
            border: none;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <div class="header">
                <span class="title">{{ title }}</span>
                <span class="badge">&mdash; {{ subtitle }} &mdash;</span>
            </div>
            <div class="content">{{ content }}</div>
            <div class="footer">COGITO ERGO SUM &bull; {{ timestamp }}</div>
        </div>
    </div>
</body>
</html>
"""

def sanitize_filename(session_id: str) -> str:
    return re.sub(r'[:\\/\*?"<>|]', '_', session_id)

@register(
    "Rosaintelligent_retry_with_cot",
    "ReedSein",
    "集成了思维链(CoT)处理的智能重试插件。v3.8.17 绿灯补丁版，修复 SpectreCore 静默指令被误判重试的问题。",
    "3.8.17-SpectreCore-GreenLight",
)
class IntelligentRetryWithCoT(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task())
        self._parse_config(config)
        
        # --- 罗莎配置 ---
        self.cot_start_tag = config.get("cot_start_tag", "<ROSAOS>")
        self.cot_end_tag = config.get("cot_end_tag", "</ROSAOS>")
        self.final_reply_pattern_str = config.get("final_reply_pattern", r"最终的罗莎回复[:：]?\s*")
        self.incantation_tag = str(config.get("incantation_tag", "Incantatio")).strip()
        self.incantation_fallback_reply = config.get(
            "incantation_fallback_reply",
            "咒语调用失败，请稍后再试。",
        )
        self.clean_spectrecore_newlines = bool(config.get("clean_spectrecore_newlines", False))
        
        self.FINAL_REPLY_PATTERN = re.compile(self.final_reply_pattern_str, re.IGNORECASE)
        self.INCANTATION_PATTERN = (
            self._build_incantation_pattern(self.incantation_tag)
            if self.incantation_tag
            else None
        )
        self.INCANTATION_OPEN_PATTERN = (
            self._build_incantation_open_pattern(self.incantation_tag)
            if self.incantation_tag
            else None
        )
        self.INCANTATION_CLOSE_PATTERN = (
            self._build_incantation_close_pattern(self.incantation_tag)
            if self.incantation_tag
            else None
        )
        
        # 构造灵活的标签检测正则，兼容中英文括号
        # 匹配规则：[<＜《(（] ROSAOS [>＞》)）]
        # 提取标签核心词（去掉尖括号部分）
        start_core = self.cot_start_tag.strip("<>＜＞《》()（）")
        end_core = self.cot_end_tag.strip("</>＜＞《》()（）")
        
        # 构造正则：允许前后括号是任意常见的中英文括号
        brackets = r"[<＜《\(\[（]"
        close_brackets = r"[>＞》\)\]）]"
        
        self.COT_TAG_DETECTOR = re.compile(
            f"({brackets}/?{re.escape(start_core)}{close_brackets})|"
            f"({brackets}/?{re.escape(end_core)}{close_brackets})", 
            re.IGNORECASE
        )
        
        escaped_start = re.escape(self.cot_start_tag)
        escaped_end = re.escape(self.cot_end_tag)
        self.THOUGHT_TAG_PATTERN = re.compile(f'{escaped_start}(?P<content>.*?){escaped_end}', re.DOTALL)
        self.DOSSIER_TAG_PATTERN = re.compile(
            r"[<＜]\s*DOSSIER_UPDATE\s*[>＞].*?[<＜]/\s*DOSSIER_UPDATE\s*[>＞]",
            re.IGNORECASE | re.DOTALL,
        )
        self.DOSSIER_OPEN_PATTERN = re.compile(r"[<＜]\s*DOSSIER_UPDATE\b", re.IGNORECASE)
        self.DOSSIER_CLOSE_PATTERN = re.compile(r"[<＜]/\s*DOSSIER_UPDATE\b", re.IGNORECASE)
        
        self.display_cot_text = config.get("display_cot_text", False)
        self.filtered_keywords = config.get("filtered_keywords", ["呵呵，", "（……）"])
        
        # --- 总结配置 ---
        self.summary_provider_id = config.get("summary_provider_id", "")
        self.summary_max_retries = max(1, int(config.get("summary_max_retries", 2)))
        self.history_limit = int(config.get("history_limit", 100))
        self.summary_timeout = int(config.get("summary_timeout", 60))
        self.summary_prompt_template = config.get("summary_prompt_template", "总结日志：\n{log}")

        logger.info(f"[IntelligentRetry] 3.8.17 SpectreCore-GreenLight 已加载。")

    def _parse_config(self, config: AstrBotConfig) -> None:
        self.max_attempts = config.get("max_attempts", 3)
        self.retry_delay = config.get("retry_delay", 2)
        
        # [Config] 扩充异常检测词库 (用于 on_llm_response)
        # v3.0.0: Updated error keywords
        default_keywords = (
            "达到最大长度限制而被截断\n"
            "exception\n"
            "error\n"
            "timeout"
        )
        keywords_str = config.get("error_keywords", default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split("\n") if k.strip()]

        self.retryable_status_codes = self._parse_status_codes(config.get("retryable_status_codes", "400\n429\n502\n503\n504"))
        self.non_retryable_status_codes = self._parse_status_codes(config.get("non_retryable_status_codes", ""))
        self.fallback_reply = config.get("fallback_reply", "抱歉，服务波动，罗莎暂时无法回应。")
        self.enable_truncation_retry = config.get("enable_truncation_retry", False)
        self.force_cot_structure = config.get("force_cot_structure", True)

        # 配置化排除命令列表
        exclude_commands_str = config.get("exclude_retry_commands", "/cogito\n/rosaos\nreset\nnew")
        self.exclude_retry_commands = [
            cmd.strip().lower() 
            for cmd in exclude_commands_str.split("\n") 
            if cmd.strip()
        ]

    # ======================= 渲染辅助 =======================
    async def _render_and_reply(self, event: AstrMessageEvent, title: str, subtitle: str, content: str):
        try:
            render_data = {"title": title, "subtitle": subtitle, "content": content, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            # 高清化参数：增大 Viewport, 启用 deviceScaleFactor (如果支持)
            img_url = await self.html_render(
                LOG_TEMPLATE, 
                render_data, 
                options={
                    "viewport": {"width": 1000, "height": 1200}, # 拓宽视口
                    "deviceScaleFactor": 2, # 2x 缩放采样 (Retina级清晰度)
                    "full_page": True
                }
            )
            if img_url: yield event.image_result(img_url)
            else: yield event.plain_result(f"【渲染失败】\n{content}")
        except Exception: yield event.plain_result(f"【系统异常】\n{content}")

    # ======================= 存储层 =======================
    async def _async_save_thought(self, session_id: str, content: str):
        if not session_id or not content: return
        def _write_impl():
            try:
                date_str = datetime.now().strftime("%Y-%m-%d")
                archive_path = COLD_ARCHIVE_DIR / f"{date_str}_thought.log"
                with open(archive_path, 'a', encoding='utf-8') as f:
                    f.write(f"[{datetime.now().strftime('%H:%M:%S')}] [Session: {session_id}]\n{content}\n{'-'*40}\n")
                
                safe_name = sanitize_filename(session_id)
                json_path = HOT_STORAGE_DIR / f"{safe_name}.json"
                thoughts = []
                if json_path.exists():
                    try:
                        with open(json_path, 'r', encoding='utf-8') as f: thoughts = json.load(f)
                    except Exception: thoughts = []
                thoughts.insert(0, {"time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"), "content": content})
                if len(thoughts) > self.history_limit: thoughts = thoughts[:self.history_limit]
                with open(json_path, 'w', encoding='utf-8') as f: json.dump(thoughts, f, ensure_ascii=False, indent=2)
            except Exception: pass
        await asyncio.to_thread(_write_impl)

    async def _async_read_thought(self, session_id: str, index: int) -> Optional[str]:
        def _read_impl():
            try:
                safe_name = sanitize_filename(session_id)
                json_path = HOT_STORAGE_DIR / f"{safe_name}.json"
                if not json_path.exists(): return None
                with open(json_path, 'r', encoding='utf-8') as f: thoughts = json.load(f)
                target_idx = index - 1
                if target_idx < 0 or target_idx >= len(thoughts): return None
                content = str(thoughts[target_idx].get('content', ''))
                if content == "[NO_THOUGHT_FLAG]":
                    return "罗莎似乎并没有思考喵"
                return content
            except Exception: return None
        return await asyncio.to_thread(_read_impl)

    # --- Helper Methods ---

    def _build_incantation_pattern(self, tag: str) -> re.Pattern:
        tag_core = tag.strip("<>＜＞").strip()
        tag_escaped = re.escape(tag_core)
        open_brackets = r"[<＜]"
        close_brackets = r"[>＞]"
        slash = r"[\\/／]"
        pattern = (
            rf"{open_brackets}\s*{tag_escaped}\s*{close_brackets}"
            rf"(?P<content>.*?)"
            rf"{open_brackets}\s*{slash}\s*{tag_escaped}\s*{close_brackets}"
        )
        return re.compile(pattern, re.IGNORECASE | re.DOTALL)

    def _build_incantation_open_pattern(self, tag: str) -> re.Pattern:
        tag_core = tag.strip("<>＜＞").strip()
        tag_escaped = re.escape(tag_core)
        open_brackets = r"[<＜]"
        close_brackets = r"[>＞]"
        pattern = rf"{open_brackets}\s*{tag_escaped}\s*{close_brackets}"
        return re.compile(pattern, re.IGNORECASE)

    def _build_incantation_close_pattern(self, tag: str) -> re.Pattern:
        tag_core = tag.strip("<>＜＞").strip()
        tag_escaped = re.escape(tag_core)
        open_brackets = r"[<＜]"
        close_brackets = r"[>＞]"
        slash = r"[\\/／]"
        pattern = rf"{open_brackets}\s*{slash}\s*{tag_escaped}\s*{close_brackets}"
        return re.compile(pattern, re.IGNORECASE)

    def _split_by_final_anchor(self, text: str) -> Optional[tuple[str, str]]:
        matches = list(self.FINAL_REPLY_PATTERN.finditer(text))
        if not matches:
            return None
        last = matches[-1]
        thought = text[:last.start()].strip()
        reply = text[last.end():].strip()
        return thought, reply

    def _safe_process_response(self, text: str) -> tuple[Optional[str], str]:
        """
        [New Core] 安全响应处理
        1. 使用配置的 FINAL_REPLY_PATTERN 进行最后锚点分割
        2. 零信任拦截：有标签无锚点 -> 抛出异常
        3. 放行：无标签无锚点 -> 返回 (None, text)
        """
        if not text:
            return None, ""

        split = self._split_by_final_anchor(text)
        if split:
            thought, reply = split
            return thought, self._finalize_reply_only(reply)

        has_tag = bool(self.COT_TAG_DETECTOR.search(text))
        if has_tag:
            raise ValueError("检测到思维链标签(或其变体)但缺失锚点，触发零信任拦截。")

        return None, self._finalize_reply_only(text)

    def _finalize_reply_only(self, text: str) -> str:
        """仅清洗回复"""
        reply = text.strip()
        for kw in self.filtered_keywords:
            reply = reply.replace(kw, "")
        return reply

    def _extract_incantation_commands(self, text: str) -> tuple[list[str], str]:
        if not text or not self.INCANTATION_PATTERN:
            return [], text

        commands: list[str] = []

        def _normalize_cmd(cmd: str) -> str:
            return re.sub(r"\s+", " ", cmd).strip()

        def _replacer(match: re.Match) -> str:
            cmd_text = _normalize_cmd(match.group("content"))
            if cmd_text:
                commands.append(cmd_text)
            return ""

        cleaned = self.INCANTATION_PATTERN.sub(_replacer, text)
        return commands, cleaned

    def _has_incomplete_incantation_tag(self, text: str) -> bool:
        if not text or not self.INCANTATION_PATTERN:
            return False
        open_matches = (
            self.INCANTATION_OPEN_PATTERN.findall(text)
            if self.INCANTATION_OPEN_PATTERN
            else []
        )
        close_matches = (
            self.INCANTATION_CLOSE_PATTERN.findall(text)
            if self.INCANTATION_CLOSE_PATTERN
            else []
        )
        if not open_matches and not close_matches:
            return False
        if not self.INCANTATION_PATTERN.search(text):
            return True
        return len(open_matches) != len(close_matches)

    def _has_incomplete_dossier_tag(self, text: str) -> bool:
        if not text:
            return False
        if self.DOSSIER_TAG_PATTERN.search(text):
            return False
        return bool(
            self.DOSSIER_OPEN_PATTERN.search(text)
            or self.DOSSIER_CLOSE_PATTERN.search(text)
        )

    def _is_spectrecore_event(self, event: AstrMessageEvent) -> bool:
        handlers = event.get_extra("activated_handlers", []) or []
        for h in handlers:
            module_path = getattr(h, "handler_module_path", "") or ""
            if "astrbot_plugin_spectrecorepro" in module_path:
                return True
        return False

    def _resolve_event(self, event: Any, *args) -> Optional[AstrMessageEvent]:
        if isinstance(event, AstrMessageEvent):
            return event
        if args and isinstance(args[0], AstrMessageEvent):
            return args[0]
        return None

    def _normalize_newlines(self, text: str, event: AstrMessageEvent | None = None) -> str:
        """
        将所有换行移除（与关键词过滤类似的“直接删除”方式），
        仅对 spectrecore 事件且开关开启时生效。
        """
        if not text or not self.clean_spectrecore_newlines:
            return text
        if event and not self._is_spectrecore_event(event):
            return text
        text = text.replace("\r\n", "").replace("\r", "").replace("\n", "")
        return text.strip()

    def _enqueue_command_event(self, event: AstrMessageEvent, cmd_text: str) -> None:
        new_event = copy.copy(event)
        new_event._extras = {}
        new_event.clear_result()
        new_event.message_str = cmd_text

        msg_obj = new_event.message_obj
        if msg_obj:
            msg_obj = copy.copy(msg_obj)
            msg_obj.message_str = cmd_text
            msg_obj.message = [Comp.Plain(cmd_text)]
            new_event.message_obj = msg_obj

        new_event.set_extra("incantation_command", True)
        new_event.should_call_llm(True)
        self.context.get_event_queue().put_nowait(new_event)

    def _try_enqueue_command_event(self, event: AstrMessageEvent, cmd_text: str) -> bool:
        try:
            logger.info(f"[IntelligentRetry] ✨ 开始调用咒语指令: {cmd_text}")
            self._enqueue_command_event(event, cmd_text)
            logger.info(f"[IntelligentRetry] ✅ 咒语指令已入队: {cmd_text}")
            return True
        except Exception as e:
            logger.warning(f"[IntelligentRetry] ❌ 咒语指令入队失败: {cmd_text} | {e}")
            return False
    @event_filter.command("rosaos")
    async def get_rosaos_log(self, event: AstrMessageEvent, index: str = "1"):
        """获取内心OS"""
        idx = int(index) if index.isdigit() else 1
        log_content = await self._async_read_thought(event.unified_msg_origin, idx)
        if not log_content: yield event.plain_result(f"📭 未找到第 {idx} 条记录。")
        else:
            async for msg in self._render_and_reply(event, "罗莎内心记录", f"Index: {idx}", log_content): yield msg

    @event_filter.command("cogito")
    async def handle_cogito(self, event: AstrMessageEvent, index: str = "1"):
        """认知分析"""
        idx = int(index) if index.isdigit() else 1
        log_content = await self._async_read_thought(event.unified_msg_origin, idx)
        if not log_content: yield event.plain_result("📭 找不到该条日志。"); return
        target_provider_id = self.summary_provider_id or await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        if not target_provider_id: yield event.plain_result("❌ 无法获取模型 Provider。"); return

        yield event.plain_result(f"🧠 分析中... (Index: {idx})")
        prompt = self.summary_prompt_template.replace("{log}", log_content)
        success = False; final_summary = ""
        for _ in range(self.summary_max_retries):
            try:
                resp = await asyncio.wait_for(self.context.llm_generate(chat_provider_id=target_provider_id, prompt=prompt), timeout=self.summary_timeout)
                if resp and resp.completion_text: final_summary = resp.completion_text; success = True; break
            except Exception: pass
        if success:
            async for msg in self._render_and_reply(event, "COGITO 分析报告", f"Index {idx}", final_summary): yield msg
        else: yield event.plain_result("⚠️ 分析超时。")



    @event_filter.on_llm_request(priority=70)
    async def store_llm_request(self, event: AstrMessageEvent, req, *args):
        """记录请求上下文"""
        if not hasattr(req, "prompt"):
            return
        # 检查是否是排除命令（配置化）
        msg_lower = (event.message_str or "").strip().lower()
        if any(msg_lower.startswith(cmd) for cmd in self.exclude_retry_commands):
            return

        msg_obj = getattr(event, "message_obj", None)
        image_urls = []
        if msg_obj and hasattr(msg_obj, "message"):
            image_urls = [c.url for c in msg_obj.message if isinstance(c, Comp.Image) and c.url]
            
        sender_info = {
            "user_id": getattr(msg_obj, "user_id", None) if msg_obj else None,
            "nickname": getattr(msg_obj, "nickname", None) if msg_obj else None,
            "group_id": getattr(msg_obj, "group_id", None) if msg_obj else None,
            "platform": getattr(msg_obj, "platform", None) if msg_obj else None,
        }

        request_key = self._get_request_key(event)

        stored_params = {
            "prompt": req.prompt,
            # 避免后续阶段/插件对 req.contexts 的原地修改影响重试上下文
            "contexts": copy.deepcopy(getattr(req, "contexts", [])),
            "image_urls": image_urls,
            "system_prompt": getattr(req, "system_prompt", ""),
            "func_tool": getattr(req, "func_tool", None),
            "unified_msg_origin": event.unified_msg_origin,
            # Bug 1.1: Store conversation_id instead of live object
            "conversation_id": getattr(req.conversation, "id", None) if hasattr(req, "conversation") else None,
            "timestamp": time.time(),
            "sender": sender_info,
            "provider_params": {k: getattr(req, k, None) for k in ["model", "temperature", "max_tokens"] if hasattr(req, k)}
        }
        self.pending_requests[request_key] = stored_params



    @event_filter.on_llm_response(priority=5)
    async def process_and_retry_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        # 0. 原始数据获取
        raw_text = getattr(resp, "completion_text", "") or ""
        # run_agent 异常分支会先触发 on_llm_response，然后再把 event.result 强制覆盖为 err_msg；
        # 如果此处触发重试会导致：
        # 1) 重试结果被覆盖（用户仍收到错误消息）
        # 2) retry_guard 被提前设置，阻止 on_decorating_result 阶段的拦截重试
        if getattr(resp, "role", None) == "err" and "AstrBot 请求失败" in raw_text:
            return

        # 1. 安全处理 (Safe Processing)
        # 此时不修改 resp，也不写日志
        try:
            thought_content, reply_content = self._safe_process_response(raw_text)
            is_valid_structure = True
        except ValueError as e:
            # 捕获到安全异常
            logger.warning(f"[IntelligentRetry] 🛡️ {e}")
            thought_content, reply_content = None, ""
            is_valid_structure = False
        has_incomplete_incantation = self._has_incomplete_incantation_tag(raw_text)
        if has_incomplete_incantation:
            logger.warning(
                "[IntelligentRetry] 🛡️ 检测到不完整的咒语标签，触发重试。",
            )
        has_incomplete_dossier = self._has_incomplete_dossier_tag(raw_text)
        if has_incomplete_dossier:
            logger.warning(
                "[IntelligentRetry] 🛡️ 检测到不完整的档案标签，触发重试。",
            )

        # 如果响应直接是空的或者带有错误标记，也视为需要重试
        is_tool_call = False
        if getattr(resp, "raw_completion", None):
            choices = getattr(resp.raw_completion, "choices", [])
            if choices and getattr(choices[0], "finish_reason", None) == "tool_calls": 
                is_tool_call = True

        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests: return
        if self._retry_guard_hit(request_key):
            return

        # ================= [SpectreCore 绿灯通道] =================
        if "<NO_RESPONSE>" in raw_text:
            logger.info(f"[IntelligentRetry] 🟢 检测到 <NO_RESPONSE>，放行静默请求 (Key: {request_key})")
            return
        # ========================================================

        is_trunc = self.enable_truncation_retry and self._is_truncated(resp)
        
        # [Check] 检查原始响应是否包含报错
        raw_str = str(getattr(resp, "raw_completion", "")).lower()
        is_error = "error" in raw_str and ("upstream" in raw_str or "500" in raw_str)
        
        needs_retry = not is_tool_call and (
            not raw_text.strip()
            or self._should_retry_response(resp)
            or is_trunc
            or not is_valid_structure
            or is_error
            or has_incomplete_incantation
            or has_incomplete_dossier
        )
        
        if needs_retry:
            logger.info(f"[IntelligentRetry] 🔴 触发重试逻辑 (Key: {request_key})")
            self._set_retry_guard(request_key)

            # 物理静音防止报错泄漏
            self._silence_event(event)

            # 进入重试循环
            success = await self._execute_retry_sequence(event, request_key)
            if success:
                res = event.get_result()
                resp.completion_text = res.get_plain_text() if res else ""
            else:
                if self.fallback_reply:
                    await event.send(event.plain_result(self.fallback_reply))
                    resp.completion_text = ""
        else:
            # 2. 成功提交 (Submission) - 仅在无需重试时执行
            
            # A. 应用清洗后的回复 (Commit Reply)
            if self.display_cot_text and thought_content:
                resp.completion_text = f"🤔 罗莎思考中：\n{thought_content}\n\n---\n\n{reply_content}"
            else:
                resp.completion_text = reply_content
                
            # B. 日志缓冲提交 (Commit Log)
            # 只有确认成功后才写入。若无思考内容，写入哨兵标记
            log_payload = thought_content if thought_content else "[NO_THOUGHT_FLAG]"
            await self._async_save_thought(event.unified_msg_origin, log_payload)
        
    @event_filter.on_decorating_result(priority=20)
    async def intercept_api_error(self, event: AstrMessageEvent, *args):
        """
        [NEW] 异常拦截层 (Priority=20) - 物理静音版
        使用正则表达式强力捕获 Core 抛出的格式化异常。
        """
        event = self._resolve_event(event, *args)
        if not event:
            return
        request_key = self._get_request_key(event)
        # Fix: 不要在这里做 pop 操作，否则重试中途如果并发触发，Key 没了会导致重试失败。
        # 依赖 _periodic_cleanup_task 清理即可。
        if request_key not in self.pending_requests: return
        if self._retry_guard_hit(request_key):
            return

        result = event.get_result()
        if not result: return

        text = result.get_plain_text() or ""

        # 使用统一的错误检测逻辑
        has_api_error = self._has_api_error_pattern(text)
        has_config_keyword = any(kw.lower() in text.lower() for kw in self.error_keywords)

        # 判定逻辑：如果检测到 API 错误或包含配置关键词
        if has_api_error or has_config_keyword:
            logger.warning(f"[IntelligentRetry] 🛡️ 拦截到 Core 异常 (Key: {request_key}) | 内容片段: {text[:50]}...")
            self._set_retry_guard(request_key)

            # --- CRITICAL FIX: 物理静音 ---
            # 必须彻底清空 Chain，否则 Core 可能会发送残余信息
            self._silence_event(event)
            
            # 启动重试
            success = await self._execute_retry_sequence(event, request_key)
            
            if success:
                logger.info(f"[IntelligentRetry] 🛡️ 异常拦截重试成功！")
            else:
                # 重试失败，强制应用兜底
                if self.fallback_reply:
                    self._apply_fallback(event)
            
            # Fix: 移除 pop 操作，保持上下文直到自然过期

    @event_filter.on_decorating_result(priority=5)
    async def final_cot_stripper(self, event: AstrMessageEvent, *args):
        """最后一道防线：全局清洗"""
        event = self._resolve_event(event, *args)
        if not event:
            return
        result = event.get_result()
        if not result or not result.chain or not result.is_llm_result():
            return
        
        # 获取全文进行判断，避免组件碎片化处理导致的部分替换、部分泄露
        plain_text = result.get_plain_text()
        if not plain_text:
            return
        
        # 使用正则进行模糊匹配，兼容中英文括号
        has_tag = bool(self.COT_TAG_DETECTOR.search(plain_text))
        has_anchor = bool(self.FINAL_REPLY_PATTERN.search(plain_text))

        if has_tag or has_anchor:
            try:
                # 尝试对全文进行提取
                _, reply = self._safe_process_response(plain_text)

                # 如果成功提取（找到了锚点），重构消息链只保留回复
                # 这是一个破坏性操作，但在防泄露场景下是必要的
                result.chain.clear()
                result.chain.append(Comp.Plain(reply))

            except ValueError:
                # 如果全文判定非法（有标签无锚点），全量替换为兜底
                result.chain.clear()
                result.chain.append(Comp.Plain(self.fallback_reply))

    @event_filter.on_decorating_result(priority=4)
    async def dispatch_tool_command(self, event: AstrMessageEvent, *args):
        event = self._resolve_event(event, *args)
        if not event:
            return
        result = event.get_result()
        if not result or not result.chain or not result.is_llm_result():
            return

        plain_text = result.get_plain_text()
        if not plain_text:
            return

        commands, cleaned = self._extract_incantation_commands(plain_text)
        if commands:
            result.chain.clear()
            if cleaned.strip():
                result.chain.append(Comp.Plain(cleaned.strip()))
            has_failure = False
            for cmd_text in commands:
                ok = self._try_enqueue_command_event(event, cmd_text)
                if not ok:
                    has_failure = True
            if has_failure and self.incantation_fallback_reply:
                result.chain.append(Comp.Plain(self.incantation_fallback_reply))
            return

        return

    @event_filter.on_decorating_result(priority=-999)
    async def normalize_spectrecore_newlines(self, event: AstrMessageEvent, *args):
        event = self._resolve_event(event, *args)
        if not event:
            return
        if not self.clean_spectrecore_newlines:
            return
        if not self._is_spectrecore_event(event):
            return
        result = event.get_result()
        if not result or not result.chain:
            return
        if result.result_content_type not in (
            ResultContentType.LLM_RESULT,
            ResultContentType.STREAMING_FINISH,
        ):
            return
        normalized = []
        for comp in result.chain:
            if isinstance(comp, Comp.Plain):
                comp = Comp.Plain(self._normalize_newlines(comp.text, event))
            normalized.append(comp)
        result.chain = normalized

    # --- Helper Methods ---

    def _silence_event(self, event: AstrMessageEvent):
        """
        [NEW] 物理静音：清空消息链，防止报错泄漏
        这比 set_result(None) 更安全，因为它保留了对象但清空了内容。
        """
        result = event.get_result()
        if result:
            # 清空消息组件列表
            if result.chain:
                result.chain.clear()
            # 清空文本缓存
            if hasattr(result, "plain_text"): 
                result.plain_text = ""
            # 确保不回退到 raw_message
            if hasattr(result, "use_raw"):
                result.use_raw = False
        else:
            # 如果没有 result，创建一个空的
            empty_res = MessageEventResult()
            empty_res.chain = []
            event.set_result(empty_res)

    def _apply_fallback(self, event: AstrMessageEvent):
        """应用兜底回复"""
        logger.warning(f"[IntelligentRetry] ❌ 重试耗尽，应用兜底回复")
        anti_spam_suffix = "\u200b" * (int(time.time()) % 3) 
        final_fallback = f"{self.fallback_reply}{anti_spam_suffix}"
        
        final_res = MessageEventResult()
        final_res.message(final_fallback)
        final_res.result_content_type = ResultContentType.LLM_RESULT
        event.set_result(final_res)

    def _is_truncated(self, text_or_response) -> bool:
        text = text_or_response.completion_text if hasattr(text_or_response, "completion_text") else text_or_response
        if hasattr(text_or_response, "completion_text") and "[TRUNCATED_BY_LENGTH]" in (text or ""): return True
        return False

    async def _periodic_cleanup_task(self):
        while True:
            try:
                await asyncio.sleep(60)
                now = time.time()
                keys_to_remove = [k for k, v in self.pending_requests.items() if now - v.get("timestamp", 0) > 300]
                for k in keys_to_remove:
                    if k in self.pending_requests:
                        del self.pending_requests[k]
            except Exception: 
                await asyncio.sleep(10)

    def _parse_status_codes(self, codes_str: str) -> set:
        return {int(line.strip()) for line in codes_str.split("\n") if line.strip().isdigit()}

    def _get_request_key(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "_retry_plugin_request_key"): 
            return event._retry_plugin_request_key
        trace_id = uuid.uuid4().hex[:8]
        key = f"{event.unified_msg_origin}_{trace_id}"
        event._retry_plugin_request_key = key
        return key

    def _retry_guard_hit(self, request_key: str) -> bool:
        stored = self.pending_requests.get(request_key)
        return bool(stored and stored.get("retry_guard"))

    def _set_retry_guard(self, request_key: str) -> None:
        stored = self.pending_requests.get(request_key)
        if stored is not None:
            stored["retry_guard"] = True

    def _should_retry_response(self, result) -> bool:
        if not result: return True
        text = getattr(result, "completion_text", "") or ""
        if not text and hasattr(result, "get_plain_text"): text = result.get_plain_text()
        if not (text or "").strip(): return True
        
        # Keyword-based detection
        for kw in self.error_keywords:
            if kw in text.lower(): return True
        
        # Regex-based detection (unified with intercept_api_error)
        if self._has_api_error_pattern(text):
            return True
            
        return False
    
    def _has_api_error_pattern(self, text: str) -> bool:
        """统一的 API 错误检测逻辑（正则表达式）"""
        if not text: return False
        
        # 1. AstrBot 失败标记
        is_astrbot_fail = "AstrBot" in text and "请求失败" in text
        if is_astrbot_fail: return True
        
        # 2. 错误模式匹配
        error_patterns = [
            r"Error\s*code:\s*5\d{2}",       # 500, 502, 503, 504...
            r"APITimeoutError",
            r"Request\s*timed\s*out",
            r"InternalServerError",
            r"count_token_failed",
            r"bad_response_status_code",
            r"connection\s*error",
            r"remote\s*disconnected",
            r"read\s*timeout",
            r"connect\s*timeout"
        ]
        
        combined_pattern = re.compile("|".join(error_patterns), re.IGNORECASE)
        return bool(combined_pattern.search(text))

    async def _fix_user_history(self, event: AstrMessageEvent, request_key: str, bot_reply: str = None):
        """
        Bug 1.3: Manually add the user's prompt to the conversation history
        to prevent disjointed context (assistant -> assistant).
        """
        try:
            stored_params = self.pending_requests.get(request_key)
            if not stored_params: return

            conv_mgr = self.context.conversation_manager
            umo = event.unified_msg_origin
            cid = stored_params.get("conversation_id")
            if not cid: cid = await conv_mgr.get_curr_conversation_id(umo)
            
            conv = await conv_mgr.get_conversation(umo, cid)
            prompt = stored_params.get("prompt")

            if conv and prompt:
                history_list = json.loads(conv.history) if conv.history else []
                if not history_list or history_list[-1].get("content") != prompt:
                    history_list.append({"role": "user", "content": prompt})
                    logger.debug(f"已为会话 {cid} 手动补全用户历史记录")
                
                if bot_reply:
                    history_list.append({"role": "assistant", "content": bot_reply})
                    logger.debug(f"已为会话 {cid} 手动补全Bot回复历史记录")

                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=umo, conversation_id=cid, history=history_list
                )
        except Exception as e:
            logger.error(f"手动补全历史记录时出错: {e}", exc_info=True)

    async def _perform_retry_with_stored_params(self, request_key: str) -> Optional[Any]:
        if request_key not in self.pending_requests: return None
        stored = self.pending_requests[request_key]
        provider = self.context.get_using_provider()
        if not provider: return None
        try:
            kwargs = {
                "prompt": stored.get("prompt"),
                "image_urls": copy.deepcopy(stored.get("image_urls", [])),
                "func_tool": stored.get("func_tool"),
                "system_prompt": stored.get("system_prompt"),
            }
            
            # Bug 1.1 & 1.2: Reconstruct conversation and contexts
            conversation_id = stored.get("conversation_id")
            unified_msg_origin = stored.get("unified_msg_origin")
            
            if conversation_id and unified_msg_origin:
                conv_mgr = getattr(self.context, "conversation_manager", None)
                if conv_mgr:
                    conversation = await conv_mgr.get_conversation(unified_msg_origin, conversation_id)
                    if conversation:
                        kwargs["conversation"] = conversation
                        # Restore sender info if needed
                        if not hasattr(conversation, "metadata") or not conversation.metadata:
                            conversation.metadata = {}
                        conversation.metadata["sender"] = stored.get("sender", {})

            # Bug 1.2: Context reconstruction
            # 注意：Provider.text_chat 在 prompt 与 contexts 同时存在时，会把 prompt 作为最新记录追加到 contexts 中。
            # 这里必须避免对 stored["contexts"] 原地 append，否则多次重试会导致上下文膨胀/重复。
            kwargs["contexts"] = copy.deepcopy(stored.get("contexts", []))
            
            kwargs.update(stored.get("provider_params", {}))
            
            # --- 核心修复：防御性调用 ---
            return await provider.text_chat(**kwargs)
            
        except Exception as e:
            logger.error(f"[IntelligentRetry] ⚠️ 重试尝试失败 (Provider API 抛出异常): {e}")
            return None

    async def _execute_retry_sequence(self, event: AstrMessageEvent, request_key: str) -> bool:
        """
        [Audited Fix] 执行重试循环
        修正了异常吞噬问题，确保格式错误(ValueError)必定触发下一次重试。
        """
        delay = max(0, int(self.retry_delay))
        session_id = event.unified_msg_origin
        
        for attempt in range(self.max_attempts):
            current_attempt = attempt + 1
            logger.warning(f"[IntelligentRetry] 🔄 (Session: {session_id}) 正在执行第 {current_attempt}/{self.max_attempts} 次重试...")
            
            # 1. 执行请求
            new_response = await self._perform_retry_with_stored_params(request_key)
            
            # 2. 检查响应是否存在
            if not new_response or not getattr(new_response, "completion_text", ""):
                 logger.warning(f"[IntelligentRetry] ⚠️ 第 {current_attempt} 次重试返回空 (可能再次超时)")
                 if current_attempt < self.max_attempts: await asyncio.sleep(delay * current_attempt)
                 continue # 强制进入下一次循环

            raw_text = new_response.completion_text
            
            # 3. 结构安全检查 (Zero Trust)
            try:
                thought, reply = self._safe_process_response(raw_text)
                # 如果能走到这里，说明结构合法
            except ValueError as e:
                # [Critical Fix] 捕获格式错误，绝对不能吞噬，必须 continue
                logger.warning(f"格式错误，正在进行第 {current_attempt}/{self.max_attempts} 次重试...")
                logger.warning(f"[IntelligentRetry] ⚠️ 第 {current_attempt} 次重试格式校验失败: {e} | 片段: {raw_text[:30]}...")
                if current_attempt < self.max_attempts: await asyncio.sleep(delay * current_attempt)
                continue # 强制进入下一次循环
            
            # 4. 内容关键词/API错误检查
            if self._has_incomplete_incantation_tag(raw_text):
                logger.warning(
                    f"[IntelligentRetry] ⚠️ 第 {current_attempt} 次重试检测到不完整咒语标签",
                )
                if current_attempt < self.max_attempts:
                    await asyncio.sleep(delay * current_attempt)
                continue

            if self._has_incomplete_dossier_tag(raw_text):
                logger.warning(
                    f"[IntelligentRetry] ⚠️ 第 {current_attempt} 次重试检测到档案标签不完整",
                )
                if current_attempt < self.max_attempts:
                    await asyncio.sleep(delay * current_attempt)
                continue

            if self._should_retry_response(new_response):
                logger.warning(f"[IntelligentRetry] ⚠️ 第 {current_attempt} 次重试触发内容拦截 (API Error/Keywords)")
                if current_attempt < self.max_attempts: await asyncio.sleep(delay * current_attempt)
                continue # 强制进入下一次循环

            # ================= 成功出口 =================
            logger.info(f"[IntelligentRetry] ✅ 第 {current_attempt} 次重试成功")
            
            # A. 补全历史
            await self._fix_user_history(event, request_key, bot_reply=reply)
            
            # B. 日志存储
            log_payload = thought if thought else "[NO_THOUGHT_FLAG]"
            await self._async_save_thought(session_id, log_payload)
            
            # C. 更新结果
            final_res = MessageEventResult()
            if self.display_cot_text and thought:
                final_res.message(f"🤔 罗莎思考中：\n{thought}\n\n---\n\n{reply}")
            else:
                final_res.message(reply)
                
            final_res.result_content_type = ResultContentType.LLM_RESULT
            event.set_result(final_res)
            
            return True # 任务完成
        
        # 循环结束仍未返回 True，说明全部失败
        logger.error(f"[IntelligentRetry] ❌ {self.max_attempts} 次重试全部失败。")
        return False

    async def terminate(self):
        self._cleanup_task.cancel()
        self.pending_requests.clear()
        logger.info("[IntelligentRetry] 插件已卸载")

# --- END OF FILE main.py ---
