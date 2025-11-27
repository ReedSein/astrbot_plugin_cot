import asyncio
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

# --- HTML 渲染模板 (IMAX HD Version) ---
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
    "集成了思维链(CoT)处理的智能重试插件。v3.8.14 异常拦截终极版 (Interceptor Pro)，专治超时与网络波动。",
    "3.8.14-Rosa-Interceptor-Pro",
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

        logger.info(f"[IntelligentRetry] 3.8.14 异常拦截终极版已加载。")

    def _parse_config(self, config: AstrBotConfig) -> None:
        self.max_attempts = config.get("max_attempts", 3)
        self.retry_delay = config.get("retry_delay", 2)
        
        # [Config] 扩充异常检测词库
        default_keywords = "api 返回的内容为空\n调用失败\n[TRUNCATED_BY_LENGTH]\nupstream error\ndo_request_failed\ninternal server error\n500\n429\nrate limit\ntimeout\ntimed out"
        keywords_str = config.get("error_keywords", default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split("\n") if k.strip()]

        self.retryable_status_codes = self._parse_status_codes(config.get("retryable_status_codes", "400\n429\n502\n503\n504"))
        self.non_retryable_status_codes = self._parse_status_codes(config.get("non_retryable_status_codes", ""))
        self.fallback_reply = config.get("fallback_reply", "抱歉，服务波动，罗莎暂时无法回应。")
        self.enable_truncation_retry = config.get("enable_truncation_retry", False)
        self.force_cot_structure = config.get("force_cot_structure", True)

    # ======================= 渲染辅助 =======================
    async def _render_and_reply(self, event: AstrMessageEvent, title: str, subtitle: str, content: str):
        try:
            render_data = {"title": title, "subtitle": subtitle, "content": content, "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")}
            img_url = await self.html_render(LOG_TEMPLATE, render_data, options={"viewport": {"width": 640, "height": 800}, "full_page": True})
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
                return str(thoughts[target_idx].get('content', ''))
            except Exception: return None
        return await asyncio.to_thread(_read_impl)

    # ======================= 功能指令 =======================
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

    # ======================= 核心重试逻辑 =======================

    @event_filter.on_llm_request(priority=70)
    async def store_llm_request(self, event: AstrMessageEvent, req):
        """记录请求上下文"""
        if not hasattr(req, "prompt"): return
        if (event.message_str or "").strip().startswith(("/cogito", "/rosaos", "reset", "new")): return

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
            "contexts": getattr(req, "contexts", []),
            "image_urls": image_urls,
            "system_prompt": getattr(req, "system_prompt", ""),
            "func_tool": getattr(req, "func_tool", None),
            "unified_msg_origin": event.unified_msg_origin,
            "conversation": getattr(req, "conversation", None),
            "timestamp": time.time(),
            "sender": sender_info,
            "provider_params": {k: getattr(req, k, None) for k in ["model", "temperature", "max_tokens"] if hasattr(req, k)}
        }
        self.pending_requests[request_key] = stored_params

    @event_filter.on_llm_response(priority=5)
    async def process_and_retry_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        # 1. 优先执行 CoT 裁剪
        if resp and hasattr(resp, "completion_text") and self.cot_start_tag in (resp.completion_text or ""):
            await self._split_and_format_cot(resp, event)

        # 如果响应直接是空的或者带有错误标记，也视为需要重试
        if getattr(resp, "raw_completion", None):
            choices = getattr(resp.raw_completion, "choices", [])
            if choices and getattr(choices[0], "finish_reason", None) == "tool_calls": return

        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests: return

        text = getattr(resp, "completion_text", "") or ""
        is_trunc = self.enable_truncation_retry and self._is_truncated(resp)
        
        # [Check] 检查原始响应是否包含报错
        raw_str = str(getattr(resp, "raw_completion", "")).lower()
        is_error = "error" in raw_str and ("upstream" in raw_str or "500" in raw_str)

        needs_retry = not text.strip() or self._should_retry_response(resp) or is_trunc or self._is_cot_structure_incomplete(text) or is_error
        
        if needs_retry:
            logger.info(f"[IntelligentRetry] 🔴 触发重试逻辑 (Key: {request_key})")
            success = await self._execute_retry_sequence(event, request_key)
            if success:
                res = event.get_result()
                resp.completion_text = res.get_plain_text() if res else ""
            else:
                if self.fallback_reply:
                    self._apply_fallback(event)
                    # 尝试同步更新 resp 以防万一
                    resp.completion_text = self.fallback_reply
        
        self.pending_requests.pop(request_key, None)

    @event_filter.on_decorating_result(priority=20)
    async def intercept_api_error(self, event: AstrMessageEvent):
        """
        [NEW] 异常拦截层 (Priority=20)
        捕获 Core 因 Provider 抛出异常 (Timeout, 500, 429) 而生成的报错消息。
        这些异常会导致 on_llm_response 被跳过，必须在这里拦截。
        """
        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests: return

        result = event.get_result()
        if not result: return
        
        text = result.get_plain_text() or ""
        text_lower = text.lower()

        # --- 核心修复：更广泛的异常特征库 ---
        core_error_signatures = [
            "astrbot 请求失败",
            "error code:",
            "internalservererror",
            "ratelimiterror",
            "timeout",         # 捕获各类超时
            "timed out",
            "connection error",
            "connection refused",
            "max retries",
            "remote disconnected",
            "socket error",
            "ssl error"
        ]

        # 同时匹配 硬编码特征 和 用户配置的关键词
        is_core_signature = any(sig in text_lower for sig in core_error_signatures)
        is_config_keyword = any(kw in text_lower for kw in self.error_keywords)
        
        is_core_error = is_core_signature or is_config_keyword
        
        if is_core_error:
            logger.warning(f"[IntelligentRetry] 🛡️ 拦截到 Core 异常消息 (Key: {request_key}) | 内容片段: {text[:50]}...")
            
            # 启动重试
            success = await self._execute_retry_sequence(event, request_key)
            
            if success:
                logger.info(f"[IntelligentRetry] 🛡️ 异常拦截重试成功！")
            else:
                # 重试失败，强制应用兜底
                if self.fallback_reply:
                    self._apply_fallback(event)
            
            # 清理 Key，避免重复处理
            self.pending_requests.pop(request_key, None)

    @event_filter.on_decorating_result(priority=5)
    async def final_cot_stripper(self, event: AstrMessageEvent):
        """最后一道防线"""
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

    # --- Helper Methods ---

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
        if not text and hasattr(result, "get_plain_text"): text = result.get_plain_text()
        if not (text or "").strip(): return True
        for kw in self.error_keywords:
            if kw in text.lower(): return True
        return False

    async def _perform_retry_with_stored_params(self, request_key: str) -> Optional[Any]:
        if request_key not in self.pending_requests: return None
        stored = self.pending_requests[request_key]
        provider = self.context.get_using_provider()
        if not provider: return None
        try:
            kwargs = {k: stored.get(k) for k in ["prompt", "image_urls", "func_tool", "system_prompt"]}
            if stored.get("conversation"):
                kwargs["conversation"] = stored["conversation"]
                if not hasattr(kwargs["conversation"], "metadata") or not kwargs["conversation"].metadata:
                    kwargs["conversation"].metadata = {}
                kwargs["conversation"].metadata["sender"] = stored.get("sender", {})
            else: kwargs["contexts"] = stored.get("contexts", [])
            kwargs.update(stored.get("provider_params", {}))
            
            # --- 核心修复：防御性调用 ---
            # 这里的调用可能会再次因为网络问题抛出异常
            # 我们必须捕获它，否则会中断重试循环，导致插件直接报错退出
            return await provider.text_chat(**kwargs)
            
        except Exception as e:
            logger.error(f"[IntelligentRetry] ⚠️ 重试尝试失败 (Provider API 抛出异常): {e}")
            # 返回 None，告知上层本次重试失败，继续下一次循环或进入兜底
            return None

    async def _execute_retry_sequence(self, event: AstrMessageEvent, request_key: str) -> bool:
        """执行重试循环"""
        delay = max(0, int(self.retry_delay))
        session_id = event.unified_msg_origin
        for attempt in range(1, self.max_attempts + 1):
            logger.warning(f"[IntelligentRetry] 🔄 (Session: {session_id}) 检测到异常/超时，正在重试 {attempt}/{self.max_attempts}...")
            
            # 这里调用 _perform_retry_with_stored_params 现在是安全的
            new_response = await self._perform_retry_with_stored_params(request_key)
            
            if new_response and getattr(new_response, "completion_text", ""):
                text = new_response.completion_text
                if not self._should_retry_response(new_response) and not self._is_cot_structure_incomplete(text):
                    logger.info(f"[IntelligentRetry] ✅ 第 {attempt} 次重试成功")
                    await self._split_and_format_cot(new_response, event)
                    final_res = MessageEventResult()
                    final_res.message(new_response.completion_text)
                    final_res.result_content_type = ResultContentType.LLM_RESULT
                    event.set_result(final_res)
                    return True
            
            # 如果不是最后一次尝试，则等待
            if attempt < self.max_attempts: 
                await asyncio.sleep(delay)
        
        return False

    async def terminate(self):
        self._cleanup_task.cancel()
        self.pending_requests.clear()
        logger.info("[IntelligentRetry] 插件已卸载")

# --- END OF FILE main.py ---
