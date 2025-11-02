# --- START OF FILE main.py ---

import asyncio
import json
import re
from typing import Dict, Any, Optional
from datetime import datetime
import os
import time

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter
from astrbot.api.provider import LLMResponse

# --- 日志记录部分 (修改后，变为异步非阻塞) ---
LOG_DIR = r"logs"

async def log_thought(content: str):
    """将思考内容异步写入独立的日志文件，避免阻塞事件循环"""
    if not content:
        return
    try:
        def blocking_write():
            # 这个函数包含所有同步阻塞的代码
            if not os.path.exists(LOG_DIR):
                os.makedirs(LOG_DIR)
            now = datetime.now()
            log_file = os.path.join(LOG_DIR, f"{now.strftime('%Y-%m-%d')}_thought.log")
            with open(log_file, "a", encoding="utf-8") as f:
                f.write(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {content}\n\n")
        
        # 在独立的线程中执行阻塞的写入操作
        await asyncio.to_thread(blocking_write)

    except Exception as e:
        logger.error(f"写入思考日志时发生错误: {e}")


@register(
    "intelligent_retry_with_cot",
    "木有知 & 长安某 & 罗莎人格适配版",
    "集成了思维链(CoT)处理的智能重试插件。在验证回复完整性后，自动分离并记录内心OS，仅输出最终回复。",
    "3.0.0-Rosa",
)
class IntelligentRetryWithCoT(Star):
    # --- START: 从 ExternalCoTFilter 整合过来的逻辑 ---
    FINAL_REPLY_PATTERN = re.compile(r"最终的罗莎回复[:：]?\s*", re.IGNORECASE)
    THOUGHT_TAG_PATTERN = re.compile(
        r'<(?P<tag>罗莎内心OS)>(?P<content>.*?)</(?P=tag)>',
        re.DOTALL
    )
    FILTERED_KEYWORDS = ["呵呵，", "比利立我"]
    # --- END: 整合逻辑 ---

    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.pending_requests: Dict[str, Dict[str, Any]] = {}
        self._parse_config(config)
        
        # 从配置中读取是否显示思考过程，默认为False
        self.display_cot_text = (
            self.context.get_config()
            .get("provider_settings", {})
            .get("display_cot_text", False)
        )
        
        # 修改日志输出，反映新插件的名称和功能
        logger.info(
            f"已加载 [IntelligentRetryWithCoT] 插件 v3.0.0-Rosa , "
            f"将在LLM回复无效时重试，并在成功后自动处理罗莎的内心OS。显示模式: {'开启' if self.display_cot_text else '关闭'}"
        )

    def _parse_config(self, config: AstrBotConfig) -> None:
        """解析配置文件，统一配置初始化逻辑"""
        # 基础配置
        self.max_attempts = config.get("max_attempts", 3)
        self.retry_delay = config.get("retry_delay", 2)
        self.retry_delay_mode = (
            config.get("retry_delay_mode", "exponential").lower().strip()
        )

        # 错误关键词配置
        default_keywords = (
            "api 返回的内容为空\n"
            "API 返回的 completion 由于内容安全过滤被拒绝(非 AstrBot)\n"
            "调用失败\n"
            "[TRUNCATED_BY_LENGTH]\n"
            "达到最大长度限制而被截断"
        )
        keywords_str = config.get("error_keywords", default_keywords)
        self.error_keywords = [
            k.strip().lower() for k in keywords_str.split("\n") if k.strip()
        ]

        # 基于状态码的重试控制
        self.retryable_status_codes = self._parse_status_codes(
            config.get("retryable_status_codes", "400\n429\n502\n503\n504")
        )
        self.non_retryable_status_codes = self._parse_status_codes(
            config.get("non_retryable_status_codes", "")
        )

        # 兜底回复
        self.fallback_reply = config.get(
            "fallback_reply",
            "抱歉，刚才遇到服务波动，我已自动为你重试多次仍未成功。请稍后再试或换个说法。",
        )

        # 截断重试配置
        self.enable_truncation_retry = bool(
            config.get("enable_truncation_retry", False)
        )
        
        # --- 新增配置项 ---
        self.force_cot_structure = bool(config.get("force_cot_structure", True))
        logger.info(f"[IntelligentRetry] 强制CoT结构模式: {'开启' if self.force_cot_structure else '关闭'}")
        # --------------------
        
        # 通用方括号清理模式：匹配所有方括号 [] 及其内部内容 (作为兜底)
        self.universal_cleanup_pattern = re.compile(
            r"\[[\s\S]*?\]",
            re.DOTALL
        )

        # 新增：截断检测模式和选项
        self.truncation_detection_mode = (
            config.get("truncation_detection_mode", "enhanced").lower().strip()
        )
        self.check_structural_integrity = bool(
            config.get("check_structural_integrity", True)
        )
        self.check_content_type_specific = bool(
            config.get("check_content_type_specific", True)
        )
        self.min_reasonable_length = max(
            5, int(config.get("min_reasonable_length", 10))
        )
        self.code_block_detection = bool(config.get("code_block_detection", True))
        self.quote_matching_detection = bool(
            config.get("quote_matching_detection", True)
        )

        # 原有的正则表达式配置（保持向后兼容）
        self.truncation_valid_tail_pattern = config.get(
            "truncation_valid_tail_pattern",
            r"[。！？!?,;:、，．…—\-\(\)\[\]'\""
            "''\\w\\d_\u4e00-\u9fa5\\s\\t]$"
            r"|\.(com|cn|org|net|io|ai|pdf|jpg|png|jpeg|gif|mp3|mp4|txt|zip|tar|gz|html|htm)$"
            r"|https?://[\\w\.-]+$",
        )

        # 并发重试配置 - 遵循官方性能和安全规范
        self.enable_concurrent_retry = bool(
            config.get("enable_concurrent_retry", False)
        )
        self.concurrent_retry_threshold = max(
            0, int(config.get("concurrent_retry_threshold", 1))
        )

        # 基础并发数量配置
        concurrent_count = int(config.get("concurrent_retry_count", 2))
        self.concurrent_retry_count = max(
            1, min(concurrent_count, 5)
        )  # 基础并发数1-5范围

        # 指数增长控制配置
        self.enable_exponential_growth = bool(
            config.get("enable_exponential_growth", True)
        )
        self.max_concurrent_multiplier = max(
            2, min(int(config.get("max_concurrent_multiplier", 4)), 8)
        )
        self.absolute_concurrent_limit = max(
            5, min(int(config.get("absolute_concurrent_limit", 10)), 20)
        )

        # 超时时间限制，遵循官方资源管理规范
        timeout = int(config.get("concurrent_retry_timeout", 30))
        self.concurrent_retry_timeout = max(5, min(timeout, 300))  # 5-300秒范围

        # 配置验证日志 - 使用官方logger规范
        if self.enable_concurrent_retry:
            max_concurrent = min(
                self.concurrent_retry_count * self.max_concurrent_multiplier,
                self.absolute_concurrent_limit,
            )
            logger.info(
                f"并发重试配置: 阈值={self.concurrent_retry_threshold}(0=立即并发), "
                f"基础并发数={self.concurrent_retry_count}, 最大并发={max_concurrent}, "
                f"超时={self.concurrent_retry_timeout}s, 指数增长={'启用' if self.enable_exponential_growth else '禁用'}"
            )

    def _parse_status_codes(self, codes_str: str) -> set:
        """解析状态码配置字符串"""
        codes = set()
        for line in codes_str.split("\n"):
            line = line.strip()
            if line.isdigit():
                try:
                    codes.add(int(line))
                except Exception:
                    pass
        return codes

    def _get_request_key(self, event: AstrMessageEvent) -> str:
        """生成稳定的请求唯一标识符，不依赖可变的消息内容"""
        from datetime import datetime

        message_id = getattr(event.message_obj, "message_id", "no_id")
        # 使用时间戳作为后备，以处理某些平台可能没有 message_id 的情况
        timestamp = getattr(event.message_obj, "timestamp", datetime.now().timestamp())
        session_info = event.unified_msg_origin

        # 对于大多数平台，message_id 已经足够唯一。
        # 添加时间戳可以进一步增加唯一性，以防万一。
        return f"{session_info}_{message_id}_{timestamp}"

    @filter.on_llm_request(priority=70)
    async def store_llm_request(self, event: AstrMessageEvent, req):
        """存储LLM请求参数，并在存储前清理过期的挂起请求，防止内存泄漏。"""
        
        # --- 新增：内存泄漏防治机制 ---
        try:
            current_time = time.time()
            # 清理超过5分钟（300秒）的过期请求
            expired_keys = [
                key for key, value in self.pending_requests.items()
                if current_time - value.get("timestamp", 0) > 300
            ]
            if expired_keys:
                logger.debug(f"[IntelligentRetry] 清理了 {len(expired_keys)} 个过期的挂起请求。")
                for key in expired_keys:
                    del self.pending_requests[key]
        except Exception as e:
            logger.warning(f"[IntelligentRetry] 清理挂起请求时发生异常: {e}")
        # --- 内存清理结束 ---

        if not hasattr(req, "prompt") or not hasattr(req, "contexts"):
            logger.warning(
                "store_llm_request: Expected ProviderRequest-like object but got different type"
            )
            return
        
        request_key = self._get_request_key(event)

        image_urls = [
            comp.url
            for comp in event.message_obj.message
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
            "timestamp": time.time() # --- 新增：为当前请求添加时间戳 ---
        }
        
        stored_params["sender"] = {
            "user_id": getattr(event.message_obj, "user_id", None),
            "nickname": getattr(event.message_obj, "nickname", None),
            "group_id": getattr(event.message_obj, "group_id", None),
            "platform": getattr(event.message_obj, "platform", None),
        }
        
        provider_params = {}
        common_params = [
            "model", "temperature", "max_tokens", "top_p", "top_k",
            "frequency_penalty", "presence_penalty", "stop", "stream"
        ]
        for param in common_params:
            if hasattr(req, param):
                provider_params[param] = getattr(req, param, None)
        
        stored_params["provider_params"] = provider_params
        self.pending_requests[request_key] = stored_params
        logger.debug(f"已存储LLM请求参数（含完整人格信息和sender信息）: {request_key}")

    def _is_truncated(self, text_or_response) -> bool:
        """主入口方法：多层截断检测，支持文本和LLMResponse对象"""
        if hasattr(text_or_response, "completion_text"):
            resp = text_or_response
            text = resp.completion_text or ""

            if "[TRUNCATED_BY_LENGTH]" in text:
                logger.debug("LLMResponse对象中检测到截断标记")
                return True

            if (
                hasattr(resp, "raw_completion")
                and resp.raw_completion
                and hasattr(resp.raw_completion, "choices")
                and resp.raw_completion.choices
                and getattr(resp.raw_completion.choices[0], "finish_reason", None)
                == "length"
            ):
                logger.debug("LLMResponse对象的raw_completion检测到length截断")
                return True
        else:
            text = text_or_response

        if not text or not text.strip() or len(text.strip()) < self.min_reasonable_length:
            return False

        try:
            if self.truncation_detection_mode == "basic":
                return self._detect_character_level_truncation(text)
            elif self.truncation_detection_mode == "enhanced":
                return (
                    self._detect_character_level_truncation(text)
                    or self._detect_structural_truncation(text)
                    or self._detect_content_type_truncation(text)
                )
            elif self.truncation_detection_mode == "strict":
                return self._detect_character_level_truncation(
                    text
                ) and self._detect_structural_truncation(text)
            else:
                return self._detect_character_level_truncation(text)
        except Exception as e:
            logger.warning(f"截断检测发生错误，回退到基础模式: {e}")
            return self._detect_character_level_truncation(text)

    def _detect_character_level_truncation(self, text: str) -> bool:
        """第一层：增强的字符级截断检测"""
        if not text or not text.strip():
            return False
        last_line = text.strip().splitlines()[-1]
        enhanced_pattern = (
            self.truncation_valid_tail_pattern
            + r"|[->=:]+$|[}\])]$|[0-9]+[%°]?$"
            + r"|\.(py|js|ts|java|cpp|c|h|css|html|json|xml|yaml|yml|md|rst)$"
        )
        return not re.search(enhanced_pattern, last_line, re.IGNORECASE)

    def _detect_structural_truncation(self, text: str) -> bool:
        """第二层：结构完整性检测"""
        if not self.check_structural_integrity:
            return False
        try:
            if not self._check_bracket_balance(text):
                logger.debug("检测到括号不匹配，可能被截断")
                return True
            if self.quote_matching_detection and not self._check_quote_balance(text):
                logger.debug("检测到引号不匹配，可能被截断")
                return True
            if self.code_block_detection and not self._check_markdown_completeness(text):
                logger.debug("检测到代码块不完整，可能被截断")
                return True
            return False
        except Exception as e:
            logger.debug(f"结构检测出错，跳过: {e}")
            return False

    def _detect_content_type_truncation(self, text: str) -> bool:
        """第三层：内容类型自适应检测"""
        if not self.check_content_type_specific:
            return False
        try:
            content_type = self._get_content_type(text)
            if content_type == "code":
                return self._is_code_truncated(text)
            elif content_type == "list":
                return self._is_list_truncated(text)
            elif content_type == "table":
                return self._is_table_truncated(text)
            elif content_type == "json":
                return self._is_json_truncated(text)
            else:
                return self._is_natural_language_truncated(text)
        except Exception as e:
            logger.debug(f"内容类型检测出错，跳过: {e}")
            return False

    def _check_bracket_balance(self, text: str) -> bool:
        """检查括号是否平衡"""
        brackets = {"(": ")", "[": "]", "{": "}", "<": ">"}
        stack = []
        for char in text:
            if char in brackets:
                stack.append(char)
            elif char in brackets.values():
                if not stack or brackets[stack.pop()] != char:
                    return False
        return len(stack) == 0

    def _check_quote_balance(self, text: str) -> bool:
        """检查引号是否平衡"""
        if (text.count('"') - text.count('\\"')) % 2 != 0:
            return False
        single_quotes = text.count("'") - text.count("\\'")
        if single_quotes > 2 and single_quotes % 2 != 0:
            return False
        return True

    def _check_markdown_completeness(self, text: str) -> bool:
        """检查Markdown结构完整性"""
        if text.count("```") % 2 != 0:
            return False
        if (text.count("`") - text.count("\\`")) % 2 != 0:
            return False
        return True

    def _get_content_type(self, text: str) -> str:
        """识别内容类型"""
        text_lower = text.lower().strip()
        if text.count("```") >= 2 or re.search(r"^\s*(def|function|class|import|from|#include)", text, re.MULTILINE) or (text.count("{") > 2 and text.count("}") > 2):
            return "code"
        if (text_lower.startswith("{") and text_lower.endswith("}")) or (text_lower.startswith("[") and text_lower.endswith("]")):
            return "json"
        if re.search(r"^\s*[-*+]\s+", text, re.MULTILINE) or re.search(r"^\s*\d+\.\s+", text, re.MULTILINE):
            return "list"
        if "|" in text and text.count("|") > 3:
            return "table"
        return "natural_language"

    def _is_code_truncated(self, text: str) -> bool:
        """检测代码是否被截断"""
        if text.endswith('"') is False and '"' in text and text.count('"') % 2 == 1:
            return True
        lines = text.splitlines()
        if lines and lines[-1].strip().startswith("#") and not lines[-1].strip().endswith("."):
            return True
        return False

    def _is_list_truncated(self, text: str) -> bool:
        """检测列表是否被截断"""
        lines = text.strip().splitlines()
        if not lines: return False
        last_line = lines[-1].strip()
        if re.match(r"^\s*[-*+]\s*$", last_line) or re.match(r"^\s*\d+\.\s*$", last_line):
            return True
        return False

    def _is_table_truncated(self, text: str) -> bool:
        """检测表格是否被截断"""
        lines = text.strip().splitlines()
        if not lines: return False
        last_line = lines[-1]
        if "|" in last_line and not last_line.strip().endswith("|"):
            return True
        return False

    def _is_json_truncated(self, text: str) -> bool:
        """检测JSON是否被截断"""
        try:
            json.loads(text)
            return False
        except json.JSONDecodeError:
            return True

    def _is_natural_language_truncated(self, text: str) -> bool:
        """检测自然语言是否被截断"""
        conjunctions = ["and", "or", "but", "however", "therefore", "而且", "但是", "然而", "因此", "所以"]
        last_words = text.strip().split()[-3:]
        for word in last_words:
            if word.lower() in conjunctions:
                return True
        return False

    def _extract_status_code(self, text: str) -> Optional[int]:
        """从错误文本中提取 4xx/5xx 状态码"""
        if not text: return None
        try:
            match = re.search(r"\b([45]\d{2})\b", text)
            if match: return int(match.group(1))
        except Exception: pass
        return None

    def _should_retry_response(self, result) -> bool:
        """判断是否需要重试（重构后的检测逻辑）"""
        # (此函数在 process_and_retry_on_llm_response 中被调用，用于检测基础错误)
        if not result:
            logger.debug("结果为空，需要重试")
            return True

        if hasattr(result, "completion_text"): # 传入的是LLMResponse
            message_str = result.completion_text or ""
        elif hasattr(result, "get_plain_text"): # 传入的是MessageEventResult
            message_str = result.get_plain_text()
        else:
            return False

        if not message_str.strip():
            logger.debug("检测到空回复，需要重试")
            return True

        # 状态码检测
        code = self._extract_status_code(message_str)
        if code is not None:
            if code in self.non_retryable_status_codes:
                return False
            if code in self.retryable_status_codes:
                return True

        # 关键词检测
        lower_message_str = message_str.lower()
        for keyword in self.error_keywords:
            if keyword in lower_message_str:
                logger.debug(f"检测到错误关键词 '{keyword}'，需要重试")
                return True
        
        return False

    async def _perform_retry_with_stored_params(self, request_key: str) -> Optional[Any]:
        """使用存储的参数执行重试"""
        if request_key not in self.pending_requests:
            logger.warning(f"未找到存储的请求参数: {request_key}")
            return None

        stored_params = self.pending_requests[request_key]
        
        if not stored_params.get("prompt") or not str(stored_params["prompt"]).strip():
            logger.error("存储的prompt参数为空，无法进行重试")
            return None
        
        provider = self.context.get_using_provider()
        if not provider:
            logger.warning("LLM提供商未启用，无法重试。")
            return None

        try:
            kwargs = {
                "prompt": stored_params["prompt"],
                "image_urls": stored_params.get("image_urls", []),
                "func_tool": stored_params.get("func_tool", None),
            }
            
            system_prompt = None
            conversation = stored_params.get("conversation")
            if conversation and hasattr(conversation, "persona_id") and conversation.persona_id:
                try:
                    persona_mgr = getattr(self.context, "persona_manager", None)
                    if persona_mgr:
                        persona = await persona_mgr.get_persona(conversation.persona_id)
                        if persona and persona.system_prompt:
                            system_prompt = persona.system_prompt
                except Exception as e:
                    logger.warning(f"重试时实时加载 Persona 失败: {e}")

            if not system_prompt:
                system_prompt = stored_params.get("system_prompt")
            
            if system_prompt:
                kwargs["system_prompt"] = system_prompt
            
            sender_info = stored_params.get("sender", {})
            if conversation:
                kwargs["conversation"] = conversation
                if sender_info:
                    self._attach_sender_to_conversation(conversation, sender_info)
            else:
                kwargs["contexts"] = stored_params.get("contexts", [])
                if sender_info:
                    kwargs["sender"] = sender_info
            
            if "provider_params" in stored_params:
                provider_params = stored_params["provider_params"]
                for param_name, param_value in provider_params.items():
                    if param_value is not None:
                        kwargs[param_name] = param_value

            logger.debug(f"正在执行重试，prompt前50字符: '{stored_params['prompt'][:50]}...'")
            return await provider.text_chat(**kwargs)

        except Exception as e:
            logger.error(f"重试调用LLM时发生错误: {e}", exc_info=True)
            return None
    
    def _attach_sender_to_conversation(self, conversation, sender_info: dict) -> None:
        """将sender信息附加到conversation对象的辅助方法"""
        if not conversation or not sender_info: return
        try:
            if not hasattr(conversation, "metadata") or conversation.metadata is None:
                conversation.metadata = {}
            conversation.metadata["sender"] = sender_info
        except Exception as e:
            logger.debug(f"设置sender信息时出现异常（已忽略）: {e}")

    async def _execute_retry_sequence(self, event: AstrMessageEvent, request_key: str) -> bool:
        """执行重试序列（支持顺序和并发两种模式）"""
        delay = max(0, int(self.retry_delay))
        if not self.enable_concurrent_retry or self.concurrent_retry_threshold > 0:
            attempts = self.concurrent_retry_threshold if self.enable_concurrent_retry else self.max_attempts
            if await self._sequential_retry_sequence(event, request_key, attempts, delay):
                return True
        
        if self.enable_concurrent_retry:
            remaining_attempts = self.max_attempts - self.concurrent_retry_threshold
            if remaining_attempts > 0:
                return await self._concurrent_retry_sequence(event, request_key, remaining_attempts)
        
        return False

    async def _sequential_retry_sequence(self, event: AstrMessageEvent, request_key: str, max_attempts: int, initial_delay: int) -> bool:
        """顺序重试序列"""
        delay = initial_delay
        for attempt in range(1, max_attempts + 1):
            logger.info(f"第 {attempt}/{max_attempts} 次重试...")
            new_response = await self._perform_retry_with_stored_params(request_key)
            if new_response and getattr(new_response, "completion_text", ""):
                if not self._should_retry_response(new_response) and not self._is_truncated(new_response) and not self._is_cot_structure_incomplete(new_response.completion_text):
                    logger.info(f"第 {attempt} 次重试成功")
                    
                    # --- 关键修复：在设置结果前，手动调用 favourpro 的核心逻辑 ---
                    try:
                        favourpro_plugin = self.context.get_star_instance("astrbot_plugin_favourpro")
                        if favourpro_plugin and hasattr(favourpro_plugin, "process_llm_response"):
                            # 调用 favourpro 的核心处理逻辑，完成状态更新和清理
                            await favourpro_plugin.process_llm_response(event, new_response)
                            logger.debug("重试成功后，已手动调用 favourpro 插件进行状态更新和清理。")
                    except Exception as e:
                        logger.error(f"重试成功后手动调用 favourpro 失败: {e}")
                    # --- 修复结束 ---

                    # --- CoT 分割和清理 ---
                    await self._split_and_format_cot(new_response)
                    # --- CoT 修复结束 ---

                    from astrbot.api.event import MessageEventResult, ResultContentType
                    result = MessageEventResult()
                    result.message(new_response.completion_text)
                    result.result_content_type = ResultContentType.LLM_RESULT
                    event.set_result(result)
                    return True
            if attempt < max_attempts and delay > 0:
                await asyncio.sleep(delay)
                if self.retry_delay_mode == "exponential":
                    delay = min(delay * 2, 30)
        return False

    async def _concurrent_retry_sequence(self, event: AstrMessageEvent, request_key: str, remaining_attempts: int) -> bool:
        """并发重试序列"""
        if remaining_attempts <= 0: return False
        attempts_used = 0
        batch_number = 1
        while attempts_used < remaining_attempts:
            if self.enable_exponential_growth:
                base_count = self.concurrent_retry_count
                exp_count = base_count * (2 ** (batch_number - 1))
                current_concurrent_count = min(exp_count, remaining_attempts - attempts_used, self.concurrent_retry_count * self.max_concurrent_multiplier, self.absolute_concurrent_limit)
            else:
                current_concurrent_count = min(self.concurrent_retry_count, remaining_attempts - attempts_used)
            
            logger.info(f"启动第 {batch_number} 批次并发重试，并发数: {current_concurrent_count}")
            if await self._single_concurrent_batch(event, request_key, current_concurrent_count):
                return True
            
            attempts_used += current_concurrent_count
            batch_number += 1
            if attempts_used < remaining_attempts:
                await asyncio.sleep(1)
        return False

    async def _single_concurrent_batch(self, event: AstrMessageEvent, request_key: str, concurrent_count: int) -> bool:
        """执行单个并发批次"""
        first_valid_result = None
        result_lock = asyncio.Lock()

        async def single_concurrent_attempt(attempt_id: int):
            nonlocal first_valid_result
            try:
                new_response = await self._perform_retry_with_stored_params(request_key)
                if new_response and getattr(new_response, "completion_text", ""):
                    if not self._should_retry_response(new_response) and not self._is_truncated(new_response) and not self._is_cot_structure_incomplete(new_response.completion_text):
                        async with result_lock:
                            if first_valid_result is None:
                                first_valid_result = new_response.completion_text
                                logger.info(f"并发重试任务 #{attempt_id} 获得首个有效结果")
            except Exception as e:
                logger.error(f"并发重试任务 #{attempt_id} 发生异常: {e}")

        tasks = [asyncio.create_task(single_concurrent_attempt(i)) for i in range(1, concurrent_count + 1)]
        try:
            await asyncio.wait(tasks, timeout=self.concurrent_retry_timeout, return_when=asyncio.ALL_COMPLETED)
        except asyncio.TimeoutError:
            logger.warning(f"并发重试超时（{self.concurrent_retry_timeout}s）")
        
        await self._cleanup_concurrent_tasks(tasks)

        if first_valid_result:
            # --- 关键修复：在设置结果前，先进行CoT分割和清理 ---
            temp_resp = LLMResponse()
            temp_resp.completion_text = first_valid_result
            await self._split_and_format_cot(temp_resp)
            # --- 修复结束 ---
            from astrbot.api.event import MessageEventResult, ResultContentType
            result = MessageEventResult()
            result.message(temp_resp.completion_text)
            result.result_content_type = ResultContentType.LLM_RESULT
            event.set_result(result)
            return True
        return False

    def _handle_retry_failure(self, event: AstrMessageEvent) -> None:
        """处理重试失败的情况"""
        logger.error(f"所有 {self.max_attempts} 次重试均失败")
        if self.fallback_reply and self.fallback_reply.strip():
            from astrbot.api.event import MessageEventResult, ResultContentType
            result = MessageEventResult()
            result.message(self.fallback_reply.strip())
            result.result_content_type = ResultContentType.LLM_RESULT
            event.set_result(result)
        else:
            event.clear_result()
            event.stop_event()

    # --- 新增/修改的核心逻辑 ---
    def _is_cot_structure_incomplete(self, text: str) -> bool:
        """
        验证罗莎人格的CoT结构是否完整。
        如果开启了 force_cot_structure，则任何不包含完整结构的消息都会被视为不完整。
        """
        if not text:
            return False  # 空文本不处理

        has_os_tag_start = "<罗莎内心OS>" in text
        has_os_tag_end = "</罗莎内心OS>" in text
        has_final_reply_tag = self.FINAL_REPLY_PATTERN.search(text)

        is_structure_complete = has_os_tag_start and has_os_tag_end and has_final_reply_tag

        # 如果开启了强制模式，那么任何不完整的结构（包括完全没有结构）都应该重试
        if self.force_cot_structure:
            if not is_structure_complete:
                logger.debug("强制CoT模式开启：检测到结构不完整或缺失，将触发重试。")
                return True # 返回 True 表示“不完整”，需要重试
            return False # 结构完整，返回 False

        # 如果未开启强制模式，则使用旧逻辑：只有在结构部分存在但不完整时才重试
        else:
            # 如果压根没有任何标签，就认为它不是一个CoT回复，直接放行
            if not has_os_tag_start and not has_final_reply_tag:
                return False
            
            # 如果有部分标签但结构不完整，则判定为不完整
            if not is_structure_complete:
                logger.debug("检测到罗莎CoT结构部分存在但不完整，将触发重试。")
                return True
            
            return False

    async def _split_and_format_cot(self, response: LLMResponse):
        """分割CoT并格式化最终回复。"""
        if not response or not response.completion_text: return
        original_text = response.completion_text
        thought_part, reply_part = "", original_text
        
        parts = self.FINAL_REPLY_PATTERN.split(original_text, 1)
        if len(parts) > 1:
            os_match = self.THOUGHT_TAG_PATTERN.search(parts[0])
            thought_part = os_match.group('content').strip() if os_match else parts[0].strip()
            reply_part = parts[1].strip()
        else:
            os_match = self.THOUGHT_TAG_PATTERN.search(original_text)
            if os_match:
                thought_part = os_match.group('content').strip()
                reply_part = self.THOUGHT_TAG_PATTERN.sub("", original_text).strip()
        
        if thought_part: await log_thought(thought_part)
        for kw in self.FILTERED_KEYWORDS:
            reply_part = reply_part.replace(kw, "")
        
        if self.display_cot_text and thought_part:
            response.completion_text = f"🤔 思考过程：\n{thought_part}\n\n---\n\n{reply_part}"
        else:
            response.completion_text = reply_part
            
        # 兜底清理：移除所有剩余的方括号内容
        response.completion_text = self.universal_cleanup_pattern.sub(
            '', 
            response.completion_text
        ).strip()

    @filter.on_llm_response(priority=5)
    async def process_and_retry_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """核心处理钩子：验证 -> 重试 -> 分割"""
        if self.max_attempts <= 0 or not hasattr(resp, "completion_text"):
            return
            
        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests:
            return

        # --- 新增：工具调用前置检查 ---
        # 检查这是否是一次工具调用响应。如果是，则直接返回，不进行任何处理。
        # 插件会“站到一边”，等待工具执行完毕后的下一次（真正的）文本响应。
        # 此时我们不删除 pending_requests 中的 key，因为最终的文本回复还需要它。
        if (hasattr(resp, "raw_completion") and resp.raw_completion and
            hasattr(resp.raw_completion, "choices") and resp.raw_completion.choices and
            getattr(resp.raw_completion.choices[0], "finish_reason", None) == "tool_calls"):
            
            logger.debug("[IntelligentRetry] 检测到工具调用，跳过本次响应处理，并保留请求密钥以待最终回复。")
            return  # 直接返回，等待工具执行后的最终文本回复
        # --- 工具调用检查结束 ---

        original_text = resp.completion_text or ""
        should_retry = (
            not original_text.strip()
            or self._should_retry_response(resp)
            or (self.enable_truncation_retry and self._is_truncated(resp))
            or self._is_cot_structure_incomplete(original_text)
        )

        if should_retry:
            logger.info("检测到需要重试的情况，开始执行重试序列...")
            if await self._execute_retry_sequence(event, request_key):
                resp.completion_text = event.get_result().get_plain_text()
                logger.info("重试成功，准备进行CoT处理。")
            else:
                if self.fallback_reply: resp.completion_text = self.fallback_reply
                logger.warning("所有重试均失败，将输出兜底回复或原始错误。")
                if request_key in self.pending_requests:
                    del self.pending_requests[request_key]
                return
        
        await self._split_and_format_cot(resp)
        if request_key in self.pending_requests:
            del self.pending_requests[request_key]

    @filter.on_decorating_result(priority=-100)
    async def check_and_retry(self, event: AstrMessageEvent, *args, **kwargs):
        """备用检查和清理钩子"""
        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests: return

        llm_response = getattr(event, "llm_response", None)
        if llm_response and hasattr(llm_response, "choices") and llm_response.choices:
            if getattr(llm_response.choices[0], "finish_reason", None) == "tool_calls":
                if request_key in self.pending_requests: del self.pending_requests[request_key]
                return

        result = event.get_result()
        if not self._should_retry_response(result):
            if request_key in self.pending_requests: del self.pending_requests[request_key]
            return

        if not event.message_str or not event.message_str.strip():
            if request_key in self.pending_requests: del self.pending_requests[request_key]
            return

        logger.info("在结果装饰阶段检测到需要重试的情况（备用处理）")
        if not await self._execute_retry_sequence(event, request_key):
            self._handle_retry_failure(event)

        if request_key in self.pending_requests:
            del self.pending_requests[request_key]

    async def _cleanup_concurrent_tasks(self, tasks):
        """安全清理并发任务"""
        if not tasks: return
        for task in tasks:
            if not task.done():
                task.cancel()
                try: await task
                except asyncio.CancelledError: pass
                except Exception as e: logger.debug(f"清理并发任务时出现异常: {e}")

    @filter.on_decorating_result(priority=5)
    async def final_cot_stripper(self, event: AstrMessageEvent, *args, **kwargs):
        """
        最终出口拦截器。
        专门处理在工具调用等特殊流程中，可能被绕过 on_llm_response 钩子的 CoT 文本。
        这个钩子在消息发送前的最后阶段运行，确保万无一失。
        """
        result = event.get_result()
        if not result or not result.chain:
            return

        # 获取即将发送的纯文本内容
        plain_text = result.get_plain_text()

        # 检查是否包含未经处理的 CoT 结构
        has_os_tag = "<罗莎内心OS>" in plain_text
        has_final_reply_tag = self.FINAL_REPLY_PATTERN.search(plain_text)

        if has_os_tag or has_final_reply_tag: # 使用 OR 条件，增强安全网的覆盖范围
            logger.debug("[IntelligentRetry] 在最终出口检测到未处理的CoT结构，正在进行最后分割...")
            
            # 创建一个临时的 LLMResponse 对象来复用我们的分割逻辑
            temp_resp = LLMResponse()
            temp_resp.completion_text = plain_text
            
            # 调用我们现有的、强大的分割和格式化函数
            await self._split_and_format_cot(temp_resp)
            
            # 用处理过的干净文本，更新最终要发送的消息
            # 我们需要重建消息链，因为原始消息可能包含图片等组件
            new_message_chain = []
            text_part_updated = False
            for component in result.chain:
                if isinstance(component, Comp.Text) and not text_part_updated:
                    new_message_chain.append(Comp.Text(text=temp_resp.completion_text))
                    text_part_updated = True
                elif not isinstance(component, Comp.Text):
                    new_message_chain.append(component)
            
            result.chain.clear()
            result.chain.extend(new_message_chain)

    async def terminate(self):
        """插件卸载时清理资源"""
        self.pending_requests.clear()
        logger.info("已卸载 [IntelligentRetryWithCoT] 插件并清理所有资源")

    @filter.on_decorating_result(priority=9999)
    async def final_universal_cleanup(self, event: AstrMessageEvent, *args, **kwargs):
        """
        最终、无条件的兜底清理钩子。
        在所有插件处理完毕后执行，确保没有任何方括号内容泄露给用户。
        """
        result = event.get_result()
        if not result or not result.chain: return

        # 获取即将发送的纯文本内容
        text = result.get_plain_text()
        if not text: return

        # 使用通用方括号清理模式，移除所有 [] 及其内容
        cleaned_text = self.universal_cleanup_pattern.sub('', text).strip()

        # 只有在确实发生了清理时才重新设置结果
        if cleaned_text != text:
            # 重新设置消息链中的文本部分
            new_message_chain = []
            text_part_updated = False
            for component in result.chain:
                if isinstance(component, Comp.Text) and not text_part_updated:
                    new_message_chain.append(Comp.Text(text=cleaned_text))
                    text_part_updated = True
                elif not isinstance(component, Comp.Text):
                    new_message_chain.append(component)
            
            result.chain.clear()
            result.chain.extend(new_message_chain)

# --- END OF FILE main.py ---
