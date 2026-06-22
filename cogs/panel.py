import random
import asyncio
import discord
from discord.ext import commands
from config import TARGET_USER_IDS, GEMINI_API_KEY
from cogs.apex import (
    APEX_LEGENDS, APEX_API_KEY,
    build_rankmap_embed, build_apexstatus_embed, build_apexstats_embed,
)
from cogs.stats import build_stats_embed
from cogs.fun import (
    ROULETTES, NEWS_TEMPLATES,
    fill_template, scan_messages, generate_ai_news,
)
from cogs.utils import build_mac_embed, build_ping_embed


class TeamSelectView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=60)

    @discord.ui.select(
        cls=discord.ui.UserSelect,
        placeholder="チーム分けするメンバーを選択（2人以上）",
        min_values=2,
        max_values=10,
    )
    async def select_members(self, interaction: discord.Interaction, select: discord.ui.UserSelect):
        members = select.values
        shuffled = list(members)
        random.shuffle(shuffled)
        mid = (len(shuffled) + 1) // 2
        team_a = shuffled[:mid]
        team_b = shuffled[mid:]
        embed = discord.Embed(title="⚔️ チーム分け結果", color=discord.Color.blue())
        embed.add_field(name="🔴 チームA", value="\n".join(f"・{m.display_name}" for m in team_a), inline=True)
        embed.add_field(name="🔵 チームB", value="\n".join(f"・{m.display_name}" for m in team_b), inline=True)
        if len(members) % 2 != 0:
            embed.set_footer(text="人数が奇数のためチームAに1人多く振り分けました")
        await interaction.response.send_message(embed=embed)


class ApexStatsModal(discord.ui.Modal, title="👤 Apex プレイヤー統計"):
    username = discord.ui.TextInput(label="EA名", placeholder="プレイヤー名を入力", required=True)
    platform = discord.ui.TextInput(label="プラットフォーム（PC / PS4 / X1）", default="PC", required=False)

    async def on_submit(self, interaction: discord.Interaction):
        await interaction.response.defer(thinking=True)
        plat = self.platform.value.strip() or "PC"
        embed = await build_apexstats_embed(self.username.value.strip(), plat)
        if embed is None:
            await interaction.followup.send("❌ プレイヤーが見つからなかったよ（EA名とプラットフォームを確認してね）")
        else:
            await interaction.followup.send(embed=embed)


class PanelView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=600)

    @discord.ui.button(label="🗺️ ランクマップ", style=discord.ButtonStyle.primary, row=0)
    async def btn_rankmap(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            await interaction.followup.send(embed=await build_rankmap_embed())
        except Exception as e:
            await interaction.followup.send(f"❌ 取得失敗: {e}")

    @discord.ui.button(label="🖥️ サーバー状態", style=discord.ButtonStyle.primary, row=0)
    async def btn_apexstatus(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            await interaction.followup.send(embed=await build_apexstatus_embed())
        except Exception as e:
            await interaction.followup.send(f"❌ 取得失敗: {e}")

    @discord.ui.button(label="🎯 レジェンド", style=discord.ButtonStyle.secondary, row=0)
    async def btn_apex(self, interaction: discord.Interaction, button: discord.ui.Button):
        legend, catchphrase = random.choice(list(APEX_LEGENDS.items()))
        await interaction.response.send_message(f"🎯 今日のレジェンドは **{legend}** だ！\n> {catchphrase}")

    @discord.ui.button(label="👤 Apex統計", style=discord.ButtonStyle.primary, row=0)
    async def btn_apexstats(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(ApexStatsModal())

    @discord.ui.button(label="⚔️ チーム分け", style=discord.ButtonStyle.primary, row=1)
    async def btn_team(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_message(
            "チーム分けするメンバーを選んでね！", view=TeamSelectView(), ephemeral=True
        )

    @discord.ui.button(label="🎰 ルーレット", style=discord.ButtonStyle.danger, row=1)
    async def btn_roulette(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        await asyncio.sleep(2)
        if interaction.user.id in TARGET_USER_IDS:
            result = "🍺 次の集まりで全員分おごり確定！"
        else:
            result = random.choice(ROULETTES)
        await interaction.followup.send(f"🎰 **結果発表！** {interaction.user.mention}\n{result}")

    @discord.ui.button(label="🍟 マック", style=discord.ButtonStyle.success, row=1)
    async def btn_mac(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            embed = await build_mac_embed()
            if embed:
                await interaction.followup.send(embed=embed)
            else:
                await interaction.followup.send("❌ メニューの取得に失敗したよ")
        except Exception as e:
            await interaction.followup.send(f"❌ エラー: {e}")

    @discord.ui.button(label="📊 サーバー統計", style=discord.ButtonStyle.secondary, row=2)
    async def btn_stats(self, interaction: discord.Interaction, button: discord.ui.Button):
        stats_cog = interaction.client.get_cog("Stats")
        await interaction.response.send_message(
            embed=build_stats_embed(interaction.guild, stats_cog.data if stats_cog else {})
        )

    @discord.ui.button(label="🌐 回線速度", style=discord.ButtonStyle.secondary, row=2)
    async def btn_ping(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(thinking=True)
        try:
            await interaction.followup.send(embed=await build_ping_embed())
        except Exception as e:
            await interaction.followup.send(f"❌ 測定失敗: {e}")

    @discord.ui.button(label="📰 フェイクニュース", style=discord.ButtonStyle.secondary, row=2)
    async def btn_news(self, interaction: discord.Interaction, button: discord.ui.Button):
        members = [m for m in interaction.guild.members if not m.bot]
        if len(members) < 2:
            await interaction.response.send_message("❌ メンバーが足りないよ！")
            return
        await interaction.response.defer(thinking=True)
        ai_text = None
        if GEMINI_API_KEY:
            history = await scan_messages(interaction.guild)
            names = [m.display_name for m in members]
            ai_text = await generate_ai_news(history, names)
        if ai_text:
            embed = discord.Embed(title="📰 速報 — 今北Bot通信社", description=ai_text, color=discord.Color.yellow())
            embed.set_footer(text="※ AIがチャット履歴を学習して生成したフィクションです")
        else:
            count = random.randint(2, 3)
            items = random.sample(NEWS_TEMPLATES, min(count, len(NEWS_TEMPLATES)))
            icons = ["📰", "⚡", "🔥", "💥", "🚨"]
            lines = [f"{icons[i % len(icons)]} {fill_template(t, members)}" for i, t in enumerate(items)]
            embed = discord.Embed(title="📰 速報 — 今北Bot通信社", description="\n\n".join(lines), color=discord.Color.yellow())
            embed.set_footer(text="※ この記事はフィクションです")
        await interaction.followup.send(embed=embed)


class Panel(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.command()
    async def panel(self, ctx):
        embed = discord.Embed(
            title="🎮 Bot コントロールパネル",
            description="ボタンを押して機能を使ってね！\n（パネルは10分間有効）",
            color=discord.Color.blurple(),
        )
        await ctx.send(embed=embed, view=PanelView())


async def setup(bot):
    await bot.add_cog(Panel(bot))
