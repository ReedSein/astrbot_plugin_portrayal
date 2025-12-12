import asyncio
import re
from datetime import datetime
from typing import Any, List, Dict

import astrbot.api.message_components as Comp
from astrbot import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register("astrbot_plugin_portrayal", "Zhalslar", "爬取群友聊天记录并生成性格画像", "v1.2.2")
class Relationship(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config

    # --- 核心逻辑部分 ---

    def _build_user_context(self, round_messages: List[Dict[str, Any]], target_id: str) -> List[Dict[str, str]]:
        """构建 OpenAI 格式的上下文"""
        contexts = []
        target_int_id = int(target_id) 

        for msg in round_messages:
            if msg.get("sender", {}).get("user_id") != target_int_id:
                continue

            text_segments = [seg["data"]["text"] for seg in msg["message"] if seg["type"] == "text"]
            text = "".join(text_segments).strip()
            
            if text:
                contexts.append({"role": "user", "content": text})

        return contexts

    async def get_msg_contexts(
        self, event: AiocqhttpMessageEvent, target_id: str, max_query_rounds: int
    ) -> tuple[List[dict], int]:
        """持续获取群聊历史消息 (带重试机制)"""
        group_id = event.get_group_id()
        query_rounds = 0
        message_seq = 0
        contexts = []
        
        target_count = self.conf.get("max_msg_count", 500)
        per_count = self.conf.get("per_msg_count", 200)
        MAX_RETRIES = 3
        BASE_DELAY = 1.0

        while len(contexts) < target_count:
            payloads = {
                "group_id": group_id,
                "message_seq": message_seq,
                "count": per_count,
                "reverseOrder": True,
            }
            round_messages = None
            
            for attempt in range(MAX_RETRIES):
                try:
                    result = await event.bot.api.call_action("get_group_msg_history", **payloads)
                    round_messages = result.get("messages", [])
                    break 
                except Exception as e:
                    if attempt < MAX_RETRIES - 1:
                        await asyncio.sleep(BASE_DELAY * (2 ** attempt))
                    else:
                        logger.error(f"[portrayal] 获取历史消息失败: {e}")

            if not round_messages:
                break
                
            try:
                message_seq = round_messages[0]["message_id"]
            except (KeyError, IndexError):
                break

            contexts.extend(self._build_user_context(round_messages, target_id))
            query_rounds += 1
            if query_rounds >= max_query_rounds:
                break
                
        return contexts, query_rounds

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

    async def get_llm_respond(self, user_info: Dict[str, Any], contexts: List[dict]) -> str | None:
        """调用 LLM 进行分析 (带智能重试)"""
        specific_provider_id = self.conf.get("specific_provider_id")
        target_provider_id = specific_provider_id if specific_provider_id else None
        
        # 获取重试配置
        max_retries = max(1, int(self.conf.get("llm_max_retries", 3)))
        retry_delay = max(0, int(self.conf.get("llm_retry_delay", 2)))

        # 准备格式化参数
        format_args = user_info.copy()
        format_args["gender_cn"] = "他" if user_info.get("gender") == "male" else "她"
        format_args.setdefault("nickname", "群友")
        
        try:
            system_prompt = self.conf["system_prompt_template"].format(**format_args)
        except KeyError as e:
            logger.warning(f"[portrayal] System Prompt 格式化缺少变量: {e}, 将使用默认简单模板")
            system_prompt = f"分析此人的性格。档案：{user_info.get('profile', '无')}"

        final_prompt = (
            f"以下是 {user_info.get('nickname')} 的聊天记录片段。请根据 System Prompt 进行深度性格分析。"
        )

        for attempt in range(1, max_retries + 1):
            try:
                if attempt > 1:
                    logger.info(f"[portrayal] 正在进行第 {attempt}/{max_retries} 次 LLM 重试...")
                
                llm_response = await self.context.llm_generate(
                    prompt=final_prompt,
                    system_prompt=system_prompt,
                    contexts=contexts,
                    chat_provider_id=target_provider_id
                )
                
                text = llm_response.completion_text
                
                # 校验逻辑
                is_empty = not (text and text.strip())
                is_error = self._has_api_error_pattern(text)
                
                if not is_empty and not is_error:
                    return text
                else:
                    logger.warning(f"[portrayal] 第 {attempt} 次生成结果无效 (Empty: {is_empty}, Error: {is_error})")
                    if attempt < max_retries:
                        await asyncio.sleep(retry_delay)
            
            except Exception as e:
                logger.error(f"[portrayal] 第 {attempt} 次调用异常: {e}")
                if attempt < max_retries:
                    await asyncio.sleep(retry_delay)

        logger.error("[portrayal] LLM 重试耗尽，分析失败。")
        return None

    # --- 信息获取与处理部分 ---

    async def get_target_info(self, event: AiocqhttpMessageEvent, user_id: str) -> Dict[str, Any]:
        """
        获取目标的详细信息，返回字典供模板渲染
        """
        group_id = int(event.get_group_id())
        user_id_int = int(user_id)
        
        # 初始化数据字典，给所有可能的字段默认值，防止 format 报错
        info = {
            "nickname": "群友",
            "gender": "unknown",
            "age": "未知",
            "level": "未知",
            "role": "成员",
            "title": "无",
            "join_time": "未知",
            "last_sent": "未知",
            "profile": "" # 汇总摘要
        }

        try:
            member_info = await event.bot.get_group_member_info(
                group_id=group_id, user_id=user_id_int, no_cache=True
            )
        except Exception:
            member_info = {}

        try:
            stranger_info = await event.bot.get_stranger_info(
                user_id=user_id_int, no_cache=True
            )
        except Exception:
            stranger_info = {}

        # 填充数据
        info["nickname"] = member_info.get("card") or member_info.get("nickname") or stranger_info.get("nickname") or "群友"
        info["gender"] = member_info.get("sex") or stranger_info.get("sex") or "unknown"
        
        raw_age = stranger_info.get("age", 0)
        if raw_age: info["age"] = str(raw_age)

        role_map = {"owner": "群主", "admin": "管理员", "member": "群员"}
        raw_role = member_info.get("role", "member")
        info["role"] = role_map.get(raw_role, raw_role)

        raw_level = stranger_info.get("level", 0)
        if raw_level: info["level"] = str(raw_level)

        raw_title = member_info.get("title", "")
        if raw_title: info["title"] = raw_title

        join_ts = member_info.get("join_time", 0)
        if join_ts:
            info["join_time"] = datetime.fromtimestamp(join_ts).strftime('%Y-%m-%d')
            
        last_sent_ts = member_info.get("last_sent_time", 0)
        if last_sent_ts:
             info["last_sent"] = datetime.fromtimestamp(last_sent_ts).strftime('%Y-%m-%d %H:%M')

        # 生成摘要 profile，方便用户直接用 {profile}
        profile_parts = []
        if info["age"] != "未知": profile_parts.append(f"年龄:{info['age']}")
        if info["level"] != "未知": profile_parts.append(f"LV:{info['level']}")
        profile_parts.append(f"身份:{info['role']}")
        if info["title"] != "无": profile_parts.append(f"头衔:{info['title']}")
        if info["join_time"] != "未知": profile_parts.append(f"入群:{info['join_time']}")
        
        info["profile"] = " | ".join(profile_parts)
        
        return info

    async def get_at_id(self, event: AiocqhttpMessageEvent) -> str | None:
        return next(
            (str(seg.qq) for seg in event.get_messages() if isinstance(seg, Comp.At) and str(seg.qq) != event.get_self_id()),
            None
        )

    @filter.command("画像")
    async def get_portrayal(self, event: AstrMessageEvent):
        """画像 @群友 <查询轮数>"""
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("❌ 抱歉，该插件目前仅支持 OneBot (QQ) 协议。")
            return
        assert isinstance(event, AiocqhttpMessageEvent)

        target_id = await self.get_at_id(event) or event.get_sender_id()
        
        # 获取详细信息字典
        user_info = await self.get_target_info(event, target_id)
        nickname = user_info["nickname"]
        
        # 解析参数
        msg_parts = event.message_str.split(" ")
        end_parm = msg_parts[-1]
        max_query_rounds = int(end_parm) if end_parm.isdigit() else self.conf.get("max_query_rounds", 10)
        target_query_rounds = min(200, max(0, max_query_rounds))

        yield event.plain_result(
            f"🚬 吐出一口烟圈，漫不经心地回溯着 {nickname} 留下的过往痕迹..."
        )
        
        contexts, query_rounds = await self.get_msg_contexts(event, target_id, target_query_rounds)

        if not contexts:
            yield event.plain_result("⚠️ 烟灰缸都满了，也没翻到这家伙的一句话。（未找到有效发言记录）")
            return

        yield event.plain_result(
            f"⚖️ 勉强扫了一眼 {len(contexts)} 条消息 (基于 {query_rounds} 轮扫描)... 罗莎正在透过屏幕，给这个家伙的性格定性..."
        )

        try:
            # 传入完整信息字典
            llm_respond = await self.get_llm_respond(user_info, contexts)
            if llm_respond:
                url = await self.text_to_image(llm_respond)
                yield event.image_result(url)
            else:
                yield event.plain_result("❌ 啧，灵感枯竭了。（LLM 响应为空）")
        except Exception as e:
            logger.error(f"分析失败: {e}")
            yield event.plain_result(f"分析中断: {e}")
