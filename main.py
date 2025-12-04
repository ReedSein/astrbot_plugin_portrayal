import asyncio
from typing import Any, List, Dict

import astrbot.api.message_components as Comp
from astrbot import logger
from astrbot.api.event import filter, AstrMessageEvent
from astrbot.api.star import Context, Star, register
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import AiocqhttpMessageEvent

@register("astrbot_plugin_portrayal", "Zhalslar", "爬取群友聊天记录并生成性格画像", "v1.1.1")
class Relationship(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.conf = config
        # 移除 contexts_cache，遵守无状态 (Stateless) 原则

    def _build_user_context(self, round_messages: List[Dict[str, Any]], target_id: str) -> List[Dict[str, str]]:
        """构建 OpenAI 格式的上下文"""
        contexts = []
        target_int_id = int(target_id) # 提前转换，避免循环内重复转换

        for msg in round_messages:
            # 1. 过滤发送者
            if msg.get("sender", {}).get("user_id") != target_int_id:
                continue

            # 2. 提取并拼接所有 text 片段
            text_segments = [seg["data"]["text"] for seg in msg["message"] if seg["type"] == "text"]
            text = "".join(text_segments).strip()
            
            # 3. 仅当真正说了话才保留
            if text:
                contexts.append({"role": "user", "content": text})

        return contexts

    async def get_msg_contexts(
        self, event: AiocqhttpMessageEvent, target_id: str, max_query_rounds: int
    ) -> tuple[List[dict], int]:
        """
        持续获取群聊历史消息，包含健壮的重试机制
        """
        group_id = event.get_group_id()
        query_rounds = 0
        message_seq = 0
        contexts = []
        
        # 配置参数
        target_count = self.conf.get("max_msg_count", 500)
        per_count = self.conf.get("per_msg_count", 200)
        
        # 重试配置
        MAX_RETRIES = 3
        BASE_DELAY = 1.0  # 基础等待时间(秒)

        while len(contexts) < target_count:
            payloads = {
                "group_id": group_id,
                "message_seq": message_seq,
                "count": per_count,
                "reverseOrder": True,
            }
            
            round_messages = None
            
            # --- 重试逻辑块 START ---
            for attempt in range(MAX_RETRIES):
                try:
                    # 调用 OneBot API
                    result = await event.bot.api.call_action("get_group_msg_history", **payloads)
                    round_messages = result.get("messages", [])
                    # 如果成功获取，直接跳出重试循环
                    break 
                except Exception as e:
                    # 计算当前是第几次重试
                    if attempt < MAX_RETRIES - 1:
                        sleep_time = BASE_DELAY * (2 ** attempt) # 指数退避: 1s, 2s, 4s...
                        logger.warning(f"[astrbot_plugin_portrayal] 获取历史消息失败 (第 {attempt + 1}/{MAX_RETRIES} 次尝试): {e}。将在 {sleep_time}秒 后重试...")
                        await asyncio.sleep(sleep_time)
                    else:
                        # 最后一次尝试也失败了
                        logger.error(f"[astrbot_plugin_portrayal] 获取历史消息彻底失败，已达到最大重试次数。错误: {e}")
            # --- 重试逻辑块 END ---

            # 如果 round_messages 依然为 None 或空，说明 API 调用彻底失败或没有更多消息了
            if not round_messages:
                logger.info("[astrbot_plugin_portrayal] 消息获取中断：API调用失败或到达消息尽头。")
                break
                
            # 更新 seq，为下一轮做准备
            try:
                message_seq = round_messages[0]["message_id"]
            except (KeyError, IndexError):
                # 防御性编程：防止返回的数据结构异常
                logger.warning("[astrbot_plugin_portrayal] 历史消息数据结构异常，停止获取。")
                break

            # 处理数据
            contexts.extend(self._build_user_context(round_messages, target_id))
            
            query_rounds += 1
            if query_rounds >= max_query_rounds:
                break
                
        return contexts, query_rounds

    async def get_llm_respond(self, nickname: str, gender: str, contexts: List[dict]) -> str | None:
        """调用 LLM 进行分析"""
        try:
            # 1. 获取配置中的 Provider ID
            specific_provider_id = self.conf.get("specific_provider_id")
            
            # 2. 如果配置为空（用户没选），则获取当前会话默认的模型 ID
            target_provider_id = specific_provider_id if specific_provider_id else None

            system_prompt = self.conf["system_prompt_template"].format(
                nickname=nickname, 
                gender=("他" if gender == "male" else "她")
            )

            # 3. 调用 LLM，传入 chat_provider_id
            # 使用 v4.5.7+ 新版 API
            llm_response = await self.context.llm_generate(
                prompt=f"这是 {nickname} 的聊天记录，请根据 System Prompt 进行分析。",
                system_prompt=system_prompt,
                contexts=contexts, # 将聊天记录作为历史上下文传入
                chat_provider_id=target_provider_id  # <--- 指定特定模型
            )
            return llm_response.completion_text

        except Exception as e:
            logger.error(f"LLM 调用失败: {e}")
            return None

    # 辅助方法
    async def get_at_id(self, event: AiocqhttpMessageEvent) -> str | None:
        return next(
            (
                str(seg.qq)
                for seg in event.get_messages()
                if (isinstance(seg, Comp.At)) and str(seg.qq) != event.get_self_id()
            ),
            None,
        )

    async def get_nickname(self, event: AiocqhttpMessageEvent, user_id: str | int) -> tuple[str, str]:
        """获取指定群友的昵称和性别"""
        try:
            all_info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=int(user_id)
            )
            nickname = all_info.get("card") or all_info.get("nickname") or "群友"
            gender = all_info.get("sex", "unknown")
            return nickname, gender
        except Exception:
            return "群友", "unknown"

    @filter.command("画像")
    async def get_portrayal(self, event: AstrMessageEvent):
        """
        画像 @群友 <查询轮数>
        """
        # 1. 平台兼容性检查 (Fail Fast)
        if event.get_platform_name() != "aiocqhttp":
            yield event.plain_result("❌ 抱歉，该插件目前仅支持 OneBot (QQ) 协议，因为需要获取群历史消息。")
            return

        # 此时可以安全断言为 AiocqhttpMessageEvent
        assert isinstance(event, AiocqhttpMessageEvent)

        target_id = await self.get_at_id(event) or event.get_sender_id()
        nickname, gender = await self.get_nickname(event, target_id)
        
        # 解析参数
        msg_parts = event.message_str.split(" ")
        end_parm = msg_parts[-1]
        max_query_rounds = int(end_parm) if end_parm.isdigit() else self.conf.get("max_query_rounds", 10)
        target_query_rounds = min(200, max(0, max_query_rounds))

        # --- 文案修改点 1 ---
        yield event.plain_result(
            f"🚬 吐出一口烟圈，漫不经心地回溯着 {nickname} 留下的过往痕迹..."
        )
        
        # 获取消息 (无状态调用)
        contexts, query_rounds = await self.get_msg_contexts(
            event, target_id, target_query_rounds
        )

        if not contexts:
            yield event.plain_result("⚠️ 烟灰缸都满了，也没翻到这家伙的一句话。（未找到有效发言记录）")
            return

        # --- 文案修改点 2 ---
        yield event.plain_result(
            f"⚖️ 勉强扫了一眼 {len(contexts)} 条消息 (基于 {query_rounds} 轮扫描)... 罗莎正在透过屏幕，给这个家伙的性格定性..."
        )

        try:
            llm_respond = await self.get_llm_respond(nickname, gender, contexts)
            if llm_respond:
                url = await self.text_to_image(llm_respond)
                yield event.image_result(url)
            else:
                yield event.plain_result("❌ 啧，灵感枯竭了。（LLM 响应为空）")
        except Exception as e:
            logger.error(f"分析失败: {e}")
            yield event.plain_result(f"分析中断: {e}")
