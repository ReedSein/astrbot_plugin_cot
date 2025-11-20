# --- START OF FILE main.py ---

import asyncio
import json
import re
import time
import os
from typing import Dict, Any, Optional, List
from datetime import datetime

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register
# 【关键修复】将 filter 重命名为 event_filter，避免覆盖 Python 内置的 filter 函数
from astrbot.api.event import AstrMessageEvent, filter as event_filter
from astrbot.api.provider import LLMResponse

# 独立的 Logger 标记
LOG_DIR = "logs"

@register(
    "Rosaintelligent_retry_with_cot",
    "ReedSein",
    "集成了思维链(CoT)处理的智能重试插件。专为罗莎人格打造，内置 Cogito 认知总结系统。",
    "3.3.0-Rosa-Cogito-Fix",
)
class IntelligentRetryWithCoT(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        
        # --- 1. 内存管理：后台清理任务 ---
        self._cleanup_task = asyncio.create_task(self._periodic_cleanup_task())
        
        self._parse_config(config)
        
        # --- 2. 罗莎核心配置 (Hardcoded for stability) ---
        self.cot_start_tag = config.get("cot_start_tag", "<罗莎内心OS>")
        self.cot_end_tag = config.get("cot_end_tag", "</罗莎内心OS>")
        self.final_reply_pattern_str = config.get("final_reply_pattern", r"最终的罗莎回复[:：]?\s*")
        
        # 预编译正则，提升性能
        self.FINAL_REPLY_PATTERN = re.compile(self.final_reply_pattern_str, re.IGNORECASE)
        escaped_start = re.escape(self.cot_start_tag)
        escaped_end = re.escape(self.cot_end_tag)
        # DOTALL 模式确保能匹配包含换行符的内容
        self.THOUGHT_TAG_PATTERN = re.compile(
            f'{escaped_start}(?P<content>.*?){escaped_end}',
            re.DOTALL
        )
        
        self.display_cot_text = config.get("display_cot_text", False)
        self.filtered_keywords = config.get("filtered_keywords", ["呵呵，", "（……）"])
        
        # --- 3. 总结功能 (Cogito) 配置 ---
        self.summary_provider_id = config.get("summary_provider_id", "")
        self.summary_max_retries = max(0, int(config.get("summary_max_retries", 2)))
        self.summary_prompt_template = config.get("summary_prompt_template", 
            "请阅读以下机器人的'内心独白(Inner Thought)'日志，用简练、客观的语言总结其核心思考逻辑、情绪状态以及最终的决策意图。\n\n日志内容：\n{log}")

        logger.info(f"[IntelligentRetry] 罗莎 Cogito 修复版已加载。")

    def _parse_config(self, config: AstrBotConfig) -> None:
        """解析配置"""
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
        
        # 并发配置
        self.enable_concurrent_retry = config.get("enable_concurrent_retry", False)
        self.concurrent_retry_threshold = max(0, int(config.get("concurrent_retry_threshold", 1)))
        self.concurrent_retry_count = max(1, min(int(config.get("concurrent_retry_count", 2)), 5))
        self.concurrent_retry_timeout = max(5, min(int(config.get("concurrent_retry_timeout", 30)), 300))
        self.truncation_detection_mode = config.get("truncation_detection_mode", "enhanced")

    # ======================= Cogito 认知总结模块 =======================

    @event_filter.command("rosaos")
    async def get_rosaos_log(self, event: AstrMessageEvent, index: str = "1"):
        """
        获取原始日志。
        /rosaos 1 -> 最新一条
        """
        try:
            idx = int(index)
            if idx < 1:
                yield event.plain_result("❌ 索引必须大于 0")
                return
        except ValueError:
            yield event.plain_result(f"❌ 无效的数字: {index}")
            return

        log_content = await self._read_thought_log(idx)
        if not log_content:
            yield event.plain_result("📭 未找到对应的日志记录，今天可能还没说过话。")
        else:
            yield event.plain_result(f"📔 **罗莎内心OS (Index {idx})**:\n\n{log_content}")

    @event_filter.command("cogito")
    async def handle_cogito(self, event: AstrMessageEvent, index: str = "1"):
        """
        调用小型LLM总结指定日志。
        """
        try:
            idx = int(index)
            if idx < 1: raise ValueError
        except ValueError:
            yield event.plain_result("❌ 请输入有效的数字索引，例如 /cogito 1")
            return

        # 1. 读取日志
        log_content = await self._read_thought_log(idx)
        if not log_content:
            yield event.plain_result("📭 找不到该条日志，无法进行总结。")
            return
            
        # 2. 确定 Provider
        target_provider_id = self.summary_provider_id
        if not target_provider_id:
            target_provider_id = await self.context.get_current_chat_provider_id(event.unified_msg_origin)
        
        if not target_provider_id:
            yield event.plain_result("❌ 无法获取可用的模型 Provider，请检查配置。")
            return

        yield event.plain_result(f"🧠 正在调用模型 ({target_provider_id}) 回顾第 {idx} 条心路历程...")

        # 3. 构建 Prompt
        prompt = self.summary_prompt_template.replace("{log}", log_content)
        
        # 4. 执行简单重试逻辑
        success = False
        final_summary = ""
        retry_count = max(1, self.summary_max_retries)
        
        for attempt in range(retry_count):
            try:
                resp = await self.context.llm_generate(
                    chat_provider_id=target_provider_id,
                    prompt=prompt
                )
                
                if resp and resp.completion_text:
                    final_summary = resp.completion_text
                    success = True
                    break
                else:
                    logger.warning(f"[Cogito] 第 {attempt+1} 次总结尝试返回为空。")
            except Exception as e:
                logger.warning(f"[Cogito] 第 {attempt+1} 次总结尝试异常: {e}")
                if attempt < retry_count - 1:
                    await asyncio.sleep(1)

        # 5. 输出结果
        if success:
            yield event.plain_result(f"📝 **认知分析报告**:\n\n{final_summary}")
        else:
            yield event.plain_result(f"❌ 认知分析失败 (重试了 {retry_count} 次)，请检查模型状态。")

    # ======================= 核心拦截与处理模块 =======================

    @event_filter.on_llm_request(priority=70)
    async def store_llm_request(self, event: AstrMessageEvent, req):
        """
        捕获并存储请求。
        【防拦截核心】对 /cogito 等内部指令建立绝对白名单。
        """
        if not hasattr(req, "prompt") or not hasattr(req, "contexts"):
            return
            
        # 1. 防拦截检查
        msg_text = (event.message_str or "").strip().lower()
        if msg_text.startswith(("/cogito", "/rosaos", "reset", "new")):
            logger.debug(f"[IntelligentRetry] 旁路放行内部指令: {msg_text[:10]}...")
            return

        # 2. 正常存储逻辑
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

    async def _read_thought_log(self, index: int) -> Optional[str]:
        """异步读取日志文件，支持倒序索引"""
        now = datetime.now()
        # 确保 logs 目录使用绝对路径或相对运行根目录的路径
        # AstrBot 运行根目录通常包含 data/ 和 logs/
        log_file = os.path.join(LOG_DIR, f"{now.strftime('%Y-%m-%d')}_thought.log")
        
        # 简单的路径检查，如果相对路径找不到，尝试绝对路径（兜底）
        if not os.path.exists(log_file):
            # 尝试拼接到当前工作目录
            abs_path = os.path.abspath(log_file)
            if not os.path.exists(abs_path):
                # 再尝试一下用户提到的 /root/AstrBot/logs
                fallback_path = os.path.join("/root/AstrBot", LOG_DIR, f"{now.strftime('%Y-%m-%d')}_thought.log")
                if os.path.exists(fallback_path):
                    log_file = fallback_path
                else:
                    logger.warning(f"[IntelligentRetry] 日志文件未找到: {log_file}")
                    return None

        def _blocking_read():
            try:
                with open(log_file, "r", encoding="utf-8") as f:
                    content = f.read()
                
                # 【关键修复】使用列表推导式代替 filter(None, ...)，避免与模块名冲突
                entries = [entry.strip() for entry in content.split("\n\n") if entry.strip()]
                
                if not entries: return None
                
                target_idx = -1 * index
                if abs(target_idx) > len(entries): return None
                
                return entries[target_idx]
            except Exception as e:
                logger.error(f"[IntelligentRetry] 读取日志失败: {e}")
                return None

        return await asyncio.to_thread(_blocking_read)

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
                    await self._split_and_format_cot(new_response)
                    from astrbot.api.event import MessageEventResult, ResultContentType
                    result = MessageEventResult()
                    result.message(new_response.completion_text)
                    result.result_content_type = ResultContentType.LLM_RESULT
                    event.set_result(result)
                    return True
            if attempt < attempts: await asyncio.sleep(delay)
        return False

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

    async def _split_and_format_cot(self, response: LLMResponse):
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
        
        if thought: await self._async_log_thought(thought)
        for kw in self.filtered_keywords: reply = reply.replace(kw, "")
            
        if self.display_cot_text and thought:
            response.completion_text = f"🤔 罗莎思考中：\n{thought}\n\n---\n\n{reply}"
        else:
            response.completion_text = reply

    async def _async_log_thought(self, content: str):
        if not content: return
        if not os.path.exists(LOG_DIR): os.makedirs(LOG_DIR)
        def _write():
            now = datetime.now()
            fpath = os.path.join(LOG_DIR, f"{now.strftime('%Y-%m-%d')}_thought.log")
            with open(fpath, "a", encoding="utf-8") as f:
                f.write(f"[{now.strftime('%H:%M:%S')}] {content}\n\n")
        await asyncio.to_thread(_write)

    @event_filter.on_llm_response(priority=5)
    async def process_and_retry_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        if self.max_attempts <= 0 or not hasattr(resp, "completion_text"): return
        if getattr(resp, "raw_completion", None):
            choices = getattr(resp.raw_completion, "choices", [])
            if choices and getattr(choices[0], "finish_reason", None) == "tool_calls": return

        request_key = self._get_request_key(event)
        # 防拦截：如果 key 不在，说明是白名单指令，直接放行
        if request_key not in self.pending_requests: return

        text = resp.completion_text or ""
        is_trunc = self.enable_truncation_retry and getattr(self, "_is_truncated", lambda x: False)(resp)
        
        if not text.strip() or self._should_retry_response(resp) or is_trunc or self._is_cot_structure_incomplete(text):
            logger.info(f"[IntelligentRetry] 触发重试 (Key: {request_key})")
            if await self._execute_retry_sequence(event, request_key):
                res = event.get_result()
                resp.completion_text = res.get_plain_text() if res else ""
            else:
                if self.fallback_reply: resp.completion_text = self.fallback_reply
        
        await self._split_and_format_cot(resp)
        self.pending_requests.pop(request_key, None)

    @event_filter.on_decorating_result(priority=5)
    async def final_cot_stripper(self, event: AstrMessageEvent):
        result = event.get_result()
        if not result or not result.chain: return
        plain_text = result.get_plain_text()
        has_tag = self.cot_start_tag in plain_text or self.FINAL_REPLY_PATTERN.search(plain_text)
        if has_tag:
            for comp in result.chain:
                if isinstance(comp, Comp.Text) and comp.text:
                    temp = LLMResponse()
                    temp.completion_text = comp.text
                    await self._split_and_format_cot(temp)
                    comp.text = temp.completion_text

    async def terminate(self):
        self._cleanup_task.cancel()
        self.pending_requests.clear()
        logger.info("[IntelligentRetry] 插件已卸载")

# --- END OF FILE main.py ---
