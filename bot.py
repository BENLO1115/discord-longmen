import discord
from discord import app_commands
import aiosqlite
import random
import json
from datetime import date
import os
from itertools import combinations
from dotenv import load_dotenv

load_dotenv()

# ── Constants ─────────────────────────────────────────────────────────────────

SUITS = ['♠', '♥', '♦', '♣']
RANKS = ['A', '2', '3', '4', '5', '6', '7', '8', '9', '10', 'J', 'Q', 'K']
VALUES = {r: i + 1 for i, r in enumerate(RANKS)}
DB = 'longmen.db'
INITIAL_CHIPS = 1000
DAILY_CHIPS = 200

SHOP_ITEMS = {
    '傳奇至尊寶': {'price': 500000, 'emoji': '🌟', 'desc': '傳說中的至高存在，萬年一遇', 'color': 0xFF0000},
    '賭神':       {'price': 100000, 'emoji': '👑', 'desc': '至高無上的傳奇稱號',         'color': 0xFF4500},
    '賭聖':       {'price': 40000,  'emoji': '🔱', 'desc': '萬中選一的賭場聖者',         'color': 0x9932CC},
    '賭王':       {'price': 30000,  'emoji': '💠', 'desc': '稱霸賭場的王者',             'color': 0x4169E1},
    '賭鬼':       {'price': 20000,  'emoji': '👹', 'desc': '為賭而生的瘋狂存在',         'color': 0xDC143C},
    '幸運星':     {'price': 5000,   'emoji': '⭐', 'desc': '天生好運的象徵',             'color': 0xFFD700},
    '賭場常客':   {'price': 2000,   'emoji': '🎰', 'desc': '常駐賭場的老鳥稱號',         'color': 0xFFA500},
    '小賭怡情':   {'price': 1000,   'emoji': '🎲', 'desc': '小賭一下，開心就好',         'color': 0x00BFFF},
    '破產王':     {'price': 500,    'emoji': '💸', 'desc': '輸光過的勇者稱號',           'color': 0x808080},
    '小可愛':     {'price': 200,    'emoji': '🍬', 'desc': '超級可愛的入門稱號',         'color': 0xFF69B4},
}


SLOTS = ['🍒', '🍋', '🍊', '🍇', '💎', '7️⃣']
SLOT_WEIGHTS = [40, 30, 20, 15, 10, 5]

intents = discord.Intents.default()
bot = discord.Client(intents=intents)
tree = app_commands.CommandTree(bot)

active_games = {}  # user_id -> (card1, card2)

# ── Helpers ───────────────────────────────────────────────────────────────────

def draw_card():
    return (random.choice(SUITS), random.choice(RANKS))

def card_str(c):
    return c[0] + c[1]

def card_val(c):
    return VALUES[c[1]]

def face_val(c):
    v = VALUES[c[1]]
    return 10 if v > 10 else v  # J/Q/K → 10

def bj_total(cards):
    total = sum(face_val(c) for c in cards)
    aces = sum(1 for c in cards if c[1] == 'A')
    for _ in range(aces):
        if total + 10 <= 21:
            total += 10
    return total

def calc_niu(cards):
    best = -1
    for idxs in combinations(range(5), 3):
        if sum(face_val(cards[i]) for i in idxs) % 10 == 0:
            s2 = sum(face_val(cards[i]) for i in range(5) if i not in idxs) % 10
            best = max(best, 10 if s2 == 0 else s2)
    return best  # -1=沒牛, 1-9=牛N, 10=牛牛

def niu_name(n):
    if n == -1: return '沒牛'
    if n == 10: return '牛牛'
    return f'牛{n}'

async def get_or_create_title_role(guild: discord.Guild, name: str) -> discord.Role | None:
    role = discord.utils.get(guild.roles, name=name)
    if not role:
        data = SHOP_ITEMS.get(name, {})
        try:
            role = await guild.create_role(
                name=name,
                color=discord.Color(data.get('color', 0x99AAB5)),
                hoist=True,
                reason='射龍門 Bot 自動建立稱號身分組'
            )
            bot_member = guild.get_member(bot.user.id)
            if bot_member and bot_member.top_role.position > 1:
                target_pos = max(bot_member.top_role.position - 1, 1)
                try:
                    await role.edit(position=target_pos)
                except (discord.Forbidden, discord.HTTPException):
                    pass
        except discord.Forbidden:
            return None
    return role

async def update_title_role(member: discord.Member, new_title: str):
    title_roles = [r for r in member.roles if r.name in SHOP_ITEMS]
    try:
        if title_roles:
            await member.remove_roles(*title_roles, reason='切換稱號')
        if new_title:
            role = await get_or_create_title_role(member.guild, new_title)
            if role:
                await member.add_roles(role, reason=f'裝備稱號：{new_title}')
    except discord.Forbidden:
        pass
    # 同步更新暱稱顯示稱號文字
    base = member.display_name
    if '【' in base:
        base = base[:base.index('【')].strip()
    new_nick = f'{base} 【{new_title}】' if new_title else base
    try:
        await member.edit(nick=new_nick)
    except discord.Forbidden:
        pass

