# --- START OF FILE main.py ---

import asyncio
import json
import re
import time
import os
import uuid
import types
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter as event_filter, MessageEventResult, ResultContentType
from astrbot.api.provider import LLMResponse

# --- 存储架构配置 (保留原版) ---
HOT_STORAGE_DIR = Path("data/cot_os_logs/sessions")
COLD_ARCHIVE_DIR = Path("data/cot_os_logs/daily_archive")

HOT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
COLD_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

# --- HTML 渲染模板 (IMAX HD Version - 保留原版) ---
LOG_TEMPLATE = """
<!DOCTYPE html>
<html lang="zh-CN">
<head>
    <meta charset="UTF-8">
    <style>
        /* 引入系统级字体栈，确保渲染清晰 */
        body {
            font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, 'Helvetica Neue', Arial, 'PingFang SC', 'Microsoft YaHei', sans-serif;
            background-color: #1a1a1a;
            margin: 0;
            padding: 0;
            display: inline-block;
            width: 100%;
        }
        
        .container {
            padding: 20px;
            box-sizing: border-box;
        }

        .card {
            background: #252525;
            border-radius: 16px;
            box-shadow: 0 10px 40px rgba(0, 0, 0, 0.6);
            border: 1px solid rgba(255, 255, 255, 0.08);
            overflow: hidden;
            width: 100%; 
            max-width: 800px;
            margin: 0 auto;
        }

        .header {
            background: linear-gradient(135deg, #2c3e50 0%, #000000 100%);
            padding: 25px 30px;
            border-bottom: 1px solid rgba(255, 255, 255, 0.1);
            display: flex;
            align-items: center;
            justify-content: space-between;
        }

        .title {
            font-size: 26px; 
            font-weight: 800;
            color: #ffffff;
            letter-spacing: 0.5px;
            -webkit-font-smoothing: antialiased;
            text-shadow: 0 2px 4px rgba(0,0,0,0.5);
        }

        .badge {
            font-size: 16px;
            font-weight: 600;
            background: rgba(255, 255, 255, 0.15);
            padding: 6px 14px;
            border-radius: 8px;
            color: #64b5f6;
            backdrop-filter: blur(4px);
        }

        .content {
            padding: 35px;
            font-size: 22px; /* 字号大幅提升，保证缩放后清晰 */
            line-height: 1.6;
            color: #e0e0e0;
            white-space: pre-wrap;
            text-align: justify;
            font-weight: 400;
            -webkit-font-smoothing: antialiased;
        }

        .footer {
            padding: 20px 35px;
            background: #1e1e1e;
            border-top: 1px solid rgba(255, 255, 255, 0.05);
            font-size: 15px;
            color: #777;
            text-align: right;
            font-family: 'JetBrains Mono', Consolas, monospace;
        }

        strong { color: #ffb74d; font-weight: 700; }
        em { 
            color: #4fc3f7; 
            font-style: normal; 
            background: rgba(79, 195, 247, 0.1);
            padding: 2px 6px;
            border-radius: 4px;
        }
    </style>
</head>
<body>
    <div class="container">
        <div class="card">
            <div class="header">
                <span class="title">{{ title }}</span>
                <span class="badge">{{ subtitle }}</span>
            </div>
            <div class="content">{{ content }}</div>
            <div class="footer">COGITO SYSTEM &bull; {{ timestamp }}</div>
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
    "集成了思维链(CoT)处理的智能重试插件。v3.10.0 Dual-Core Engine (Patch + Event).",
    "3.10.0-Rosa-DualCore",
)
class IntelligentRetryWithCoT(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task())
        self._parse_config(config)
        
        # --- 罗莎配置 ---
        self.cot_start_tag = config.get("cot_start_tag", "<罗莎内心OS>")
        self.cot_end_tag = config.get("cot_end_tag", "</罗莎内心OS>")
        self.final_reply_pattern_str = config.get("final_reply_pattern", r"最终的罗莎回复[:：]?\s*")
        
        self.FINAL_REPLY_PATTERN = re.compile(self.final_reply_pattern_str, re.IGNORECASE)
        escaped_start = re.escape(self.cot_start_tag)
        escaped_end = re.escape(self.cot_end_tag)
        self.THOUGHT_TAG_PATTERN = re.compile(f'{escaped_start}(?P<content>.*?){escaped_end}', re.DOTALL)
        
        self.display_cot_text = config.get("display_cot_text", False)
        self.filtered_keywords = config.get("filtered_keywords", ["呵呵，", "（……）"])
        
        # --- 总结配置 ---
        self.summary_provider_id = config.get("summary_provider_id", "")
        self.summary_max_retries = max(1, int(config.get("summary_max_retries", 2)))
        self.history_limit = int(config.get("history_limit", 100))
        self.summary_timeout = int(config.get("summary_timeout", 60))
        self.summary_prompt_template = config.get("summary_prompt_template", "总结日志：\n{log}")

        logger.info(f"[IntelligentRetry] 3.10.0 双核引擎已加载 (Patch + Regex Guard)。")

    def _parse_config(self, config: AstrBotConfig) -> None:
        self.max_attempts = config.get("max_attempts", 3)
        self.retry_delay = config.get("retry_delay", 2)
        
        # [Config] 异常检测词库
        default_keywords = (
            "达到最大长度限制而被截断\n"
            "exception\n"
            "error\n"
            "timeout"
        )
        keywords_str = config.get("error_keywords", default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split("\n") if k.strip()]

        self.retryable_status_codes = self._parse_status_codes(config.get("retryable_status_codes", "400\n429\n502\n503\n504"))
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

    # ======================= Layer 0: Monkey Patch (Kernel) =======================
    # 这一层负责防止 Timeout/503 导致 Crash。它在 Core 抛出异常前进行拦截。
    
    def _patch_provider_method(self):
        """
        动态劫持 Provider 的 text_chat 方法。
        """
        provider = self.context.get_using_provider()
        if not provider: return

        # 防止重复 Patch
        if getattr(provider, "_rosa_patched_hybrid_v1", False):
            return

        original_text_chat = provider.text_chat
        logger.info(f"[IntelligentRetry] 💉 正在注入混合动力补丁 (Kernel Layer)...")

        async def patched_text_chat(_self, **kwargs):
            # 1. 白名单检测 (保留原版逻辑)
            current_prompt = kwargs.get("prompt", "")
            if not current_prompt and kwargs.get("contexts"):
                 try:
                    for msg in reversed(kwargs["contexts"]):
                        if isinstance(msg, dict) and msg.get("role") == "user":
                            current_prompt = msg.get("content", ""); break
                        elif hasattr(msg, "role") and msg.role == "user":
                            current_prompt = getattr(msg, "content", ""); break
                 except Exception: pass
            
            if current_prompt:
                prompt_lower = str(current_prompt).strip().lower()
                for cmd in self.exclude_retry_commands:
                    if prompt_lower.startswith(cmd):
                        return await original_text_chat(**kwargs)

            # 2. 底层重试循环
            max_retries = self.max_attempts
            delay = self.retry_delay
            
            for attempt in range(1, max_retries + 2):
                try:
                    return await original_text_chat(**kwargs)
                except Exception as e:
                    error_str = str(e).lower()
                    critical_errors = ["timeout", "502", "503", "504", "connection", "rate limit", "overloaded", "server error", "readtimeout"]
                    is_critical = any(k in error_str for k in critical_errors)
                    
                    if attempt <= max_retries and is_critical:
                        logger.warning(f"[IntelligentRetry] 🛡️ 底层拦截异常: {e} | 重试中 ({attempt}/{max_retries})...")
                        await asyncio.sleep(delay)
                        continue
                    
                    # 关键点：底层耗尽后，不抛出异常，而是返回一个特殊的 LLMResponse
                    # 这样 AstrBot 不会 Crash，而是继续流转到上层的 on_decorating_result (Regex Guard)
                    # 从而触发你原版的高级重试逻辑
                    if attempt > max_retries:
                        logger.error(f"[IntelligentRetry] ❌ 底层重试耗尽，向下层传递异常信号。")
                        err_resp = LLMResponse()
                        err_resp.completion_text = f"ROSA_INTERNAL_ERROR: {str(e)}"
                        err_resp.raw_completion = {"error": str(e), "failed": True}
                        return err_resp
                    raise e
        
        provider.text_chat = types.MethodType(patched_text_chat, provider)
        provider._rosa_patched_hybrid_v1 = True
        logger.info(f"[IntelligentRetry] ✅ 注入成功！双层防御体系已就绪。")

    # ======================= Layer 1: Application Logic (Your Original Code) =======================
    
    # [保留原版] 渲染辅助
    async def _render_and_reply(self, event: AstrMessageEvent, title: str, subtitle: str, content: str):
        try:
            render_data = {"title": title, "subtitle": subtitle, "content": content, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            img_url = await self.html_render(LOG_TEMPLATE, render_data, options={"viewport": {"width": 640, "height": 800}, "full_page": True})
            if img_url: yield event.image_result(img_url)
            else: yield event.plain_result(f"【渲染失败】\n{content}")
        except Exception: yield event.plain_result(f"【系统异常】\n{content}")

    # [保留原版] 存储层
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
                return str(thoughts[target_idx].get('content', ''))
            except Exception: return None
        return await asyncio.to_thread(_read_impl)

    # [保留原版] 功能指令
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
                # 这里的调用也会享受到 Layer 0 的保护
                resp = await asyncio.wait_for(self.context.llm_generate(chat_provider_id=target_provider_id, prompt=prompt), timeout=self.summary_timeout)
                if resp and resp.completion_text: final_summary = resp.completion_text; success = True; break
            except Exception: pass
        if success:
            async for msg in self._render_and_reply(event, "COGITO 分析报告", f"Index {idx}", final_summary): yield msg
        else: yield event.plain_result("⚠️ 分析超时。")

    # ======================= 核心重试逻辑 (原版代码恢复) =======================

    @event_filter.on_llm_request(priority=70)
    async def store_llm_request(self, event: AstrMessageEvent, req):
        """记录请求上下文 - 关键步骤：确保存储了参数，以便 Regex Guard 可以发起重试"""
        # 0. 尝试注入底层补丁
        self._patch_provider_method()

        if not hasattr(req, "prompt"): return
        msg_lower = (event.message_str or "").strip().lower()
        if any(msg_lower.startswith(cmd) for cmd in self.exclude_retry_commands): return

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
        # 完整保存上下文，供上层重试使用
        stored_params = {
            "prompt": req.prompt,
            "contexts": getattr(req, "contexts", []),
            "image_urls": image_urls,
            "system_prompt": getattr(req, "system_prompt", ""),
            "func_tool": getattr(req, "func_tool", None),
            "unified_msg_origin": event.unified_msg_origin,
            "conversation_id": getattr(req.conversation, "id", None) if hasattr(req, "conversation") else None,
            "timestamp": time.time(),
            "sender": sender_info,
            "provider_params": {k: getattr(req, k, None) for k in ["model", "temperature", "max_tokens"] if hasattr(req, k)}
        }
        self.pending_requests[request_key] = stored_params

    @event_filter.on_llm_response(priority=5)
    async def process_and_retry_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        # 1. CoT 裁剪
        if resp and hasattr(resp, "completion_text") and self.cot_start_tag in (resp.completion_text or ""):
            await self._split_and_format_cot(resp, event)

        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests: return

        text = getattr(resp, "completion_text", "") or ""
        
        # 2. 检查 Layer 0 是否传递了失败信号
        layer0_failed = "ROSA_INTERNAL_ERROR" in text or (hasattr(resp, "raw_completion") and resp.raw_completion.get("failed"))

        is_trunc = self.enable_truncation_retry and self._is_truncated(resp)
        is_error = "error" in text.lower() and ("upstream" in text.lower() or "500" in text.lower())

        needs_retry = layer0_failed or not text.strip() or self._should_retry_response(resp) or is_trunc or self._is_cot_structure_incomplete(text) or is_error
        
        if needs_retry:
            logger.info(f"[IntelligentRetry] 🔴 Layer 1 接管：触发上层重试逻辑 (Key: {request_key})")
            success = await self._execute_retry_sequence(event, request_key)
            if success:
                res = event.get_result()
                resp.completion_text = res.get_plain_text() if res else ""
            else:
                if self.fallback_reply:
                    self._apply_fallback(event)
                    resp.completion_text = self.fallback_reply

    @event_filter.on_decorating_result(priority=20)
    async def intercept_api_error(self, event: AstrMessageEvent):
        """
        [原版 Regex Guard] 
        如果底层 Patch 失败，或者模型输出了 "I cannot answer this" 这类非异常但无效的内容，
        这里会拦截并触发重试。
        """
        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests: return

        result = event.get_result()
        text = result.get_plain_text() or ""

        has_api_error = self._has_api_error_pattern(text)
        has_config_keyword = any(kw.lower() in text.lower() for kw in self.error_keywords)
        is_internal_fail = "ROSA_INTERNAL_ERROR" in text # Layer 0 传递的信号

        if has_api_error or has_config_keyword or is_internal_fail:
            logger.warning(f"[IntelligentRetry] 🛡️ Regex Guard 拦截到异常 (Key: {request_key})")
            
            event.set_result(None) # 阻断原始报错
            
            # 使用存储的参数进行重试 (这是原版逻辑的核心优势)
            success = await self._execute_retry_sequence(event, request_key)
            
            if success:
                logger.info(f"[IntelligentRetry] 🛡️ 拦截重试成功！")
            else:
                if self.fallback_reply:
                    self._apply_fallback(event)
            
            self.pending_requests.pop(request_key, None)

    @event_filter.on_decorating_result(priority=5)
    async def final_cot_stripper(self, event: AstrMessageEvent):
        """最后一道防线 (保留原版)"""
        result = event.get_result()
        if not result or not result.chain: return
        plain_text = result.get_plain_text()
        has_tag = self.cot_start_tag in plain_text or self.FINAL_REPLY_PATTERN.search(plain_text)
        
        if has_tag:
            for comp in result.chain:
                if isinstance(comp, Comp.Plain) and comp.text:
                    temp = LLMResponse()
                    temp.completion_text = comp.text
                    await self._split_and_format_cot(temp, event)
                    comp.text = temp.completion_text

    # --- Helper Methods (恢复原版逻辑) ---

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

    def _is_cot_structure_incomplete(self, text: str) -> bool:
        if not text: return False
        has_start = self.cot_start_tag in text
        has_end = self.cot_end_tag in text
        has_final = self.FINAL_REPLY_PATTERN.search(text)
        is_complete = has_start and has_end and has_final
        if self.force_cot_structure: return not is_complete
        else: return not (has_start or has_final) and False or not is_complete

    async def _split_and_format_cot(self, response: LLMResponse, event: AstrMessageEvent):
        if not response or not response.completion_text: return
        text = response.completion_text
        thought, reply = "", text
        parts = self.FINAL_REPLY_PATTERN.split(text, 1)
        if len(parts) > 1:
            os_match = self.THOUGHT_TAG_PATTERN.search(parts[0])
            thought = os_match.group('content').strip() if os_match else parts[0].strip()
            reply = parts[1].strip()
        else:
            os_match = self.THOUGHT_TAG_PATTERN.search(text)
            if os_match:
                thought = os_match.group('content').strip()
                reply = self.THOUGHT_TAG_PATTERN.sub("", text).strip()
        
        if thought: await self._async_save_thought(event.unified_msg_origin, thought)
        for kw in self.filtered_keywords: reply = reply.replace(kw, "")
        if self.display_cot_text and thought: response.completion_text = f"🤔 罗莎思考中：\n{thought}\n\n---\n\n{reply}"
        else: response.completion_text = reply

    async def _periodic_cleanup_task(self):
        while True:
            try:
                await asyncio.sleep(300)
                self.pending_requests.clear()
            except Exception: break

    def _parse_status_codes(self, codes_str: str) -> set:
        return {int(line.strip()) for line in codes_str.split("\n") if line.strip().isdigit()}

    def _get_request_key(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "_retry_plugin_request_key"): 
            return event._retry_plugin_request_key
        trace_id = uuid.uuid4().hex[:8]
        key = f"{event.unified_msg_origin}_{trace_id}"
        event._retry_plugin_request_key = key
        return key

    def _should_retry_response(self, result) -> bool:
        if not result: return True
        text = getattr(result, "completion_text", "") or ""
        if not (text or "").strip(): return True
        for kw in self.error_keywords:
            if kw in text.lower(): return True
        if self._has_api_error_pattern(text): return True
        return False
    
    def _has_api_error_pattern(self, text: str) -> bool:
        """统一的 API 错误检测逻辑"""
        if not text: return False
        if "ROSA_INTERNAL_ERROR" in text: return True # Layer 0 信号
        is_astrbot_fail = "AstrBot" in text and "请求失败" in text
        if is_astrbot_fail: return True
        
        error_patterns = [
            r"Error\s*code:\s*5\d{2}", r"APITimeoutError", r"Request\s*timed\s*out",
            r"InternalServerError", r"count_token_failed", r"bad_response_status_code",
            r"connection\s*error", r"remote\s*disconnected", r"read\s*timeout", r"connect\s*timeout"
        ]
        combined_pattern = re.compile("|".join(error_patterns), re.IGNORECASE)
        return bool(combined_pattern.search(text))

    async def _fix_user_history(self, event: AstrMessageEvent, request_key: str, bot_reply: str = None):
        """[原版逻辑] 手动修复历史记录"""
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
                if bot_reply:
                    history_list.append({"role": "assistant", "content": bot_reply})

                await self.context.conversation_manager.update_conversation(
                    unified_msg_origin=umo, conversation_id=cid, history=history_list
                )
        except Exception as e:
            logger.error(f"手动补全历史记录时出错: {e}", exc_info=True)

    async def _perform_retry_with_stored_params(self, request_key: str) -> Optional[Any]:
        """[原版逻辑] 使用存储的参数进行重试"""
        if request_key not in self.pending_requests: return None
        stored = self.pending_requests[request_key]
        provider = self.context.get_using_provider()
        if not provider: return None
        try:
            # 这里的调用会再次经过 Layer 0 的 Patch，形成闭环保护
            kwargs = {k: stored.get(k) for k in ["prompt", "image_urls", "func_tool", "system_prompt"]}
            
            conversation_id = stored.get("conversation_id")
            unified_msg_origin = stored.get("unified_msg_origin")
            
            if conversation_id and unified_msg_origin:
                conv_mgr = getattr(self.context, "conversation_manager", None)
                if conv_mgr:
                    conversation = await conv_mgr.get_conversation(unified_msg_origin, conversation_id)
                    if conversation:
                        kwargs["conversation"] = conversation
                        if not hasattr(conversation, "metadata") or not conversation.metadata:
                            conversation.metadata = {}
                        conversation.metadata["sender"] = stored.get("sender", {})

            contexts = stored.get("contexts", [])
            if stored.get("prompt"):
                contexts.append({"role": "user", "content": stored["prompt"]})
            kwargs["contexts"] = contexts
            kwargs.update(stored.get("provider_params", {}))
            
            return await provider.text_chat(**kwargs)
            
        except Exception as e:
            logger.error(f"[IntelligentRetry] ⚠️ 重试尝试失败: {e}")
            return None

    async def _execute_retry_sequence(self, event: AstrMessageEvent, request_key: str) -> bool:
        """[原版逻辑] 执行重试循环"""
        delay = max(0, int(self.retry_delay))
        session_id = event.unified_msg_origin
        for attempt in range(1, self.max_attempts + 1):
            logger.warning(f"[IntelligentRetry] 🔄 (Session: {session_id}) 正在执行上层逻辑重试 {attempt}/{self.max_attempts}...")
            
            new_response = await self._perform_retry_with_stored_params(request_key)
            
            # 检查响应是否有效（不是 Layer 0 返回的错误信号）
            is_layer0_fail = hasattr(new_response, "completion_text") and "ROSA_INTERNAL_ERROR" in new_response.completion_text

            if new_response and getattr(new_response, "completion_text", "") and not is_layer0_fail:
                text = new_response.completion_text
                if not self._should_retry_response(new_response) and not self._is_cot_structure_incomplete(text):
                    logger.info(f"[IntelligentRetry] ✅ 第 {attempt} 次重试成功")
                    await self._fix_user_history(event, request_key, bot_reply=text)
                    await self._split_and_format_cot(new_response, event)
                    
                    final_res = MessageEventResult()
                    final_res.message(new_response.completion_text)
                    final_res.result_content_type = ResultContentType.LLM_RESULT
                    event.set_result(final_res)
                    return True
            
            if attempt < self.max_attempts: 
                await asyncio.sleep(delay)
        
        return False

    async def terminate(self):
        self._cleanup_task.cancel()
        self.pending_requests.clear()
        logger.info("[IntelligentRetry] 插件已卸载")

# --- END OF FILE main.py ---
