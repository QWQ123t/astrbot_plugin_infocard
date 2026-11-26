# astrbot_plugin_infocard - QQ用户资料卡片 + 群管工具插件
# v1.1.0: 资料卡片（/开盒）+ 群管指令集 + 进群审核
import asyncio
import re
import weakref

from aiocqhttp import CQHttp

import astrbot.api.message_components as Comp
from astrbot.api import logger
from astrbot.api.event import filter
from astrbot.api.star import Context, Star
from astrbot.core.config.astrbot_config import AstrBotConfig
from astrbot.core.message.components import At, BaseMessageComponent
from astrbot.core.message.message_event_result import MessageChain
from astrbot.core.platform.sources.aiocqhttp.aiocqhttp_message_event import (
    AiocqhttpMessageEvent,
)
from astrbot.core.star.filter.platform_adapter_type import PlatformAdapterType

from .core.config import PluginConfig
from .core.service import BoxResult, BoxService


class InfoCardPlugin(Star):
    def __init__(self, context: Context, config: AstrBotConfig):
        super().__init__(context)
        self.cfg = PluginConfig(config, context)
        self.box = BoxService(self.cfg)
        self._recall_tasks: weakref.WeakSet[asyncio.Task] = weakref.WeakSet()
        # 进群审核请求队列: 编号 -> {flag, user_id, group_id, comment}
        self.review_queue: dict[int, dict] = {}
        self.review_seq: int = 0
        # 群管辅助状态
        self._admin_cache: dict[str, tuple[float, set[str]]] = {}
        self._flood_track: dict[tuple[str, str], list[float]] = {}

    async def terminate(self):
        if self._recall_tasks:
            for t in list(self._recall_tasks):
                t.cancel()
            await asyncio.gather(*self._recall_tasks, return_exceptions=True)

    # ================= 资料卡片 =================

    @filter.command("开盒")
    async def on_command(
        self,
        event: AiocqhttpMessageEvent,
        input_id: int | str | None = None,
    ):
        """开盒 @群友/@qq, 查询 QQ 用户资料信息"""
        target_ids = self._get_ats(event, self.cfg.protect_ids) or [
            event.get_sender_id()
        ]

        for tid in target_ids:
            result = await self.box.get_box_info(
                event.bot,
                target_id=str(tid),
                group_id=event.get_group_id() or "",
            )
            if not result.is_fail():
                await self.send_box_image(event, result)

        event.stop_event()

    @filter.llm_tool()
    async def llm_infocard_user(
        self,
        event: AiocqhttpMessageEvent,
        user_id: str = "",
        send_image: bool = True,
    ):
        """
        Query QQ user profile information.
        Args:
            user_id(string): Target QQ user ID. It must be numeric.
                Defaults to the current user when empty.
            send_image(bool): Whether to send the rendered image. Defaults to True.
        """
        target_id = str(user_id).strip() or event.get_sender_id()

        if not target_id.isdigit():
            return "InfoCard query failed: user_id must be a numeric QQ ID."

        if target_id == event.get_self_id():
            return "InfoCard query failed: cannot query the bot itself."

        if target_id in self.cfg.protect_ids and target_id != event.get_sender_id():
            return "InfoCard query failed: this user is protected."

        group_id = event.get_group_id() or "0"

        try:
            result = await self.box.get_box_info(
                event.bot,
                target_id,
                group_id,
            )

            if result.is_fail():
                return f"InfoCard query failed: {result.error}"

            if send_image:
                await self.send_box_image(event, result)
                return "Image sent."

            return result.to_plain()

        except Exception as e:
            logger.error(f"llm_infocard_user failed: {e}")
            return f"InfoCard query failed: {e}"

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def handle_group_add(self, event: AiocqhttpMessageEvent):
        """自动开盒新群友/主动退群之人"""
        raw = getattr(event.message_obj, "raw_message", None)

        if not (
            isinstance(raw, dict)
            and raw.get("post_type") == "notice"
            and raw.get("user_id") != raw.get("self_id")
        ):
            return

        is_enter = raw.get("notice_type") == "group_increase"
        is_exit = (
            raw.get("notice_type") == "group_decrease"
            and raw.get("sub_type") == "leave"
        )

        if not (
            (is_enter and self.cfg.autobox.enter) or (is_exit and self.cfg.autobox.exit)
        ):
            return

        group_id = str(raw.get("group_id"))
        user_id = str(raw.get("user_id"))

        if user_id in self.cfg.protect_ids or user_id == event.get_self_id():
            return

        result = await self.box.get_box_info(
            event.bot,
            user_id,
            group_id,
        )

        if not result.is_fail():
            await self.send_box_image(event, result)

        event.stop_event()

    # ================= 群管工具 =================

    async def _is_group_admin(self, event: AiocqhttpMessageEvent) -> bool:
        """Bot管理员 或 群主/群管理员"""
        if event.is_admin():
            return True
        gid = event.get_group_id()
        if not gid:
            return False
        admins = await self._get_group_admins(gid)
        return str(event.get_sender_id()) in admins

    async def _get_group_admins(self, gid: str) -> set[str]:
        """群管理员列表（10分钟缓存，避免高频API调用）"""
        now = time.time()
        cached = self._admin_cache.get(gid)
        if cached and now - cached[0] < 600:
            return cached[1]
        admins: set[str] = set()
        try:
            members = await event_bot_get_members(self, gid)
        except Exception as e:
            logger.error(f"获取成员列表失败: {e}")
            members = []
        for m in members or []:
            if m.get("role") in ("admin", "owner"):
                admins.add(str(m.get("user_id")))
        self._admin_cache[gid] = (now, admins)
        return admins

    def _get_at_targets(self, event: AiocqhttpMessageEvent) -> list[str]:
        """仅提取消息中的 At 目标（不把数字参数当QQ号）"""
        ats = []
        for seg in event.get_messages()[1:]:
            if isinstance(seg, At) and str(seg.qq) != str(event.get_self_id()):
                qq = str(seg.qq)
                if qq != "all" and qq not in ats:
                    ats.append(qq)
        return ats

    def _cmd_args(self, event: AiocqhttpMessageEvent) -> str:
        """去掉指令词后的参数文本"""
        text = (event.message_str or "").strip()
        for cmd in (
            "禁言", "解禁", "全禁", "全员禁言", "踢人",
            "管理员", "群审核", "审核",
        ):
            if text.startswith(cmd):
                return text[len(cmd):].strip()
        return text

    def _guard_admin(self, event: AiocqhttpMessageEvent) -> bool:
        """非管理员的指令统一静默提示"""
        return True

    @filter.command("禁言")
    async def cmd_ban(self, event: AiocqhttpMessageEvent):
        """禁言 <秒数> @用户"""
        if not event.get_group_id() or not await self._is_group_admin(event):
            return
        targets = self._get_at_targets(event)
        if not targets:
            await event.send(event.chain_result(
                [Comp.Plain("用法：/禁言 <秒数> @用户")]))
            return
        args = self._cmd_args(event)
        m = re.search(r'\d+', args)
        duration = min(max(int(m.group()) if m else 600, 1), 2591999)
        ok, fail = [], []
        for uid in targets:
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    duration=duration,
                )
                ok.append(uid)
            except Exception as e:
                logger.error(f"禁言失败 {uid}: {e}")
                fail.append(uid)
        msg = f"已禁言 @{' @'.join(ok)} {duration} 秒"
        if fail:
            msg += f"\n失败：{' '.join(fail)}"
        await event.send(event.chain_result([Comp.At(qq=event.get_sender_id()), Comp.Plain(f"\n{msg}")]))
        event.stop_event()

    @filter.command("解禁")
    async def cmd_unban(self, event: AiocqhttpMessageEvent):
        """解禁 @用户"""
        if not event.get_group_id() or not await self._is_group_admin(event):
            return
        targets = self._get_at_targets(event)
        if not targets:
            await event.send(event.chain_result(
                [Comp.Plain("用法：/解禁 @用户")]))
            return
        for uid in targets:
            try:
                await event.bot.set_group_ban(
                    group_id=int(event.get_group_id()),
                    user_id=int(uid),
                    duration=0,
                )
            except Exception as e:
                logger.error(f"解禁失败 {uid}: {e}")
        await event.send(event.chain_result(
            [Comp.At(qq=event.get_sender_id()), Comp.Plain("\n已解除禁言")]))
        event.stop_event()

    @filter.command("全禁", alias={"全员禁言"})
    async def cmd_whole_ban(self, event: AiocqhttpMessageEvent):
        """全禁 开/关"""
        if not event.get_group_id() or not await self._is_group_admin(event):
            return
        args = self._cmd_args(event)
        if args.startswith("开"):
            enable = True
        elif args.startswith("关"):
            enable = False
        else:
            await event.send(event.chain_result(
                [Comp.Plain("用法：/全禁 开 或 /全禁 关")]))
            return
        try:
            await event.bot.set_group_whole_ban(
                group_id=int(event.get_group_id()), enable=enable
            )
            state = "已开启全员禁言 🔇" if enable else "已关闭全员禁言 💬"
            await event.send(event.chain_result([Comp.Plain(state)]))
        except Exception as e:
            logger.error(f"全员禁言失败: {e}")
            await event.send(event.chain_result([Comp.Plain(f"操作失败：{e}")]))
        event.stop_event()

    @filter.command("踢人")
    async def cmd_kick(self, event: AiocqhttpMessageEvent):
        """踢人 @用户"""
        if not event.get_group_id() or not await self._is_group_admin(event):
            return
        targets = self._get_at_targets(event)
        if not targets:
            await event.send(event.chain_result(
                [Comp.Plain("用法：/踢人 @用户")]))
            return
        for uid in targets:
            try:
                await event.bot.set_group_kick(
                    group_id=int(event.get_group_id()), user_id=int(uid)
                )
            except Exception as e:
                logger.error(f"踢人失败 {uid}: {e}")
        await event.send(event.chain_result(
            [Comp.At(qq=event.get_sender_id()), Comp.Plain("\n已移出指定成员")]))
        event.stop_event()

    @filter.command("管理员")
    async def cmd_admin_toggle(self, event: AiocqhttpMessageEvent):
        """管理员 @用户（设置/取消管理员）"""
        if not event.get_group_id() or not await self._is_group_admin(event):
            return
        targets = self._get_at_targets(event)
        if not targets:
            await event.send(event.chain_result(
                [Comp.Plain("用法：/管理员 @用户（再次执行即取消）")]))
            return
        uid = targets[0]
        try:
            info = await event.bot.get_group_member_info(
                group_id=int(event.get_group_id()), user_id=int(uid)
            )
            enable = info.get("role") != "admin"
            await event.bot.set_group_admin(
                group_id=int(event.get_group_id()),
                user_id=int(uid),
                enable=enable,
            )
            state = "已设为管理员" if enable else "已取消管理员"
            await event.send(event.chain_result([Comp.Plain(state)]))
        except Exception as e:
            logger.error(f"管理员操作失败: {e}")
            await event.send(event.chain_result([Comp.Plain(f"操作失败：{e}")]))
        event.stop_event()

    @filter.command("群审核")
    async def cmd_review_toggle(self, event: AiocqhttpMessageEvent):
        """群审核 开/关"""
        if not event.get_group_id() or not await self._is_group_admin(event):
            return
        args = self._cmd_args(event)
        if args.startswith("开"):
            self.cfg.group_review.enable = True
        elif args.startswith("关"):
            self.cfg.group_review.enable = False
        else:
            state = "开启" if self.cfg.group_review.enable else "关闭"
            await event.send(event.chain_result(
                [Comp.Plain(f"当前进群审核：{state}。用法：/群审核 开 或 /群审核 关")]))
            return
        self.cfg.save_config()
        state = "开启" if self.cfg.group_review.enable else "关闭"
        await event.send(event.chain_result([Comp.Plain(f"进群审核已{state}")]))
        event.stop_event()

    # ================= 进群审核 =================

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def handle_group_request(self, event: AiocqhttpMessageEvent):
        """加群请求：自动艾特管理员并给出处理选项"""
        raw = getattr(event.message_obj, "raw_message", None)
        if not (
            isinstance(raw, dict)
            and raw.get("post_type") == "request"
            and raw.get("request_type") == "group"
        ):
            return

        if not self.cfg.group_review.enable:
            return

        group_id = str(raw.get("group_id") or "")
        user_id = str(raw.get("user_id") or "")
        flag = str(raw.get("flag") or "")
        comment = str(raw.get("comment") or "") or "（无验证消息）"
        sub_type = str(raw.get("sub_type") or "add")

        if not group_id or not user_id or not flag:
            return

        event.stop_event()

        # 黑名单成员：直接拒绝
        if user_id in self.cfg.group_review.blacklist:
            try:
                await event.bot.set_group_add_request(
                    flag=flag, sub_type=sub_type, approve=False, reason="您已被拉黑"
                )
            except Exception as e:
                logger.error(f"黑名单拒绝失败: {e}")
            return

        # 拉取申请者昵称
        nickname = user_id
        try:
            info = await event.bot.get_stranger_info(user_id=int(user_id))
            nickname = info.get("nickname") or user_id
        except Exception:
            pass

        # 群管理员列表
        admin_ids: list[str] = []
        admin_set = await self._get_group_admins(event.bot, group_id)
        admin_ids = sorted(admin_set)
        if str(event.get_sender_id()) in admin_set:
            admin_ids.remove(str(event.get_sender_id()))

        self.review_seq += 1
        seq = self.review_seq
        # 队列防膨胀：只保留最近 20 条
        if len(self.review_queue) >= 20:
            for old in sorted(self.review_queue)[: len(self.review_queue) - 19]:
                self.review_queue.pop(old, None)
        self.review_queue[seq] = {
            "flag": flag,
            "user_id": user_id,
            "group_id": group_id,
            "comment": comment,
            "sub_type": sub_type,
        }

        chain: list[BaseMessageComponent] = [Comp.Plain(
            f"📢 进群审核 #{seq}\n申请人：{nickname}（{user_id}）\n"
            f"验证消息：{comment}\n"
            f"回复：同意 {seq} / 拒绝 {seq} / 拉黑 {seq}\n"
        )]
        for aid in admin_ids[:10]:
            chain.append(Comp.At(qq=aid))
        try:
            # request 事件的 event.send 不可靠，直接用 OneBot API 发送
            obmsg = await event._parse_onebot_json(MessageChain(chain=chain))
            await event.bot.send_group_msg(group_id=int(group_id), message=obmsg)
        except Exception as e:
            logger.error(f"进群审核通知发送失败: {e}")

    async def _handle_review_action(
        self,
        event: AiocqhttpMessageEvent,
        action: str,
    ):
        if not event.get_group_id() or not await self._is_group_admin(event):
            return
        args = self._cmd_args(event)
        m = re.search(r'\d+', args)
        if not m:
            await event.send(event.chain_result(
                [Comp.Plain(f"用法：{action} <编号>（编号见进群审核通知）")]))
            return
        seq = int(m.group())
        item = self.review_queue.get(seq)
        if not item:
            await event.send(event.chain_result([Comp.Plain(f"未找到编号 #{seq} 的申请（可能已处理或过期）")]))
            return

        approve = action == "同意"
        reason = ""
        if action == "拉黑":
            approve = False
            reason = "您已被拉黑"
            if item["user_id"] not in self.cfg.group_review.blacklist:
                self.cfg.group_review.blacklist.append(item["user_id"])
                self.cfg.save_config()

        try:
            await event.bot.set_group_add_request(
                flag=item["flag"],
                sub_type=item["sub_type"],
                approve=approve,
                reason=reason,
            )
            self.review_queue.pop(seq, None)
            result_text = {
                "同意": "✅ 已同意进群",
                "拒绝": "🚫 已拒绝进群",
                "拉黑": "⛔ 已拒绝并拉黑（再申请将自动拒绝）",
            }[action]
            await event.send(event.chain_result([Comp.Plain(f"#{seq} {result_text}")]))
        except Exception as e:
            logger.error(f"审核操作失败: {e}")
            await event.send(event.chain_result([Comp.Plain(f"操作失败：{e}")]))
        event.stop_event()

    @filter.command("同意")
    async def cmd_review_approve(self, event: AiocqhttpMessageEvent):
        await self._handle_review_action(event, "同意")

    @filter.command("拒绝")
    async def cmd_review_reject(self, event: AiocqhttpMessageEvent):
        await self._handle_review_action(event, "拒绝")

    @filter.command("拉黑")
    async def cmd_review_blacklist(self, event: AiocqhttpMessageEvent):
        await self._handle_review_action(event, "拉黑")

    # ================= 刷屏防护（表情包 + 长文字） =================

    @filter.platform_adapter_type(PlatformAdapterType.AIOCQHTTP)
    async def handle_antiflood(self, event: AiocqhttpMessageEvent):
        """刷屏防护：单条消息文字超上限，或窗口时间内连续发表情包达上限，自动禁言"""
        if not self.cfg.antiflood.enable or not event.get_group_id():
            return

        uid = str(event.get_sender_id())
        gid = str(event.get_group_id())

        # 豁免：自己 / Bot管理员 / 群管理员 / 保护名单
        if uid == event.get_self_id() or uid in self.cfg.protect_ids:
            return
        if event.is_admin() or uid in await self._get_group_admins(event.bot, gid):
            return

        # 从 OneBot 原始上报统计图片/表情包段与文字长度
        # （AstrBot 会丢弃 mface 商城表情段，必须读 raw_message 的 message 数组）
        img_count = 0
        text_len = 0
        raw = getattr(event.message_obj, "raw_message", None)
        if isinstance(raw, dict):
            if raw.get("post_type") not in (None, "message"):
                return
            msg_array = raw.get("message")
            if isinstance(msg_array, list):
                for seg in msg_array:
                    if isinstance(seg, dict):
                        if seg.get("type") in ("image", "mface"):
                            img_count += 1
                        elif seg.get("type") == "text":
                            text_len += len(str(seg.get("data", {}).get("text", "")))
            elif isinstance(msg_array, str):
                # 兼容 CQ 码字符串格式
                img_count += len(re.findall(r'\[CQ:(?:image|mface)[,\]]', msg_array))
                text_len += len(re.sub(r'\[CQ:[^\]]+\]', '', msg_array))
        else:
            # raw_message 非 dict 时回退到组件统计
            for s in event.get_messages():
                sname = type(s).__name__
                if sname in ("Image", "Mface", "Img") or getattr(s, "type", "") in ("image", "mface"):
                    img_count += 1
                elif sname == "Plain":
                    text_len += len(str(getattr(s, "text", "") or ""))

        # ---- 文字刷屏：单条消息超字数上限 ----
        max_chars = max(10, int(self.cfg.antiflood.max_chars))
        if text_len > max_chars and img_count == 0:
            ban_sec = max(10, int(self.cfg.antiflood.ban_seconds))
            try:
                await event.bot.set_group_ban(
                    group_id=int(gid), user_id=int(uid), duration=ban_sec
                )
                await event.send(event.chain_result([
                    Comp.At(qq=uid),
                    Comp.Plain(
                        f" 文字刷屏检测：单条消息 {text_len} 字，超过上限 {max_chars} 字，"
                        f"已自动禁言 {ban_sec} 秒。"
                    ),
                ]))
                event.stop_event()
            except Exception as e:
                logger.error(f"文字刷屏禁言失败 {uid}: {e}")
            return

        key = (gid, uid)
        now = time.time()
        window = max(10, int(self.cfg.antiflood.window_seconds))
        max_count = max(2, int(self.cfg.antiflood.max_count))

        if img_count == 0:
            # 非表情包消息打断连续计数
            self._flood_track.pop(key, None)
            return

        bucket = self._flood_track.setdefault(key, [])
        bucket[:] = [t for t in bucket if now - t <= window]
        bucket.extend([now] * img_count)

        if len(bucket) < max_count:
            return

        # 触发：清零计数并自动禁言
        self._flood_track.pop(key, None)
        ban_sec = max(10, int(self.cfg.antiflood.ban_seconds))
        try:
            await event.bot.set_group_ban(
                group_id=int(gid), user_id=int(uid), duration=ban_sec
            )
            await event.send(event.chain_result([
                Comp.At(qq=uid),
                Comp.Plain(
                    f" 刷屏检测：短时间内连续发送 {len(bucket)} 个表情包，"
                    f"已自动禁言 {ban_sec} 秒。"
                ),
            ]))
            event.stop_event()
        except Exception as e:
            logger.error(f"刷屏禁言失败 {uid}: {e}")

    # ================= 卡片发送 =================

    async def send_box_image(
        self,
        event: AiocqhttpMessageEvent,
        result: BoxResult,
    ):
        if result.is_fail():
            return

        image = await self.box.render_box_image(result)
        chain: list[BaseMessageComponent] = [Comp.Image.fromBytes(image)]

        recall_time = self.cfg.recall_time

        if recall_time:
            await self.recall_task(event, chain, recall_time)
        else:
            await event.send(event.chain_result(chain))

    async def recall_task(
        self,
        event: AiocqhttpMessageEvent,
        chain: list[BaseMessageComponent],
        recall_time: int,
    ):
        client = event.bot
        obmsg = await event._parse_onebot_json(MessageChain(chain=chain))  # type: ignore

        result = None
        if group_id := event.get_group_id():
            result = await client.send_group_msg(group_id=int(group_id), message=obmsg)
        elif user_id := event.get_sender_id():
            result = await client.send_private_msg(user_id=int(user_id), message=obmsg)

        if result and (message_id := result.get("message_id")):
            task = asyncio.create_task(
                self._recall_msg(client, int(message_id), recall_time)
            )
            self._recall_tasks.add(task)
            task.add_done_callback(lambda t: self._recall_tasks.discard(t))

    async def _recall_msg(self, client: CQHttp, message_id: int, delay: int):
        await asyncio.sleep(delay)
        try:
            await client.delete_msg(message_id=message_id)
        except Exception as e:
            logger.error(f"recall failed: {e}")

    def _get_ats(
        self,
        event: AiocqhttpMessageEvent,
        block_ids: list[str] | None = None,
    ) -> list[str]:
        ats = {str(seg.qq) for seg in event.get_messages()[1:] if isinstance(seg, At)}
        for arg in event.message_str.split():
            if arg.startswith("@") and arg[1:].isdigit():
                ats.add(arg[1:])
            elif arg.isdigit():
                ats.add(arg)
        ats.discard(event.get_self_id())
        if block_ids:
            ats.difference_update(block_ids)
        return list(ats)