def detect_niu_special(cards):
    """Returns ('鐵支',8) / ('同花順',5) / None"""
    ranks = [c[1] for c in cards]
    suits = [c[0] for c in cards]
    vals = sorted(VALUES[r] for r in ranks)
    counts = {}
    for r in ranks:
        counts[r] = counts.get(r, 0) + 1
    if max(counts.values()) >= 4:
        return ('鐵支', 8)
    same_suit = len(set(suits)) == 1
    is_seq = all(vals[i+1] - vals[i] == 1 for i in range(4))
    if not is_seq and vals == [1, 10, 11, 12, 13]:
        is_seq = True
    if same_suit and is_seq:
        return ('同花順', 5)
    return None

def spin_slots():
    return random.choices(SLOTS, weights=SLOT_WEIGHTS, k=3)

def calc_slot_delta(reels, bet):
    a, b, c = reels
    if a == b == c:
        if a == '7️⃣':  return bet * 50, 'JACKPOT！三個 7！'
        if a == '💎':   return bet * 20, '三顆鑽石！'
        return bet * 5, f'三個 {a}！'
    if a == b or b == c or a == c:
        pair = a if (a == b or a == c) else b
        if pair == '7️⃣': return bet * 3, '兩個 7！'
        if pair == '💎':  return bet * 2, '兩顆鑽石！'
        return 0, f'兩個 {pair}，平手。'
    return -bet, '沒有組合，本次落空。'

# ── Database ──────────────────────────────────────────────────────────────────

async def init_db():
    async with aiosqlite.connect(DB) as db:
        await db.execute('''CREATE TABLE IF NOT EXISTS users (
            user_id      TEXT PRIMARY KEY,
            chips        INTEGER DEFAULT 1000,
            last_checkin TEXT,
            title        TEXT DEFAULT NULL,
            owned_items  TEXT DEFAULT \'[]\'
        )''')
        for col, dflt in [('title', 'NULL'), ('owned_items', "\'[]\'")]:
            try:
                await db.execute(f'ALTER TABLE users ADD COLUMN {col} TEXT DEFAULT {dflt}')
            except Exception:
                pass
        await db.commit()

async def ensure_user(user_id: str):
    async with aiosqlite.connect(DB) as db:
        async with db.execute('SELECT 1 FROM users WHERE user_id=?', (user_id,)) as cur:
            if not await cur.fetchone():
                await db.execute(
                    "INSERT INTO users (user_id,chips,last_checkin,title,owned_items) VALUES (?,?,NULL,NULL,'[]')",
                    (user_id, INITIAL_CHIPS)
                )
                await db.commit()

async def get_chips(user_id: str) -> int:
    await ensure_user(user_id)
    async with aiosqlite.connect(DB) as db:
        async with db.execute('SELECT chips FROM users WHERE user_id=?', (user_id,)) as cur:
            row = await cur.fetchone()
    return row[0]

async def add_chips(user_id: str, delta: int):
    await ensure_user(user_id)
    async with aiosqlite.connect(DB) as db:
        await db.execute('UPDATE users SET chips=chips+? WHERE user_id=?', (delta, user_id))
        await db.commit()

async def transfer_chips(from_id: str, to_id: str, amount: int) -> bool:
    await ensure_user(from_id)
    await ensure_user(to_id)
    async with aiosqlite.connect(DB) as db:
        async with db.execute('SELECT chips FROM users WHERE user_id=?', (from_id,)) as cur:
            row = await cur.fetchone()
        if not row or row[0] < amount:
            return False
        await db.execute('UPDATE users SET chips=chips-? WHERE user_id=?', (amount, from_id))
        await db.execute('UPDATE users SET chips=chips+? WHERE user_id=?', (amount, to_id))
        await db.commit()
    return True

async def get_title(user_id: str):
    await ensure_user(user_id)
    async with aiosqlite.connect(DB) as db:
        async with db.execute('SELECT title FROM users WHERE user_id=?', (user_id,)) as cur:
            row = await cur.fetchone()
    return row[0] if row else None

async def set_title(user_id: str, title):
    async with aiosqlite.connect(DB) as db:
        await db.execute('UPDATE users SET title=? WHERE user_id=?', (title, user_id))
        await db.commit()

async def get_owned_items(user_id: str) -> list:
    await ensure_user(user_id)
    async with aiosqlite.connect(DB) as db:
        async with db.execute('SELECT owned_items FROM users WHERE user_id=?', (user_id,)) as cur:
            row = await cur.fetchone()
    return json.loads(row[0] or '[]') if row else []

async def add_owned_item(user_id: str, item: str):
    items = await get_owned_items(user_id)
    if item not in items:
        items.append(item)
    async with aiosqlite.connect(DB) as db:
        await db.execute(
            'UPDATE users SET owned_items=? WHERE user_id=?',
            (json.dumps(items, ensure_ascii=False), user_id)
        )
        await db.commit()

# ── Views ─────────────────────────────────────────────────────────────────────

