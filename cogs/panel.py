import random
import discord
from discord.ext import commands
from config import GEMINI_API_KEY
from cogs.apex import (
    APEX_LEGENDS, APEX_API_KEY,
    build_rankmap_embed, build_apexstatus_embed, build_apexstats_embed,
)
from cogs.fun import (
    NEWS_TEMPLATES,
    fill_template, scan_messages, generate_ai_news,
)
from cogs.utils import build_ping_embed


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

    @discord.ui.button(label="🌐 回線速度", style=discord.ButtonStyle.secondary, row=1)
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
