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
# 别名导入，防止与 python 内置 filter 冲突
from astrbot.api.event import AstrMessageEvent, filter as event_filter
from astrbot.api.provider import LLMResponse

# --- 存储架构配置 ---
# 1. 热数据 (Hot): 存放在 data/cot_os_logs/sessions/
#    格式: session_id.json (只存最近 N 条，供插件回溯)
HOT_STORAGE_DIR = Path("data/cot_os_logs/sessions")

# 2. 冷归档 (Cold): 存放在 data/cot_os_logs/daily_archive/
#    格式: YYYY-MM-DD_thought.log (每日全量汇总，永久保存)
COLD_ARCHIVE_DIR = Path("data/cot_os_logs/daily_archive")

# 自动创建目录
HOT_STORAGE_DIR.mkdir(parents=True, exist_ok=True)
COLD_ARCHIVE_DIR.mkdir(parents=True, exist_ok=True)

def sanitize_filename(session_id: str) -> str:
    """清洗文件名"""
    return re.sub(r'[:\\/\*?"<>|]', '_', session_id)

@register(
    "intelligent_retry_with_cot",
    "木有知 & 长安某 & AstrBot Architect",
    "集成了思维链(CoT)处理的智能重试插件。采用[Session热数据] + [每日全量归档] 混合存储架构。",
    "3.6.0-Rosa-Hybrid",
)
class IntelligentRetryWithCoT(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task())
        
        self._parse_config(config)
        
        # --- 罗莎正则 ---
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
        self.history_limit = int(config.get("history_limit", 100)) # 热数据保留条数
        self.summary_prompt_template = config.get("summary_prompt_template", 
            "请阅读以下机器人的'内心独白(Inner Thought)'日志，用简练、客观的语言总结其核心思考逻辑、情绪状态以及最终的决策意图。\n\n日志内容：\n{log}")

        logger.info(f"[IntelligentRetry] 混合存储版已加载。\n"
                    f"热数据: {HOT_STORAGE_DIR}/*.json\n"
                    f"日归档: {COLD_ARCHIVE_DIR}/YYYY-MM-DD_thought.log")

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

    # ======================= 混合存储层 (Hybrid Storage) =======================

    async def _async_save_thought(self, session_id: str, content: str):
        """
        执行双写操作：
        1. 追加到每日汇总日志 (按日期分割)。
        2. 更新会话专属 JSON (按会话分割，滚动窗口)。
        """
        if not session_id or not content: return
        
        def _write_impl():
            now = datetime.now()
            timestamp = now.strftime("%Y-%m-%d %H:%M:%S")
            date_str = now.strftime("%Y-%m-%d")
            
            # --- 1. 每日全量归档 (User Preference) ---
            # 文件名: 2025-11-21_thought.log
            # 包含所有 Session 的日志，按时间顺序排列
            try:
                archive_filename = f"{date_str}_thought.log"
                archive_path = COLD_ARCHIVE_DIR / archive_filename
                
                with open(archive_path, 'a', encoding='utf-8') as f:
                    # 格式：[时间] [会话ID] 内容
                    log_entry = (
                        f"[{timestamp}] [Session: {session_id}]\n"
                        f"{content}\n"
                        f"{'-'*40}\n"
                    )
                    f.write(log_entry)
            except Exception as e:
                logger.error(f"[IntelligentRetry] 写入每日归档失败: {e}")

            # --- 2. 热数据更新 (Plugin Logic) ---
            # 文件名: session_id.json
            # 仅包含当前 Session，用于 /cogito 和 /rosaos
            try:
                safe_name = sanitize_filename(session_id)
                json_path = HOT_STORAGE_DIR / f"{safe_name}.json"
                thoughts = []
                
                if json_path.exists():
                    try:
                        with open(json_path, 'r', encoding='utf-8') as f:
                            thoughts = json.load(f)
                    except Exception:
                        thoughts = []
                
                entry = {
                    "time": timestamp,
                    "content": content
                }
                thoughts.insert(0, entry) # 最新在最前
                
                # 维持热数据大小
                if len(thoughts) > self.history_limit:
                    thoughts = thoughts[:self.history_limit]
                
                with open(json_path, 'w', encoding='utf-8') as f:
                    json.dump(thoughts, f, ensure_ascii=False, indent=2)
                    
            except Exception as e:
                logger.error(f"[IntelligentRetry] 更新热数据JSON失败: {e}")

        await asyncio.to_thread(_write_impl)

    async def _async_read_thought(self, session_id: str, index: int) -> Optional[str]:
        """从热数据读取日志"""
        def _read_impl():
            try:
                safe_name = sanitize_filename(session_id)
                json_path = HOT_STORAGE_DIR / f"{safe_name}.json"
                
                if not json_path.exists():
                    return None
                
                with open(json_path, 'r', encoding='utf-8') as f:
                    thoughts = json.load(f)
                
                target_idx = index - 1
                if target_idx < 0 or target_idx >= len(thoughts):
                    return None
                
                entry = thoughts[target_idx]
                if isinstance(entry, dict):
                    return f"[{entry.get('time', 'Unknown')}] {entry.get('content', '')}"
                return str(entry)
                
            except Exception as e:
                logger.error(f"[IntelligentRetry] 读取日志失败: {e}")
                return None
        
        return await asyncio.to_thread(_read_impl)

    # ======================= 功能指令 =======================

    @event_filter.command("rosaos")
    async def get_rosaos_log(self, event: AstrMessageEvent, index: str = "1"):
        """获取内心OS"""
        try:
            idx = int(index)
            if idx < 1: raise ValueError
        except ValueError:
            yield event.plain_result("❌ 索引必须是大于0的整数")
            return

        session_id = event.unified_msg_origin
        log_content = await self._async_read_thought(session_id, idx)
        
        if not log_content:
            yield event.plain_result(
                f"📭 在最近的 {self.history_limit} 条热数据中未找到记录。\n"
                f"请查阅服务器每日归档: data/cot_os_logs/daily_archive/"
            )
        else:
            yield event.plain_result(f"📔 **罗莎内心OS (倒数第 {idx} 条)**:\n\n{log_content}")

    @event_filter.command("cogito")
    async def handle_cogito(self, event: AstrMessageEvent, index: str = "1"):
        """认知分析"""
        try:
            idx = int(index)
            if idx < 1: raise ValueError
        except ValueError:
            yield event.plain_result("❌ 请输入有效的数字索引，例如 /cogito 1")
            return

        session_id = event.unified_msg_origin
        log_content = await self._async_read_thought(session_id, idx)
        
        if not log_content:
            yield event.plain_result("📭 找不到该条日志(热数据已过期)，无法进行总结。")
            return
            
        target_provider_id = self.summary_provider_id
        if not target_provider_id:
            target_provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        
        if not target_provider_id:
            yield event.plain_result("❌ 无法获取模型 Provider。")
            return

        yield event.plain_result(f"🧠 正在分析第 {idx} 条心路历程...")

        prompt = self.summary_prompt_template.replace("{log}", log_content)
        success = False
        final_summary = ""
        
        for attempt in range(self.summary_max_retries):
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=target_provider_id,
                    prompt=prompt
                )
                if resp and resp.completion_text:
                    final_summary = resp.completion_text
                    success = True
                    break
                await asyncio.sleep(1)
            except Exception as e:
                logger.warning(f"[Cogito] 总结失败: {e}")

        if success:
            yield event.plain_result(f"📝 **认知分析报告**:\n\n{final_summary}")
        else:
            yield event.plain_result("❌ 分析失败。")

    # ======================= 重试拦截逻辑 =======================

    @event_filter.on_llm_request(priority=70)
    async def store_llm_request(self, event: AstrMessageEvent, req):
        """捕获请求 (含白名单)"""
        if not hasattr(req, "prompt") or not hasattr(req, "contexts"): return
        
        msg_text = (event.message_str or "").strip().lower()
        if msg_text.startswith(("/cogito", "/rosaos", "reset", "new")):
            return

        request_key = self._get_request_key(event)
        image_urls = [
            comp.url for comp in event.message_obj.message
            if isinstance(comp, Comp.Image) and hasattr(comp, "url") and comp.url
        ]

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
        """处理响应"""
        if self.max_attempts <= 0 or not hasattr(resp, "completion_text"): return
        if getattr(resp, "raw_completion", None):
            choices = getattr(resp.raw_completion, "choices", [])
            if choices and getattr(choices[0], "finish_reason", None) == "tool_calls": return

        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests: return

        text = resp.completion_text or ""
        is_trunc = self.enable_truncation_retry and self._is_truncated(resp)
        
        if not text.strip() or self._should_retry_response(resp) or is_trunc or self._is_cot_structure_incomplete(text):
            logger.info(f"[IntelligentRetry] 触发重试 (Key: {request_key})")
            if await self._execute_retry_sequence(event, request_key):
                res = event.get_result()
                resp.completion_text = res.get_plain_text() if res else ""
            else:
                if self.fallback_reply: resp.completion_text = self.fallback_reply
        
        await self._split_and_format_cot(resp, event)
        self.pending_requests.pop(request_key, None)

    @event_filter.on_decorating_result(priority=5)
    async def final_cot_stripper(self, event: AstrMessageEvent):
        """最终兜底"""
        result = event.get_result()
        if not result or not result.chain: return
        plain_text = result.get_plain_text()
        
        has_tag = self.cot_start_tag in plain_text or self.FINAL_REPLY_PATTERN.search(plain_text)
        if has_tag:
            for comp in result.chain:
                if isinstance(comp, Comp.Text) and comp.text:
                    temp = LLMResponse()
                    temp.completion_text = comp.text
                    await self._split_and_format_cot(temp, event)
                    comp.text = temp.completion_text

    # --- CoT 处理 ---

    def _is_cot_structure_incomplete(self, text: str) -> bool:
        if not text: return False
        has_start = self.cot_start_tag in text
        has_end = self.cot_end_tag in text
        has_final = self.FINAL_REPLY_PATTERN.search(text)
        is_complete = has_start and has_end and has_final
        if self.force_cot_structure: return not is_complete
        else:
            if not has_start and not has_final: return False
            return not is_complete

    async def _split_and_format_cot(self, response: LLMResponse, event: AstrMessageEvent):
        if not response or not response.completion_text: return
        text = response.completion_text
        thought = ""
        reply = text
        
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
            session_id = event.unified_msg_origin
            await self._async_save_thought(session_id, thought)
            
        for kw in self.filtered_keywords: 
            reply = reply.replace(kw, "")
            
        if self.display_cot_text and thought:
            response.completion_text = f"🤔 罗莎思考中：\n{thought}\n\n---\n\n{reply}"
        else:
            response.completion_text = reply

    # --- 辅助函数 ---
    
    async def _periodic_cleanup_task(self):
        while True:
            try:
                await asyncio.sleep(300)
                current_time = time.time()
                expired = [k for k, v in self.pending_requests.items() if current_time - v.get("timestamp", 0) > 300]
                for k in expired: del self.pending_requests[k]
            except asyncio.CancelledError: break
            except Exception: pass

    def _parse_status_codes(self, codes_str: str) -> set:
        return {int(line.strip()) for line in codes_str.split("\n") if line.strip().isdigit()}

    def _get_request_key(self, event: AstrMessageEvent) -> str:
        if hasattr(event, "_retry_plugin_request_key"): return event._retry_plugin_request_key
        message_id = getattr(event.message_obj, "message_id", "no_id")
        timestamp = getattr(event.message_obj, "timestamp", datetime.now().timestamp())
        session_info = event.unified_msg_origin
        key = f"{session_info}_{message_id}_{timestamp}"
        event._retry_plugin_request_key = key
        return key

    def _is_truncated(self, text_or_response) -> bool:
        if hasattr(text_or_response, "completion_text"):
            text = text_or_response.completion_text or ""
            if "[TRUNCATED_BY_LENGTH]" in text: return True
        else:
            text = text_or_response
        if not text or len(text) < 5: return False
        return False

    def _should_retry_response(self, result) -> bool:
        if not result: return True
        text = ""
        if hasattr(result, "completion_text"): text = result.completion_text or ""
        elif hasattr(result, "get_plain_text"): text = result.get_plain_text()
        if not text.strip(): return True
        text_lower = text.lower()
        for kw in self.error_keywords:
            if kw in text_lower: return True
        return False

    async def _perform_retry_with_stored_params(self, request_key: str) -> Optional[Any]:
        if request_key not in self.pending_requests: return None
        stored = self.pending_requests[request_key]
        provider = self.context.get_using_provider()
        if not provider: return None
        try:
            kwargs = {
                "prompt": stored["prompt"],
                "image_urls": stored["image_urls"],
                "func_tool": stored["func_tool"],
            }
            system_prompt = stored.get("system_prompt")
            conversation = stored.get("conversation")
            if conversation and conversation.persona_id:
                pm = getattr(self.context, "persona_manager", None)
                if pm:
                    persona = await pm.get_persona(conversation.persona_id)
                    if persona and persona.system_prompt: system_prompt = persona.system_prompt
            if system_prompt: kwargs["system_prompt"] = system_prompt
            if conversation:
                kwargs["conversation"] = conversation
                if not hasattr(conversation, "metadata") or not conversation.metadata: conversation.metadata = {}
                conversation.metadata["sender"] = stored.get("sender", {})
            else: kwargs["contexts"] = stored.get("contexts", [])
            kwargs.update(stored.get("provider_params", {}))
            return await provider.text_chat(**kwargs)
        except Exception as e:
            logger.error(f"重试异常: {e}")
            return None

    async def _execute_retry_sequence(self, event: AstrMessageEvent, request_key: str) -> bool:
        delay = max(0, int(self.retry_delay))
        attempts = self.max_attempts
        for attempt in range(1, attempts + 1):
            new_response = await self._perform_retry_with_stored_params(request_key)
            if new_response and getattr(new_response, "completion_text", ""):
                if not self._should_retry_response(new_response) and not self._is_cot_structure_incomplete(new_response.completion_text):
                    await self._split_and_format_cot(new_response, event)
                    from astrbot.api.event import MessageEventResult, ResultContentType
                    result = MessageEventResult()
                    result.message(new_response.completion_text)
                    result.result_content_type = ResultContentType.LLM_RESULT
                    event.set_result(result)
                    return True
            if attempt < attempts: await asyncio.sleep(delay)
        return False

    async def terminate(self):
        self._cleanup_task.cancel()
        self.pending_requests.clear()
        logger.info("[IntelligentRetry] 插件已卸载")

# --- END OF FILE main.py ---
