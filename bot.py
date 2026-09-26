import asyncio
import logging
import discord
from discord.ext import commands
from config import TOKEN

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")

intents = discord.Intents.default()
intents.message_content = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)

EXTENSIONS = [
    "cogs.recruit",
    "cogs.apex",
    "cogs.voice",
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
