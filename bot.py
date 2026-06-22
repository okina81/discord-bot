import asyncio
import discord
from discord.ext import commands
from config import TOKEN

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

EXTENSIONS = [
    "cogs.levels",
    "cogs.stats",
    "cogs.recruit",
    "cogs.pokemon",
    "cogs.apex",
    "cogs.fun",
    "cogs.utils",
    "cogs.panel",
    "cogs.responses",
]


@bot.event
async def on_ready():
    print(f"{bot.user} としてログインしました")


async def main():
    async with bot:
        for ext in EXTENSIONS:
            await bot.load_extension(ext)
        await bot.start(TOKEN)


asyncio.run(main())
