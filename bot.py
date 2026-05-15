import discord
from discord import app_commands
import aiosqlite
import random
import json
from datetime import date
import os
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
    '賭神':     {'price': 10000, 'emoji': '👑', 'desc': '至高無上的賭場稱號'},
    '幸運星':   {'price': 5000,  'emoji': '⭐', 'desc': '天生好運的象徵'},
    '賭場常客': {'price': 2000,  'emoji': '🎰', 'desc': '常駐賭場的老鳥稱號'},
    '破產王':   {'price': 500,   'emoji': '💸', 'desc': '輸光過的勇者稱號'},
}

ROLE_MILESTONES = [
    (10000, '賭神'),
    (5000,  '賭神弟子'),
    (2000,  '賭場常客'),
    (500,   '新手賭徒'),
]

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
            base = interaction.user.display_name
            if '【' in base:
                base = base[:base.index('【')].strip()
            try:
                await interaction.user.edit(nick=f'{base} 【{name}】')
            except discord.Forbidden:
                pass
            for item in self.children:
                item.disabled = True
            embed = discord.Embed(
                title='🛒 購買成功！',
                description=f"已購入 **{data['emoji']} {name}** 並自動裝備為稱號！\n剩餘籌碼：**{chips - data['price']:,}** 點",
                color=discord.Color.green()
            )
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


@tree.command(name='裝備稱號', description='切換顯示的稱號')
@app_commands.describe(稱號='要裝備的稱號名稱')
async def cmd_equip(interaction: discord.Interaction, 稱號: str):
    uid = str(interaction.user.id)
    owned = await get_owned_items(uid)
    if 稱號 not in owned:
        await interaction.response.send_message(f'你沒有「{稱號}」這個稱號！', ephemeral=True)
        return
    await set_title(uid, 稱號)
    data = SHOP_ITEMS.get(稱號, {})
    base = interaction.user.display_name
    if '【' in base:
        base = base[:base.index('【')].strip()
    try:
        await interaction.user.edit(nick=f'{base} 【{稱號}】')
    except discord.Forbidden:
        pass
    await interaction.response.send_message(f"已裝備 {data.get('emoji','')} **{稱號}**！暱稱已更新。", ephemeral=True)


@tree.command(name='兌換身分組', description='依籌碼里程碑兌換伺服器身分組')
async def cmd_role(interaction: discord.Interaction):
    if not interaction.guild:
        await interaction.response.send_message('此指令只能在伺服器中使用！', ephemeral=True)
        return
    chips = await get_chips(str(interaction.user.id))
    earned_role = None
    for threshold, rname in ROLE_MILESTONES:
        if chips >= threshold:
            earned_role = rname
            break
    if not earned_role:
        need = ROLE_MILESTONES[-1][0]
        await interaction.response.send_message(f'還沒達標！最低需要 **{need:,}** 籌碼。', ephemeral=True)
        return

    role = discord.utils.get(interaction.guild.roles, name=earned_role)
    if not role:
        try:
            role = await interaction.guild.create_role(name=earned_role, reason='射龍門 Bot 自動建立')
        except discord.Forbidden:
            await interaction.response.send_message(
                f'Bot 缺少「管理身分組」權限，請管理員手動建立名為「{earned_role}」的身分組。', ephemeral=True)
            return
    try:
        await interaction.user.add_roles(role)
        await interaction.response.send_message(f'✅ 已獲得身分組 **{earned_role}**！（你有 {chips:,} 籌碼）')
    except discord.Forbidden:
        await interaction.response.send_message('Bot 缺少「管理身分組」權限！', ephemeral=True)


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


# ── Boot ───────────────────────────────────────────────────────────────────────

@bot.event
async def on_ready():
    await init_db()
    await tree.sync()
    print(f'Bot online: {bot.user}')


bot.run(os.getenv('DISCORD_TOKEN'))
