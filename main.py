# --- START OF FILE main.py ---

import asyncio
import json
import re
import time
import os
from typing import Dict, Any, Optional, List
from datetime import datetime
from pathlib import Path

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter as event_filter
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
    "集成了思维链(CoT)处理的智能重试插件。采用[Session热数据] + [每日全量归档] 混合存储架构。",
    "3.7.0-Rosa-Hybrid-Robust",
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
        self.THOUGHT_TAG_PATTERN = re.compile(
            f'{escaped_start}(?P<content>.*?){escaped_end}',
            re.DOTALL
        )
        
        self.display_cot_text = config.get("display_cot_text", False)
        self.filtered_keywords = config.get("filtered_keywords", ["呵呵，", "（……）"])
        
        # --- 总结配置 ---
        self.summary_provider_id = config.get("summary_provider_id", "")
        self.summary_max_retries = max(1, int(config.get("summary_max_retries", 2)))
        self.history_limit = int(config.get("history_limit", 100))
        self.summary_timeout = int(config.get("summary_timeout", 60))
        self.summary_prompt_template = config.get("summary_prompt_template", 
            "请阅读以下机器人的'内心独白(Inner Thought)'日志，用简练、客观的语言总结其核心思考逻辑、情绪状态以及最终的决策意图。\n\n日志内容：\n{log}")

        logger.info(f"[IntelligentRetry] 3.8.1 高清渲染版 (Robust) 已加载。")

    def _parse_config(self, config: AstrBotConfig) -> None:
        self.max_attempts = config.get("max_attempts", 3)
        self.retry_delay = config.get("retry_delay", 2)
        self.retry_delay_mode = config.get("retry_delay_mode", "exponential").lower().strip()
        
        default_keywords = "api 返回的内容为空\n调用失败\n[TRUNCATED_BY_LENGTH]"
        keywords_str = config.get("error_keywords", default_keywords)
        self.error_keywords = [k.strip().lower() for k in keywords_str.split("\n") if k.strip()]

        self.retryable_status_codes = self._parse_status_codes(config.get("retryable_status_codes", "400\n429\n502\n503\n504"))
        self.non_retryable_status_codes = self._parse_status_codes(config.get("non_retryable_status_codes", ""))
        self.fallback_reply = config.get("fallback_reply", "抱歉，服务波动，罗莎暂时无法回应。")
        
        self.enable_truncation_retry = config.get("enable_truncation_retry", False)
        self.force_cot_structure = config.get("force_cot_structure", True)
        self.enable_concurrent_retry = config.get("enable_concurrent_retry", False)
        self.concurrent_retry_threshold = max(0, int(config.get("concurrent_retry_threshold", 1)))
        self.concurrent_retry_count = max(1, min(int(config.get("concurrent_retry_count", 2)), 5))
        self.concurrent_retry_timeout = max(5, min(int(config.get("concurrent_retry_timeout", 30)), 300))
        self.truncation_detection_mode = config.get("truncation_detection_mode", "enhanced")

    # ======================= 渲染辅助方法 (HD Optimized) =======================

    async def _render_and_reply(self, event: AstrMessageEvent, title: str, subtitle: str, content: str):
        """通用图片渲染与发送逻辑"""
        try:
            render_data = {
                "title": title,
                "subtitle": subtitle,
                "content": content,
                "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            }
            
            render_options = {
                "device_scale_factor": 3, 
                "viewport": {"width": 640, "height": 1000}, 
                "full_page": True,
            }
            
            img_url = await self.html_render(LOG_TEMPLATE, render_data, options=render_options)
            
            if img_url:
                yield event.image_result(img_url)
            else:
                yield event.plain_result(f"【渲染失败】\n{title}\n{content}")
                
        except Exception as e:
            logger.error(f"[IntelligentRetry] 图片渲染异常: {e}")
            yield event.plain_result(f"【系统异常】\n{content}")

    # ======================= 存储层 (Hybrid) =======================

    async def _async_save_thought(self, session_id: str, content: str):
        if not session_id or not content: return
        
        def _write_impl():
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            date_str = now.strftime("%Y-%m-%d")
            
            # 1. 每日归档
            try:
                archive_filename = f"{date_str}_thought.log"
                archive_path = COLD_ARCHIVE_DIR / archive_filename
                with open(archive_path, 'a', encoding='utf-8') as f:
                    f.write(f"[{timestamp}] [Session: {session_id}]\n{content}\n{'-'*40}\n")
            except Exception: pass

            # 2. 热数据
            try:
                safe_name = sanitize_filename(session_id)
                json_path = HOT_STORAGE_DIR / f"{safe_name}.json"
                thoughts = []
                if json_path.exists():
                    try:
                        with open(json_path, 'r', encoding='utf-8') as f: thoughts = json.load(f)
                    except Exception: thoughts = []
                
                thoughts.insert(0, {"time": timestamp, "content": content})
                if len(thoughts) > self.history_limit: thoughts = thoughts[:self.history_limit]
                
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(thoughts, f, ensure_ascii=False, indent=2)
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
                entry = thoughts[target_idx]
                return str(entry.get('content', '')) if isinstance(entry, dict) else str(entry)
            except Exception: return None
        return await asyncio.to_thread(_read_impl)

    # ======================= 功能指令 =======================

    @event_filter.command("rosaos")
    async def get_rosaos_log(self, event: AstrMessageEvent, index: str = "1"):
        """获取内心OS (图片版)"""
        try:
            idx = int(index)
            if idx < 1: raise ValueError
        except ValueError:
            yield event.plain_result("❌ 索引必须是大于0的整数")
            return

        session_id = event.unified_msg_origin
        log_content = await self._async_read_thought(session_id, idx)
        
        if not log_content:
            yield event.plain_result(f"📭 在最近记录中未找到第 {idx} 条。")
        else:
            async for msg in self._render_and_reply(
                event, 
                title="罗莎内心记录", 
                subtitle=f"Log Index: {idx}", 
                content=log_content
            ):
                yield msg

    @event_filter.command("cogito")
    async def handle_cogito(self, event: AstrMessageEvent, index: str = "1"):
        """认知分析 (图片版)"""
        try:
            idx = int(index)
            if idx < 1: raise ValueError
        except ValueError:
            yield event.plain_result("❌ 请输入有效的数字索引")
            return

        session_id = event.unified_msg_origin
        log_content = await self._async_read_thought(session_id, idx)
        
        if not log_content:
            yield event.plain_result("📭 找不到该条日志，无法进行总结。")
            return
            
        target_provider_id = self.summary_provider_id
        if not target_provider_id:
            target_provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        
        if not target_provider_id:
            yield event.plain_result("❌ 无法获取模型 Provider。")
            return

        yield event.plain_result(f"🧠 分析中... (Index: {idx}, Timeout: {self.summary_timeout}s)")

        prompt = self.summary_prompt_template.replace("{log}", log_content)
        success = False
        final_summary = ""
        
        for attempt in range(self.summary_max_retries):
            try:
                resp = await asyncio.wait_for(
                    self.context.llm_generate(
                        chat_provider_id=target_provider_id,
                        prompt=prompt
                    ),
                    timeout=self.summary_timeout
                )
                if resp and resp.completion_text:
                    final_summary = resp.completion_text
                    success = True
                    break
                await asyncio.sleep(1)
            except (asyncio.TimeoutError, Exception): pass

        if success:
            async for msg in self._render_and_reply(
                event,
                title="COGITO 认知分析报告",
                subtitle=f"AI Analysis | Index {idx}",
                content=final_summary
            ):
                yield msg
        else:
            yield event.plain_result("⚠️ 分析服务暂时不可用 (Timeout)。")

    # ======================= 核心重试逻辑 =======================

    @event_filter.on_llm_request(priority=70)
    async def store_llm_request(self, event: AstrMessageEvent, req):
        if not hasattr(req, "prompt") or not hasattr(req, "contexts"): return
        
        msg_text = (event.message_str or "").strip().lower()
        if msg_text.startswith(("/cogito", "/rosaos", "reset", "new")): return

        request_key = self._get_request_key(event)
        image_urls = [c.url for c in event.message_obj.message if isinstance(c, Comp.Image) and c.url]

        stored_params = {
            "prompt": req.prompt,
            "contexts": getattr(req, "contexts", []),
            "image_urls": image_urls,
            "system_prompt": getattr(req, "system_prompt", ""),
            "func_tool": getattr(req, "func_tool", None),
            "unified_msg_origin": event.unified_msg_origin,
            "conversation": getattr(req, "conversation", None),
            "timestamp": time.time(),
            "sender": {
                "user_id": getattr(event.message_obj, "user_id", None),
                "nickname": getattr(event.message_obj, "nickname", None),
                "group_id": getattr(event.message_obj, "group_id", None),
                "platform": getattr(event.message_obj, "platform", None),
            },
            "provider_params": {}
        }
        
        for param in ["model", "temperature", "max_tokens", "top_p", "top_k", "stop", "stream"]:
            if hasattr(req, param):
                stored_params["provider_params"][param] = getattr(req, param, None)
        
        self.pending_requests[request_key] = stored_params

    @event_filter.on_llm_response(priority=5)
    async def process_and_retry_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        # 1. 优先执行 CoT 裁剪 (Robust Fix: 即使 Key 丢失也要裁剪)
        # --------------------------------------------------------------------------------
        if resp and hasattr(resp, "completion_text") and self.cot_start_tag in (resp.completion_text or ""):
            logger.debug(f"[IntelligentRetry] 检测到 CoT 标签，执行无条件裁剪 (Key Check Bypass)")
            await self._split_and_format_cot(resp, event)

        if self.max_attempts <= 0 or not hasattr(resp, "completion_text"): return
        if getattr(resp, "raw_completion", None):
            choices = getattr(resp.raw_completion, "choices", [])
            if choices and getattr(choices[0], "finish_reason", None) == "tool_calls": return

        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests: return

        text = resp.completion_text or ""
        is_trunc = self.enable_truncation_retry and self._is_truncated(resp)
        
        # 检查是否需要重试
        if not text.strip() or self._should_retry_response(resp) or is_trunc or self._is_cot_structure_incomplete(text):
            logger.info(f"[IntelligentRetry] 触发重试 (Key: {request_key})")
            if await self._execute_retry_sequence(event, request_key):
                res = event.get_result()
                resp.completion_text = res.get_plain_text() if res else ""
            else:
                if self.fallback_reply: resp.completion_text = self.fallback_reply
        
        # 清理 Key
        self.pending_requests.pop(request_key, None)

    @event_filter.on_decorating_result(priority=5)
    async def final_cot_stripper(self, event: AstrMessageEvent):
        """
        最后一道防线：装饰阶段再次检查文本链中是否残留了 CoT
        """
        result = event.get_result()
        if not result or not result.chain: return
        
        plain_text = result.get_plain_text()
        has_tag = self.cot_start_tag in plain_text or self.FINAL_REPLY_PATTERN.search(plain_text)
        
        if has_tag:
            logger.debug("[IntelligentRetry] 装饰阶段发现残留 CoT，执行强制清理")
            for comp in result.chain:
                if isinstance(comp, Comp.Text) and comp.text:
                    temp = LLMResponse()
                    temp.completion_text = comp.text
                    await self._split_and_format_cot(temp, event)
                    comp.text = temp.completion_text

    # --- Helper Methods ---

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
        
        if thought:
            await self._async_save_thought(event.unified_msg_origin, thought)
            
        for kw in self.filtered_keywords: reply = reply.replace(kw, "")
        if self.display_cot_text and thought:
            response.completion_text = f"🤔 罗莎思考中：\n{thought}\n\n---\n\n{reply}"
        else:
            response.completion_text = reply

    async def _periodic_cleanup_task(self):
        while True:
            try:
                await asyncio.sleep(300)
                current = time.time()
                expired = [k for k, v in self.pending_requests.items() if current - v.get("timestamp", 0) > 300]
                for k in expired: del self.pending_requests[k]
            except (asyncio.CancelledError, Exception): break

    def _parse_status_codes(self, codes_str: str) -> set:
        return {int(line.strip()) for line in codes_str.split("\n") if line.strip().isdigit()}

    def _get_request_key(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "_retry_plugin_request_key"): return event._retry_plugin_request_key
        message_id = getattr(event.message_obj, "message_id", "no_id")
        timestamp = getattr(event.message_obj, "timestamp", datetime.now().timestamp())
        key = f"{event.unified_msg_origin}_{message_id}_{timestamp}"
        event._retry_plugin_request_key = key
        return key

    def _is_truncated(self, text_or_response) -> bool:
        text = text_or_response.completion_text if hasattr(text_or_response, "completion_text") else text_or_response
        if hasattr(text_or_response, "completion_text") and "[TRUNCATED_BY_LENGTH]" in (text or ""): return True
        return False

    def _should_retry_response(self, result) -> bool:
        if not result: return True
        text = result.completion_text if hasattr(result, "completion_text") else result.get_plain_text()
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
            return await provider.text_chat(**kwargs)
        except Exception as e:
            logger.error(f"重试异常: {e}")
            return None

    async def _execute_retry_sequence(self, event: AstrMessageEvent, request_key: str) -> bool:
        delay = max(0, int(self.retry_delay))
        for attempt in range(1, self.max_attempts + 1):
            new_response = await self._perform_retry_with_stored_params(request_key)
            if new_response and getattr(new_response, "completion_text", ""):
                if not self._should_retry_response(new_response) and not self._is_cot_structure_incomplete(new_response.completion_text):
                    await self._split_and_format_cot(new_response, event)
                    from astrbot.api.event import MessageEventResult, ResultContentType
                    final_res = MessageEventResult()
                    final_res.message(new_response.completion_text)
                    final_res.result_content_type = ResultContentType.LLM_RESULT
                    event.set_result(final_res)
                    return True
            if attempt < self.max_attempts: await asyncio.sleep(delay)
        return False

    async def terminate(self):
        self._cleanup_task.cancel()
        self.pending_requests.clear()
        logger.info("[IntelligentRetry] 插件已卸载")

# --- END OF FILE main.py ---
