import asyncio
import datetime
import discord
from discord.ext import commands
from config import JST, RECRUIT_KEYWORDS, ALLOWED_CHANNEL_IDS, RECRUIT_CHANNEL_ID
from helpers import parse_duration, format_duration, parse_clock_time, extract_clock_time


class Recruit(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.polls: dict[int, dict] = {}

    async def notify_game_start(self, poll_msg_id, channel_id, seconds, time_str):
        await asyncio.sleep(seconds)
        channel = self.bot.get_channel(channel_id)
        try:
            poll_msg = await channel.fetch_message(poll_msg_id)
        except discord.NotFound:
            return
        mentions = []
        for reaction in poll_msg.reactions:
            if str(reaction.emoji) == "✅":
                async for user in reaction.users():
                    if not user.bot:
                        mentions.append(user.mention)
        mention_str = " ".join(mentions) if mentions else ""
        await channel.send(f"⏰ **{time_str}** になったぞ！ゲーム開始の時間だ！\n{mention_str}")

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return
        if message.channel.id not in ALLOWED_CHANNEL_IDS:
            return
        if not any(kw in message.content for kw in RECRUIT_KEYWORDS):
            return

        time_result = extract_clock_time(message.content)
        if time_result:
            _, time_str, start_at = time_result
            description = (
                f"**{message.author.display_name}** さんが "
                f"**{time_str}から** 一緒にゲームをする人を募集しています！"
            )
        else:
            start_at = None
            description = f"**{message.author.display_name}** さんが一緒にゲームをする人を募集しています！"

        embed = discord.Embed(
            title="🎮 ゲーム募集",
            description=description,
            color=discord.Color.green(),
        )
        embed.set_footer(text="✅ 参加する　🕐 後から参加　❌ 参加できない")

        recruit_channel = self.bot.get_channel(RECRUIT_CHANNEL_ID)
        poll = await recruit_channel.send(embed=embed)
        await poll.add_reaction("✅")
        await poll.add_reaction("🕐")
        await poll.add_reaction("❌")
        self.polls[poll.id] = {"channel_id": recruit_channel.id, "start_at": start_at}

        if time_result:
            seconds_until, time_str, _ = time_result
            asyncio.create_task(
                self.notify_game_start(poll.id, recruit_channel.id, seconds_until, time_str)
            )

    @commands.Cog.listener()
    async def on_raw_reaction_add(self, payload):
        if payload.user_id == self.bot.user.id:
            return
        if payload.message_id not in self.polls:
            return
        if str(payload.emoji) != "🕐":
            return

        guild = self.bot.get_guild(payload.guild_id)
        member = guild.get_member(payload.user_id)
        if member is None:
            return

        poll_data = self.polls[payload.message_id]
        recruit_channel = self.bot.get_channel(poll_data["channel_id"])
        start_at = poll_data["start_at"]

        if start_at:
            time_str_base = f"{start_at.hour}時" + (f"{start_at.minute}分" if start_at.minute else "")
            dm_hint = (
                f"🕐 **{time_str_base}から**のゲームに何分後に参加できる？\n"
                f"例: `10m`（{time_str_base}の10分後）　`1h`（1時間後）　`15時30分から`（時刻で直接指定）"
            )
        else:
            dm_hint = (
                "🕐 何分後（何時間後）に参加できる？\n"
                "例: `10m`（10分後）　`1h30m`（1時間30分後）　`15時から`　`15時30分から`"
            )

        try:
            await member.send(dm_hint)
        except discord.Forbidden:
            await recruit_channel.send(
                f"{member.mention} DMが送れなかったよ。何分後・何時から参加できるか教えて！"
            )
            return

        def is_reply(m):
            return m.author.id == payload.user_id and isinstance(m.channel, discord.DMChannel)

        try:
            reply = await self.bot.wait_for("message", check=is_reply, timeout=60)
        except asyncio.TimeoutError:
            await member.send("⌛ タイムアウトしたよ。もう一度 🕐 を押してね。")
            return

        text = reply.content.strip()

        duration_sec = parse_duration(text)
        if duration_sec is not None:
            if start_at:
                notify_at = start_at + datetime.timedelta(seconds=duration_sec)
                seconds = max(0, int((notify_at - datetime.datetime.now(JST)).total_seconds()))
                notify_str = f"{notify_at.hour}時" + (f"{notify_at.minute}分" if notify_at.minute else "")
                confirm = f"⏱️ わかった！{notify_str}（{time_str_base}の{format_duration(duration_sec)}後）に教えるね。"
                notify = f"⏰ {member.mention} {notify_str}になったぞ！そろそろゲーム参加できる？"
            else:
                if duration_sec > 3 * 3600:
                    await member.send("❌ タイマーは最大3時間までだよ！")
                    return
                seconds = duration_sec
                confirm = f"⏱️ わかった！{format_duration(seconds)}後に教えるね。"
                notify = f"⏰ {member.mention} {format_duration(seconds)}経ったぞ！そろそろゲーム参加できる？"
        else:
            result = parse_clock_time(text)
            if result is None:
                await member.send(
                    "❌ 形式が合ってないよ！\n"
                    "例: `10m` `1h30m` `15時から` `15時30分から`"
                )
                return
            seconds, time_str = result
            if seconds > 12 * 3600:
                await member.send("❌ 12時間以上先の時刻は指定できないよ！")
                return
            confirm = f"⏱️ わかった！{time_str}になったら教えるね。"
            notify = f"⏰ {member.mention} {time_str}になったぞ！そろそろゲーム参加できる？"

        await member.send(confirm)
        await asyncio.sleep(seconds)
        await recruit_channel.send(notify)


async def setup(bot):
    await bot.add_cog(Recruit(bot))