class BetView(discord.ui.View):
    def __init__(self, user_id: int, card1, card2, chips: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.card1 = card1
        self.card2 = card2
        self.chips = chips
        self.done = False

    async def resolve(self, interaction: discord.Interaction, bet: int):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('這不是你的牌局！', ephemeral=True)
            return
        if self.done:
            await interaction.response.send_message('已押注過了！', ephemeral=True)
            return
        self.done = True
        for item in self.children:
            item.disabled = True

        card3 = draw_card()
        v1, v2, v3 = card_val(self.card1), card_val(self.card2), card_val(card3)
        lo, hi = min(v1, v2), max(v1, v2)

        if v3 == lo or v3 == hi:
            loss = bet // 2
            await add_chips(str(self.user_id), -loss)
            result = f'🎯 射中龍門！輸了 **{loss}** 籌碼'
            color = discord.Color.orange()
            final = self.chips - loss
        elif lo < v3 < hi:
            await add_chips(str(self.user_id), bet)
            result = f'✅ 過龍門！贏了 **{bet}** 籌碼'
            color = discord.Color.green()
            final = self.chips + bet
        else:
            await add_chips(str(self.user_id), -bet)
            result = f'❌ 沒過！輸了 **{bet}** 籌碼'
            color = discord.Color.red()
            final = self.chips - bet

        active_games.pop(self.user_id, None)

        embed = discord.Embed(title='🃏 射龍門 — 結果', color=color)
        embed.add_field(name='左牌', value=f'`{card_str(self.card1)}`', inline=True)
        embed.add_field(name='中牌', value=f'`{card_str(card3)}`', inline=True)
        embed.add_field(name='右牌', value=f'`{card_str(self.card2)}`', inline=True)
        embed.add_field(name='結果', value=result, inline=False)
        embed.add_field(name='剩餘籌碼', value=f'**{final:,}** 點', inline=False)
        await interaction.response.edit_message(embed=embed, view=PlayAgainView(self.user_id))

    @discord.ui.button(label='押 50',  style=discord.ButtonStyle.primary)
    async def bet_50(self, i, b):  await self.resolve(i, min(50, self.chips))

    @discord.ui.button(label='押 100', style=discord.ButtonStyle.primary)
    async def bet_100(self, i, b): await self.resolve(i, min(100, self.chips))

    @discord.ui.button(label='押 200', style=discord.ButtonStyle.primary)
    async def bet_200(self, i, b): await self.resolve(i, min(200, self.chips))

    @discord.ui.button(label='押 300', style=discord.ButtonStyle.primary)
    async def bet_300(self, i, b): await self.resolve(i, min(300, self.chips))

    @discord.ui.button(label='押 400', style=discord.ButtonStyle.primary)
    async def bet_400(self, i, b): await self.resolve(i, min(400, self.chips))

    @discord.ui.button(label='🔥 ALL IN', style=discord.ButtonStyle.danger)
    async def bet_allin(self, i, b): await self.resolve(i, self.chips)

    @discord.ui.button(label='不押（放棄）', style=discord.ButtonStyle.secondary)
    async def bet_skip(self, interaction: discord.Interaction, button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('這不是你的牌局！', ephemeral=True)
            return
        if self.done:
            return
        self.done = True
        for item in self.children:
            item.disabled = True
        active_games.pop(self.user_id, None)
        await interaction.response.edit_message(content='已放棄本局。', embed=None, view=PlayAgainView(self.user_id))

    async def on_timeout(self):
        active_games.pop(self.user_id, None)


class PlayAgainView(discord.ui.View):
    def __init__(self, user_id: int):
        super().__init__(timeout=120)
        self.user_id = user_id

    @discord.ui.button(label='再來一局 🃏', style=discord.ButtonStyle.success)
    async def play_again(self, interaction: discord.Interaction, button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('這不是你的牌局！', ephemeral=True)
            return
        if self.user_id in active_games:
            await interaction.response.send_message('你已有進行中的牌局！', ephemeral=True)
            return

        chips = await get_chips(str(self.user_id))
        if chips <= 0:
            await interaction.response.send_message('籌碼歸零！先用 `/簽到` 補充籌碼。', ephemeral=True)
            return

        c1, c2 = draw_card(), draw_card()
        active_games[self.user_id] = (c1, c2)
        v1, v2 = card_val(c1), card_val(c2)
        spread = abs(v1 - v2) - 1

        embed = discord.Embed(title='🃏 射龍門', description='猜猜中間那張牌是否落在兩張之間？', color=discord.Color.gold())
        embed.add_field(name='左牌', value=f'`{card_str(c1)}`', inline=True)
        embed.add_field(name='中牌', value='`  ?  `', inline=True)
        embed.add_field(name='右牌', value=f'`{card_str(c2)}`', inline=True)
        embed.add_field(name='可過牌數', value=f'{max(spread, 0)} 張', inline=True)
        embed.add_field(name='你的籌碼', value=f'**{chips:,}** 點', inline=True)
        embed.set_footer(text='射中龍門（等於邊牌）只輸一半 ｜ 60 秒未押注自動取消')
        self.stop()
        await interaction.response.edit_message(content=None, embed=embed, view=BetView(self.user_id, c1, c2, chips))


class GuessView(discord.ui.View):
    def __init__(self, user_id: int, bet: int, chips: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.bet = bet
        self.chips = chips
        self.done = False

    async def resolve(self, interaction: discord.Interaction, guess_big: bool):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('這不是你的猜牌！', ephemeral=True)
            return
        if self.done:
            return
        self.done = True
        for item in self.children:
            item.disabled = True

        card = draw_card()
        v = card_val(card)
        is_big = v >= 8

        if guess_big == is_big:
            await add_chips(str(self.user_id), self.bet)
            result = f'✅ 猜對了！贏了 **{self.bet:,}** 籌碼'
            color = discord.Color.green()
            final = self.chips + self.bet
        else:
            await add_chips(str(self.user_id), -self.bet)
            result = f'❌ 猜錯了！輸了 **{self.bet:,}** 籌碼'
            color = discord.Color.red()
            final = self.chips - self.bet

        embed = discord.Embed(title='🎴 猜大小 — 結果', color=color)
        embed.add_field(name='翻出的牌', value=f'`{card_str(card)}`', inline=True)
        embed.add_field(name='大小', value='大 (8~K)' if is_big else '小 (A~7)', inline=True)
        embed.add_field(name='你猜', value='大' if guess_big else '小', inline=True)
        embed.add_field(name='結果', value=result, inline=False)
        embed.add_field(name='剩餘籌碼', value=f'**{final:,}** 點', inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='大 (8~K)', style=discord.ButtonStyle.danger)
    async def guess_big(self, i, b):   await self.resolve(i, True)

    @discord.ui.button(label='小 (A~7)', style=discord.ButtonStyle.primary)
    async def guess_small(self, i, b): await self.resolve(i, False)


class ShopView(discord.ui.View):
    def __init__(self, user_id: int, chips: int, owned: list):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.chips = chips
        self.owned = owned
        for name, data in SHOP_ITEMS.items():
            already = name in owned
            prefix = '已擁有' if already else f"{data['emoji']} {name}"
            btn = discord.ui.Button(
                label=f"{prefix} — {data['price']:,} 籌碼",
                style=discord.ButtonStyle.secondary if already else discord.ButtonStyle.primary,
                disabled=already or chips < data['price'],
                custom_id=f'buy_{name}'
            )
            btn.callback = self._make_buy(name, data)
            self.add_item(btn)

    def _make_buy(self, name, data):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message('這不是你的商店！', ephemeral=True)
                return
            chips = await get_chips(str(self.user_id))
            if chips < data['price']:
                await interaction.response.send_message('籌碼不足！', ephemeral=True)
                return
            await add_chips(str(self.user_id), -data['price'])
            await add_owned_item(str(self.user_id), name)
            await set_title(str(self.user_id), name)
            if interaction.guild:
                member = interaction.guild.get_member(self.user_id)
                if member:
                    await update_title_role(member, name)
            for item in self.children:
                item.disabled = True
            embed = discord.Embed(
                title='🛒 購買成功！',
                description=f"已購入 **{data['emoji']} {name}** 並自動裝備為稱號！\n剩餘籌碼：**{chips - data['price']:,}** 點",
                color=discord.Color.green()
            )
            await interaction.response.edit_message(embed=embed, view=self)
        return callback

class BlackjackPlayView(discord.ui.View):
    def __init__(self, user_id: int, bet: int, chips: int, player: list, dealer: list):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.bet = bet
        self.chips = chips
        self.player = player
        self.dealer = dealer
        self.done = False

    def make_embed(self):
        pv = bj_total(self.player)
        embed = discord.Embed(title='🃏 21點', color=discord.Color.dark_green())
        embed.add_field(name=f'你的牌 ({pv})', value=' '.join(f'`{card_str(c)}`' for c in self.player), inline=False)
        embed.add_field(name='莊家的牌', value=f'`{card_str(self.dealer[0])}`  `  ?  `', inline=False)
        embed.add_field(name='押注', value=f'**{self.bet:,}** 籌碼', inline=True)
        return embed

    async def finish(self, interaction: discord.Interaction, bust=False):
        self.done = True
        for item in self.children:
            item.disabled = True
        pv = bj_total(self.player)
        while bj_total(self.dealer) < 18:
            self.dealer.append(draw_card())
        dv = bj_total(self.dealer)
        if bust or (dv <= 21 and pv < dv):
            delta = -self.bet
            result = f'❌ {"爆牌！" if bust else "莊家贏！"}輸了 **{self.bet:,}** 籌碼'
            color = discord.Color.red()
        elif pv == dv:
            delta = 0
            result = '🤝 平局！'
            color = discord.Color.blue()
        else:
            is_bj = pv == 21 and len(self.player) == 2
            delta = int(self.bet * 1.5) if is_bj else self.bet
            result = f'{"🎉 Blackjack！" if is_bj else "✅ 你贏了！"}贏了 **{delta:,}** 籌碼'
            color = discord.Color.green()
        await add_chips(str(self.user_id), delta)
        final = self.chips + delta
        embed = discord.Embed(title='🃏 21點 — 結果', color=color)
        embed.add_field(name=f'你的牌 ({pv})', value=' '.join(f'`{card_str(c)}`' for c in self.player), inline=False)
        embed.add_field(name=f'莊家的牌 ({dv})', value=' '.join(f'`{card_str(c)}`' for c in self.dealer), inline=False)
        embed.add_field(name='結果', value=result, inline=False)
        embed.add_field(name='剩餘籌碼', value=f'**{final:,}** 點', inline=False)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label='要牌 Hit', style=discord.ButtonStyle.primary)
    async def hit(self, interaction: discord.Interaction, button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('這不是你的牌局！', ephemeral=True)
            return
        if self.done:
            return
        self.player.append(draw_card())
        pv = bj_total(self.player)
        if pv > 21:
            await self.finish(interaction, bust=True)
        elif pv == 21:
            await self.finish(interaction)
        else:
            await interaction.response.edit_message(embed=self.make_embed(), view=self)

    @discord.ui.button(label='停牌 Stand', style=discord.ButtonStyle.secondary)
    async def stand(self, interaction: discord.Interaction, button):
        if interaction.user.id != self.user_id:
            await interaction.response.send_message('這不是你的牌局！', ephemeral=True)
            return
        if self.done:
            return
        await self.finish(interaction)


class BlackjackBetView(discord.ui.View):
    def __init__(self, user_id: int, chips: int, player: list, dealer: list):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.chips = chips
        self.player = player
        self.dealer = dealer
        self.done = False
        for label, amt in [('押 50',50),('押 100',100),('押 200',200),('押 300',300),('押 400',400)]:
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, disabled=chips < amt)
            btn.callback = self._make_bet(min(amt, chips))
            self.add_item(btn)
        allin = discord.ui.Button(label='🔥 ALL IN', style=discord.ButtonStyle.danger)
        allin.callback = self._make_bet(chips)
        self.add_item(allin)

    def _make_bet(self, amount):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message('這不是你的牌局！', ephemeral=True)
                return
            if self.done:
                return
            self.done = True
            for item in self.children:
                item.disabled = True
            play = BlackjackPlayView(self.user_id, amount, self.chips, self.player, self.dealer)
            await interaction.response.edit_message(embed=play.make_embed(), view=play)
        return callback


class NiuView(discord.ui.View):
    def __init__(self, user_id: int, chips: int):
        super().__init__(timeout=60)
        self.user_id = user_id
        self.chips = chips
        self.done = False
        for label, amt in [('押 50',50),('押 100',100),('押 200',200),('押 300',300),('押 400',400)]:
            btn = discord.ui.Button(label=label, style=discord.ButtonStyle.primary, disabled=chips < amt)
            btn.callback = self._make_bet(min(amt, chips))
            self.add_item(btn)
        allin = discord.ui.Button(label='🔥 ALL IN', style=discord.ButtonStyle.danger)
        allin.callback = self._make_bet(chips)
        self.add_item(allin)

    def _make_bet(self, amount):
        async def callback(interaction: discord.Interaction):
            if interaction.user.id != self.user_id:
                await interaction.response.send_message('這不是你的牌局！', ephemeral=True)
                return
            if self.done:
                return
            self.done = True
            for item in self.children:
                item.disabled = True

            player = [draw_card() for _ in range(5)]
            dealer = [draw_card() for _ in range(5)]
            p_sp = detect_niu_special(player)
            d_sp = detect_niu_special(dealer)
            p_niu = calc_niu(player)
            d_niu = calc_niu(dealer)

            def hand_rank(sp, niu):
                if sp and sp[0] == '鐵支':   return 20
                if sp and sp[0] == '同花順': return 15
                return niu

            p_rank = hand_rank(p_sp, p_niu)
            d_rank = hand_rank(d_sp, d_niu)

            if p_rank > d_rank:
                if p_sp:               mult = p_sp[1]
                elif p_niu in (8, 9):  mult = 2
                elif p_niu == 10:      mult = 3
                else:                  mult = 1
                delta = amount * mult
                p_label = f"🎴 {p_sp[0]}" if p_sp else niu_name(p_niu)
                result = f'✅ **{p_label}**！贏了 **{delta:,}** 籌碼！'
                color = discord.Color.green()
            elif p_rank == d_rank:
                delta = 0
                result = '🤝 平局！'
                color = discord.Color.blue()
            else:
                delta = -amount
                d_label = f"🎴 {d_sp[0]}" if d_sp else niu_name(d_niu)
                result = f'❌ 輸給莊家 **{d_label}**！輸了 **{amount:,}** 籌碼'
                color = discord.Color.red()

            await add_chips(str(self.user_id), delta)
            final = self.chips + delta
            p_label = f"🎴 {p_sp[0]}" if p_sp else niu_name(p_niu)
            d_label = f"🎴 {d_sp[0]}" if d_sp else niu_name(d_niu)
            embed = discord.Embed(title='🀄 妞妞 — 結果', color=color)
            embed.add_field(name=f'你的牌 — {p_label}', value=' '.join(f'`{card_str(c)}`' for c in player), inline=False)
            embed.add_field(name=f'莊家的牌 — {d_label}', value=' '.join(f'`{card_str(c)}`' for c in dealer), inline=False)
            embed.add_field(name='結果', value=result, inline=False)
            embed.add_field(name='剩餘籌碼', value=f'**{final:,}** 點', inline=False)
            await interaction.response.edit_message(embed=embed, view=self)
        return callback


# ── Commands ───────────────────────────────────────────────────────────────────

@tree.command(name='射龍門', description='開始一局射龍門紙牌遊戲')
async def cmd_longmen(interaction: discord.Interaction):
    uid = interaction.user.id
    if uid in active_games:
        await interaction.response.send_message('你已有進行中的牌局！', ephemeral=True)
        return

    chips = await get_chips(str(uid))
    if chips <= 0:
        await interaction.response.send_message('籌碼歸零！先用 `/簽到` 補充籌碼。', ephemeral=True)
        return

    c1, c2 = draw_card(), draw_card()
    active_games[uid] = (c1, c2)
    v1, v2 = card_val(c1), card_val(c2)
    spread = abs(v1 - v2) - 1

    embed = discord.Embed(title='🃏 射龍門', description='猜猜中間那張牌是否落在兩張之間？', color=discord.Color.gold())
    embed.add_field(name='左牌', value=f'`{card_str(c1)}`', inline=True)
    embed.add_field(name='中牌', value='`  ?  `', inline=True)
    embed.add_field(name='右牌', value=f'`{card_str(c2)}`', inline=True)
    embed.add_field(name='可過牌數', value=f'{max(spread, 0)} 張', inline=True)
    embed.add_field(name='你的籌碼', value=f'**{chips:,}** 點', inline=True)
    embed.set_footer(text='射中龍門（等於邊牌）只輸一半 ｜ 60 秒未押注自動取消')
    await interaction.response.send_message(embed=embed, view=BetView(uid, c1, c2, chips))


@tree.command(name='猜大小', description='猜牌面大小（8~K為大，A~7為小），贏賠 1:1')
@app_commands.describe(下注='押注的籌碼數量')
async def cmd_guess(interaction: discord.Interaction, 下注: int):
    uid = interaction.user.id
    if 下注 <= 0:
        await interaction.response.send_message('下注金額必須大於 0！', ephemeral=True)
        return
    chips = await get_chips(str(uid))
    if chips <= 0:
        await interaction.response.send_message('籌碼歸零！先用 `/簽到` 補充籌碼。', ephemeral=True)
        return
    bet = min(下注, chips)
    embed = discord.Embed(
        title='🎴 猜大小',
        description=f'押注 **{bet:,}** 籌碼，牌面是大還是小？\n大 = 8 ~ K　｜　小 = A ~ 7',
        color=discord.Color.blue()
    )
    embed.add_field(name='你的籌碼', value=f'**{chips:,}** 點')
    await interaction.response.send_message(embed=embed, view=GuessView(uid, bet, chips))


@tree.command(name='拉霸', description='拉霸機！三個相同符號贏大獎，JACKPOT 賠 50 倍')
@app_commands.describe(下注='押注的籌碼數量')
async def cmd_slot(interaction: discord.Interaction, 下注: int):
    uid = str(interaction.user.id)
    if 下注 <= 0:
        await interaction.response.send_message('下注金額必須大於 0！', ephemeral=True)
        return
    chips = await get_chips(uid)
    if chips <= 0:
        await interaction.response.send_message('籌碼歸零！先用 `/簽到` 補充籌碼。', ephemeral=True)
        return

    bet = min(下注, chips)
    reels = spin_slots()
    delta, desc = calc_slot_delta(reels, bet)
    await add_chips(uid, delta)
    final = chips + delta

    if delta >= bet * 10:  color = discord.Color.gold()
    elif delta > 0:        color = discord.Color.green()
    elif delta == 0:       color = discord.Color.blue()
    else:                  color = discord.Color.red()

    embed = discord.Embed(title='🎰 拉霸機', color=color)
    embed.add_field(name='轉輪', value=f'[ {reels[0]}  {reels[1]}  {reels[2]} ]', inline=False)
    embed.add_field(name='結果', value=desc, inline=False)
    change = f'+**{delta:,}**' if delta > 0 else (f'**{delta:,}**' if delta < 0 else '**±0**')
    embed.add_field(name='籌碼變動', value=change, inline=True)
    embed.add_field(name='剩餘籌碼', value=f'**{final:,}** 點', inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name='21點', description='21點！越接近 21 不爆牌就贏，Blackjack 賠 1.5 倍')
async def cmd_blackjack(interaction: discord.Interaction):
    uid = interaction.user.id
    chips = await get_chips(str(uid))
    if chips <= 0:
        await interaction.response.send_message('籌碼歸零！先用 `/簽到` 補充籌碼。', ephemeral=True)
        return
    player = [draw_card(), draw_card()]
    dealer = [draw_card(), draw_card()]
    pv = bj_total(player)
    embed = discord.Embed(title='🃏 21點', color=discord.Color.dark_green())
    embed.add_field(name=f'你的牌 ({pv})', value=' '.join(f'`{card_str(c)}`' for c in player), inline=False)
    embed.add_field(name='莊家的牌', value=f'`{card_str(dealer[0])}`  `  ?  `', inline=False)
    embed.add_field(name='你的籌碼', value=f'**{chips:,}** 點', inline=True)
    embed.set_footer(text='Blackjack（兩張牌21點）賠 1.5 倍 ｜ 莊家 18 點停牌')
    await interaction.response.send_message(embed=embed, view=BlackjackBetView(uid, chips, player, dealer))


@tree.command(name='妞妞', description='妞妞！先下注才能看牌，牛牛 ×3 倍，鐵支 ×8 倍，同花順 ×5 倍！')
async def cmd_niu(interaction: discord.Interaction):
    uid = interaction.user.id
    chips = await get_chips(str(uid))
    if chips <= 0:
        await interaction.response.send_message('籌碼歸零！先用 `/簽到` 補充籌碼。', ephemeral=True)
        return
    embed = discord.Embed(
        title='🀄 妞妞',
        description='先下注，押完才能看牌！\n牛8/牛9 ×2 ｜ 牛牛 ×3 ｜ 同花順 ×5 ｜ 鐵支 ×8',
        color=discord.Color.dark_gold()
    )
    embed.add_field(name='你的籌碼', value=f'**{chips:,}** 點', inline=True)
    await interaction.response.send_message(embed=embed, view=NiuView(uid, chips))


@tree.command(name='轉帳', description='轉帳籌碼給其他玩家')
@app_commands.describe(對象='收款的 Discord 用戶', 金額='轉帳籌碼數量')
async def cmd_transfer(interaction: discord.Interaction, 對象: discord.User, 金額: int):
    if 金額 <= 0:
        await interaction.response.send_message('金額必須大於 0！', ephemeral=True)
        return
    if 對象.id == interaction.user.id:
        await interaction.response.send_message('不能轉帳給自己！', ephemeral=True)
        return
    if 對象.bot:
        await interaction.response.send_message('不能轉帳給機器人！', ephemeral=True)
        return
    success = await transfer_chips(str(interaction.user.id), str(對象.id), 金額)
    if not success:
        await interaction.response.send_message('籌碼不足！', ephemeral=True)
        return
    embed = discord.Embed(title='💸 轉帳成功', color=discord.Color.green())
    embed.add_field(name='發送方', value=interaction.user.display_name, inline=True)
    embed.add_field(name='收款方', value=對象.display_name, inline=True)
    embed.add_field(name='金額', value=f'**{金額:,}** 籌碼', inline=True)
    await interaction.response.send_message(embed=embed)


@tree.command(name='商店', description='用籌碼購買專屬稱號')
async def cmd_shop(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    chips = await get_chips(uid)
    owned = await get_owned_items(uid)
    embed = discord.Embed(title='🛒 商店', description='購買稱號後會顯示在排行榜與籌碼查詢中！', color=discord.Color.purple())
    for name, data in SHOP_ITEMS.items():
        status = '✅ 已擁有' if name in owned else ('❌ 籌碼不足' if chips < data['price'] else '🛒 可購買')
        embed.add_field(
            name=f"{data['emoji']} {name}",
            value=f"{data['desc']}\n價格：**{data['price']:,}** 籌碼 ｜ {status}",
            inline=False
        )
    embed.set_footer(text=f'你的籌碼：{chips:,} 點')
    await interaction.response.send_message(embed=embed, view=ShopView(interaction.user.id, chips, owned), ephemeral=True)


@tree.command(name='我的物品', description='查看已擁有的稱號')
async def cmd_myitems(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    owned = await get_owned_items(uid)
    title = await get_title(uid)
    if not owned:
        await interaction.response.send_message('你還沒有任何稱號！去 `/商店` 購買吧。', ephemeral=True)
        return
    embed = discord.Embed(title='🎒 我的物品', color=discord.Color.blurple())
    for name in owned:
        data = SHOP_ITEMS.get(name, {})
        tag = ' ⚡裝備中' if name == title else ''
        embed.add_field(name=f"{data.get('emoji','🏷')} {name}{tag}", value=data.get('desc',''), inline=False)
    embed.set_footer(text='用 /裝備稱號 [名稱] 切換')
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='裝備稱號', description='從擁有的稱號中選擇裝備')
async def cmd_equip(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    owned = await get_owned_items(uid)
    if not owned:
        await interaction.response.send_message('你還沒有任何稱號！去 `/商店` 購買吧。', ephemeral=True)
        return
    current = await get_title(uid)

    options = [
        discord.SelectOption(
            label=name,
            emoji=SHOP_ITEMS.get(name, {}).get('emoji', '🏷'),
            description=SHOP_ITEMS.get(name, {}).get('desc', ''),
            default=(name == current)
        )
        for name in owned
    ]

    class EquipSelect(discord.ui.Select):
        def __init__(self_s):
            super().__init__(placeholder='選擇要裝備的稱號...', options=options, min_values=1, max_values=1)

        async def callback(self_s, inter: discord.Interaction):
            if inter.user.id != interaction.user.id:
                await inter.response.send_message('這不是你的選單！', ephemeral=True)
                return
            chosen = self_s.values[0]
            await set_title(uid, chosen)
            data = SHOP_ITEMS.get(chosen, {})
            if inter.guild:
                member = inter.guild.get_member(inter.user.id)
                if member:
                    await update_title_role(member, chosen)
            for item in self_s.view.children:
                item.disabled = True
            await inter.response.edit_message(content=f"✅ 已裝備 {data.get('emoji','')} **{chosen}**！身分組已更新。", view=self_s.view)

    view = discord.ui.View(timeout=60)
    view.add_item(EquipSelect())
    await interaction.response.send_message('選擇要裝備的稱號：', view=view, ephemeral=True)



@tree.command(name='籌碼', description='查看目前籌碼')
async def cmd_chips(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    chips = await get_chips(uid)
    title = await get_title(uid)
    title_str = f' 【{title}】' if title else ''
    embed = discord.Embed(title='💰 我的籌碼', color=discord.Color.gold())
    embed.add_field(name=interaction.user.display_name + title_str, value=f'**{chips:,}** 點')
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name='簽到', description='每日簽到領 200 籌碼')
async def cmd_checkin(interaction: discord.Interaction):
    uid = str(interaction.user.id)
    today = str(date.today())
    async with aiosqlite.connect(DB) as db:
        async with db.execute('SELECT last_checkin, chips FROM users WHERE user_id=?', (uid,)) as cur:
            row = await cur.fetchone()
        if not row:
            await db.execute(
                "INSERT INTO users (user_id,chips,last_checkin,title,owned_items) VALUES (?,?,?,NULL,'[]')",
                (uid, INITIAL_CHIPS + DAILY_CHIPS, today)
            )
            await db.commit()
            await interaction.response.send_message(f'🎉 歡迎新玩家！首次簽到送 **{INITIAL_CHIPS + DAILY_CHIPS:,}** 籌碼！')
            return
        last, chips = row
        if last == today:
            await interaction.response.send_message('今天已簽到過囉，明天再來！', ephemeral=True)
            return
        await db.execute('UPDATE users SET chips=chips+?, last_checkin=? WHERE user_id=?', (DAILY_CHIPS, today, uid))
        await db.commit()
    await interaction.response.send_message(f'✅ 簽到成功！+**{DAILY_CHIPS}** 籌碼，目前共 **{chips + DAILY_CHIPS:,}** 點')


@tree.command(name='排行榜', description='查看籌碼排行榜 Top 10')
async def cmd_rank(interaction: discord.Interaction):
    async with aiosqlite.connect(DB) as db:
        async with db.execute('SELECT user_id, chips, title FROM users ORDER BY chips DESC LIMIT 10') as cur:
            rows = await cur.fetchall()
    if not rows:
        await interaction.response.send_message('還沒有人玩過！')
        return
    medals = ['🥇', '🥈', '🥉']
    lines = []
    for i, (uid, chips, title) in enumerate(rows):
        medal = medals[i] if i < 3 else f'`{i+1}.`'
        try:
            user = await bot.fetch_user(int(uid))
            name = user.display_name
        except Exception:
            name = f'玩家#{uid[-4:]}'
        title_str = f' 【{title}】' if title else ''
        lines.append(f'{medal} {name}{title_str} — **{chips:,}** 點')
    embed = discord.Embed(title='🏆 籌碼排行榜', description='\n'.join(lines), color=discord.Color.gold())
    await interaction.response.send_message(embed=embed)


OWNER_ID = '843724534117040168'

@tree.command(name='管理員給予', description='（管理員限定）給予指定玩家物品或籌碼')
@app_commands.describe(目標='目標玩家', 稱號='要給予的稱號名稱（留空則不給）', 籌碼='要給予的籌碼數量（留空則不給）')
async def cmd_admin_give(interaction: discord.Interaction, 目標: discord.User, 稱號: str = None, 籌碼: int = 0):
    if str(interaction.user.id) != OWNER_ID:
        await interaction.response.send_message('你沒有權限使用此指令！', ephemeral=True)
        return
    uid = str(目標.id)
    msgs = []
    if 稱號:
        if 稱號 not in SHOP_ITEMS:
            await interaction.response.send_message(f'「{稱號}」不在商店清單中！', ephemeral=True)
            return
        await add_owned_item(uid, 稱號)
        await set_title(uid, 稱號)
        if interaction.guild:
            member = interaction.guild.get_member(目標.id)
            if member:
                await update_title_role(member, 稱號)
        msgs.append(f'已給予稱號 **{稱號}**')
    if 籌碼 > 0:
        await add_chips(uid, 籌碼)
        msgs.append(f'已給予 **{籌碼:,}** 籌碼')
    if not msgs:
        await interaction.response.send_message('請至少填寫稱號或籌碼！', ephemeral=True)
        return
    await interaction.response.send_message(f'✅ {目標.display_name}：{" ／ ".join(msgs)}', ephemeral=True)


# ── Boot ───────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await init_db()
    await tree.sync()
    for guild in bot.guilds:
        await _init_guild_roles(guild)
    await _setup_owner()
    print(f'Bot online: {bot.user}')

async def _init_guild_roles(guild: discord.Guild):
    for name in SHOP_ITEMS:
        await get_or_create_title_role(guild, name)

async def _setup_owner():
    owned = await get_owned_items(OWNER_ID)
    if '傳奇至尊寶' not in owned:
        await add_owned_item(OWNER_ID, '傳奇至尊寶')
    await set_title(OWNER_ID, '傳奇至尊寶')
    for guild in bot.guilds:
        member = guild.get_member(int(OWNER_ID))
        if member:
            await update_title_role(member, '傳奇至尊寶')


bot.run(os.getenv('DISCORD_TOKEN'))
