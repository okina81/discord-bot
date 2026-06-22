import random
import discord
from discord.ext import commands
from config import TARGET_USER_IDS


class Responses(commands.Cog):
    def __init__(self, bot):
        self.bot = bot

    @commands.Cog.listener()
    async def on_message(self, message):
        if message.author.bot:
            return

        if "x.com/" in message.content or "twitter.com/" in message.content:
            if random.random() < 1 / 3:
                await message.reply("おま、X依存症かよw")

        if message.author.id in TARGET_USER_IDS:
            if random.random() < 0.03:
                await message.reply("だぼが")

        if self.bot.user in message.mentions:
            await message.reply(random.choice([
                "マギーちゃんのビラビラおまんまん", "うおwww", "う、うおwww",
                "I love 高木", "いとこは禁句な？", "かわいいだけじゃ、ダメですか？",
                "いぇーーーーーい！！！",
            ]))

        if "コンジット" in message.content:
            await message.reply("コンジットはブス")

        if "パチンコで負け" in message.content:
            await message.reply(
                "…いや待って聞いて、確かに数字だけ見たらマイナスに見えるかもしれんけど、"
                "まず交通費と昼飯代除いたら実質もっと少ないし、そもそもあの日の大当たり映像今でも鮮明に覚えてるし"
                "精神的な充実度で言ったら絶対プラスなんよ…それにパチ屋で培った確率論の知識とか台の見極め方とか、"
                "そういうスキルって金に換算したらめちゃくちゃ価値あると思うし…あの日あの台さえ引き続けてなければ"
                "今頃全然違う結果になってたし、俺の立ち回り自体は間違ってないんよ、ただ台が悪かっただけで…"
                "あと出玉で飯食えた日も何回かあったし、そういうの全部トータルで計算したらほぼトントンやと思うし…たぶん…"
                "そもそも俺はパチンコに課金してるんじゃなくてエンタメに投資してる感覚やから、"
                "映画とかライブとか行くのと本質的には変わらんくて、むしろ安い日とかも普通にあるし…"
                "まだ終わってへんし、確率って長期で収束するもんやから今はまだ収束途中なだけで、"
                "あと少し続けたら絶対取り返せるし…ボソボソ"
            )


async def setup(bot):
    await bot.add_cog(Responses(bot))
