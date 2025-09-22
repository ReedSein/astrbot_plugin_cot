# --- START OF MODIFIED FILE main.py ---

import asyncio
import json
import re
from typing import Dict, Any, Optional

import astrbot.api.message_components as Comp
from astrbot.api import logger, AstrBotConfig
from astrbot.api.star import Context, Star, register
from astrbot.api.event import AstrMessageEvent, filter
# 关键导入：我们需要 LLMResponse 类型来直接修改模型回复
from astrbot.api.provider import LLMResponse

# --- 日志记录部分 (与原代码相同) ---
LOG_DIR = r"logs"

def log_thought(content: str):
    """将思考内容写入独立的日志文件"""
    if not content:
        return
    try:
        if not os.path.exists(LOG_DIR):
            os.makedirs(LOG_DIR)
        now = datetime.now()
        log_file = os.path.join(LOG_DIR, f"{now.strftime('%Y-%m-%d')}_thought.log")
        with open(log_file, "a", encoding="utf-8") as f:
            f.write(f"[{now.strftime('%Y-%m-%d %H:%M:%S')}] {content}\n\n")
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
    FILTERED_KEYWORDS = ["哦？", "呵呵"]
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

        logger.info(
            f"已加载 [IntelligentRetryWithCoT] 插件 v3.0.0-Rosa , "
            f"将在LLM回复无效时重试，并在成功后自动处理罗莎的内心OS。显示模式: {'开启' if self.display_cot_text else '关闭'}"
        )

    # ... (所有来自 IntelligentRetry 的配置解析和请求存储方法 _parse_config, store_llm_request 等保持不变) ...
    # ... (为了简洁，省略了未修改的函数体，请保留您原有的代码) ...
    
    # [保留您所有的配置解析、请求存储、截断检测等函数，无需修改]
    # _parse_config, _get_request_key, store_llm_request, _is_truncated, 
    # _detect_character_level_truncation, _detect_structural_truncation, 等等...

    # --- 新增的核心方法：CoT结构验证 ---
    def _is_cot_structure_incomplete(self, text: str) -> bool:
        """
        验证罗莎人格的CoT结构是否完整。
        这是整合后新增的关键验证步骤。
        """
        # 如果文本中出现了CoT的任何一部分，就必须严格检查其完整性
        has_os_tag_start = "<罗莎内心OS>" in text
        has_final_reply_tag = self.FINAL_REPLY_PATTERN.search(text)

        if not has_os_tag_start and not has_final_reply_tag:
            # 如果完全没有CoT结构，我们认为它不是一个CoT回复，不按此规则判断截断
            return False

        # 只要出现了CoT的迹象，就必须同时满足两个条件才算完整
        is_complete = self.THOUGHT_TAG_PATTERN.search(text) and has_final_reply_tag
        
        if not is_complete:
            logger.debug("检测到罗莎CoT结构不完整，判定为需要重试。")
            return True
            
        return False

    # --- 修改的核心方法：在重试判断中加入CoT结构验证 ---
    @filter.on_llm_response(priority=10)
    async def process_and_retry_on_llm_response(self, event: AstrMessageEvent, resp: LLMResponse):
        """
        修改后的核心处理函数。
        它现在执行一个清晰的流程：
        1. 验证回复是否需要重试（包括技术截断和人格格式截断）。
        2. 如果需要，执行重试循环。
        3. 如果不需要重试（或重试成功），则执行CoT分割和格式化。
        """
        if self.max_attempts <= 0 or not hasattr(resp, "completion_text"):
            return

        request_key = self._get_request_key(event)
        if request_key not in self.pending_requests:
            return

        # --- 步骤1: 验证回复是否需要重试 ---
        should_retry = False
        original_text = resp.completion_text or ""

        # 首先判断基础错误（空回复、错误关键词）
        if not original_text.strip() or self._should_retry_response(resp):
             should_retry = True
             logger.debug("检测到空回复或错误关键词，需要重试。")
        # 然后判断技术截断
        elif self.enable_truncation_retry and self._is_truncated(resp):
            should_retry = True
            logger.debug("检测到技术层面的截断，需要重试。")
        # 最后，判断罗莎人格的CoT结构是否完整
        elif self._is_cot_structure_incomplete(original_text):
            should_retry = True

        if should_retry:
            logger.info("检测到需要重试的情况，开始执行重试序列...")
            retry_success = await self._execute_retry_sequence(event, request_key)
            if retry_success:
                # 重试成功后，event.get_result()里是新的完整回复
                # 我们需要更新resp对象，以便后续的CoT分割能处理它
                new_text = event.get_result().get_plain_text()
                resp.completion_text = new_text
                logger.info("重试成功，获得新的完整回复，准备进行CoT处理。")
            else:
                # 重试失败，发送兜底回复
                if self.fallback_reply and self.fallback_reply.strip():
                    resp.completion_text = self.fallback_reply.strip()
                logger.warning("所有重试均失败，将输出兜底回复或原始错误。")
                # 清理请求，然后返回，不再进行CoT分割
                if request_key in self.pending_requests:
                    del self.pending_requests[request_key]
                return
        
        # --- 步骤2: 执行CoT分割和格式化 ---
        # 无论是否经过重试，只要我们有了一份“最终”的回复，就执行此操作
        self._split_and_format_cot(resp)

        # --- 步骤3: 清理 ---
        if request_key in self.pending_requests:
            del self.pending_requests[request_key]
            logger.debug(f"处理完成，已清理请求参数: {request_key}")

    # --- 新增的核心方法：CoT分割逻辑 ---
    def _split_and_format_cot(self, response: LLMResponse):
        """
        从 ExternalCoTFilter 移植并优化的分割逻辑。
        此方法假设输入的 response.completion_text 是最终的、完整的。
        """
        if not response or not response.completion_text:
            return

        original_text = response.completion_text
        thought_part = ""
        reply_part = ""

        # 策略1：使用 "最终的罗莎回复" 标记进行分割
        parts = self.FINAL_REPLY_PATTERN.split(original_text, 1)
        if len(parts) > 1:
            # 进一步从第一部分提取内心OS
            os_match = self.THOUGHT_TAG_PATTERN.search(parts[0])
            if os_match:
                thought_part = os_match.group('content').strip()
            else:
                # 如果没有OS标签，但有分割符，则第一部分全部视为思考
                thought_part = parts[0].strip()
            reply_part = parts[1].strip()
        else:
            # 策略2：如果策略1失败，则尝试仅提取内心OS标签
            os_match = self.THOUGHT_TAG_PATTERN.search(original_text)
            if os_match:
                thought_part = os_match.group('content').strip()
                # 移除OS标签后，剩余部分为回复
                reply_part = self.THOUGHT_TAG_PATTERN.sub("", original_text).strip()
            else:
                # 如果没有任何标记，则认为全部是回复
                reply_part = original_text.strip()

        # 日志记录
        if thought_part:
            log_thought(thought_part)

        # 关键词过滤（仅对最终回复部分处理）
        for kw in self.FILTERED_KEYWORDS:
            reply_part = reply_part.replace(kw, "")
        
        # 根据配置决定最终输出
        if self.display_cot_text and thought_part:
            response.completion_text = f"🤔 思考过程：\n{thought_part}\n\n---\n\n{reply_part}"
        else:
            response.completion_text = reply_part
        
        logger.debug("CoT处理完成，已更新response.completion_text。")


    # ... (此处省略所有未修改的函数，请保留您原有的代码) ...
    # 比如 _should_retry_response, _perform_retry_with_stored_params, _execute_retry_sequence,
    # _sequential_retry_sequence, _concurrent_retry_sequence, 等等...
    # 唯一需要注意的是，现在 on_decorating_result 钩子可以被简化或移除，
    # 因为主要逻辑都集中在 on_llm_response 中了。

    # (可选) 简化 on_decorating_result
    @filter.on_decorating_result(priority=-100)
    async def final_check(self, event: AstrMessageEvent, *args, **kwargs):
        """
        这个钩子现在只作为一个最终的清理工，防止有请求被遗漏。
        """
        request_key = self._get_request_key(event)
        if request_key in self.pending_requests:
            logger.warning(f"在最终检查阶段发现未被处理的请求: {request_key}。可能是流程异常，执行清理。")
            del self.pending_requests[request_key]

# --- END OF MODIFIED FILE main.py ---