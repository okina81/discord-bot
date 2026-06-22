import re
import aiohttp
import discord
from discord.ext import commands

STAT_NAMES = {
    "hp": "HP",
    "attack": "こうげき",
    "defense": "ぼうぎょ",
    "special-attack": "とくこう",
    "special-defense": "とくぼう",
    "speed": "すばやさ",
}


class Pokemon(commands.Cog):
    def __init__(self, bot):
        self.bot = bot
        self.cache: dict[str, int] = {}
        self.pattern = None

    async def build_cache(self):
        graphql_url = "https://beta.pokeapi.co/graphql/v1beta"
        query = """{ pokemon_v2_pokemonspeciesname(where: {language_id: {_in: [1, 11]}}) { name pokemon_species_id } }"""
        async with aiohttp.ClientSession() as session:
            async with session.post(graphql_url, json={"query": query}) as resp:
                data = await resp.json()
        for item in data["data"]["pokemon_v2_pokemonspeciesname"]:
            self.cache[item["name"]] = item["pokemon_species_id"]
        names_sorted = sorted(self.cache.keys(), key=len, reverse=True)
        self.pattern = re.compile("|".join(re.escape(n) for n in names_sorted))
        print(f"ポケモンキャッシュ構築完了: {len(self.cache)}種")

    async def get_pokemon_stats(self, pokemon_id):
        async with aiohttp.ClientSession() as session:
            async with session.get(f"https://pokeapi.co/api/v2/pokemon/{pokemon_id}/") as resp:
                data = await resp.json()
            async with session.get(f"https://pokeapi.co/api/v2/pokemon-species/{pokemon_id}/") as resp:
                species = await resp.json()
        ja_name = next(
            (n["name"] for n in species["names"] if n["language"]["name"] == "ja"),
            data["name"]
        )
        stats = {STAT_NAMES[s["stat"]["name"]]: s["base_stat"]
                 for s in data["stats"] if s["stat"]["name"] in STAT_NAMES}
        return ja_name, data["id"], stats

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot or not self.pattern:
            return
        match = self.pattern.search(message.content)
        if not match:
            return
        pokemon_name = match.group()
        pokemon_id = self.cache[pokemon_name]
        try:
            ja_name, dex_id, stats = await self.get_pokemon_stats(pokemon_id)
            total = sum(stats.values())
            embed = discord.Embed(
                title=f"📊 {ja_name}（#{dex_id}）の種族値",
                color=discord.Color.gold(),
            )
            stats_text = "\n".join(f"{k}　{v}" for k, v in stats.items())
            embed.description = f"```{stats_text}\n─────────\n合計　　{total}```"
            embed.set_footer(text="出典: PokeAPI")
            await message.channel.send(embed=embed)
        except Exception:
            pass


async def setup(bot):
    cog = Pokemon(bot)
    await bot.add_cog(cog)
    bot.loop.create_task(cog.build_cache())
