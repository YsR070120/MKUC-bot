import asyncio
import datetime
import os
import sqlite3
import aiohttp
from aiohttp import web
import discord
from discord import app_commands
from discord.ext import commands, tasks

# --- 設定 ---
TOKEN = os.getenv("DISCORD_TOKEN")  # Render等の環境変数から取得、または直接文字列で代入
PORT = int(os.getenv("PORT", 8080))

# データベースの初期化
db_conn = sqlite3.connect("mkuc_bot.db", check_same_thread=False)
db_cursor = db_conn.cursor()

db_cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS scores (
        role_id INTEGER PRIMARY KEY,
        points INTEGER DEFAULT 0
    )
"""
)
db_cursor.execute(
    """
    CREATE TABLE IF NOT EXISTS settings (
        key TEXT PRIMARY KEY,
        value TEXT
    )
"""
)
# 初期設定: お休み優先機能はデフォルトでON ("true")
db_cursor.execute(
    "INSERT OR IGNORE INTO settings (key, value) VALUES ('prioritize_2000', 'true')"
)
db_conn.commit()

# メモリ上の挙手データ構造: { date_str: { time_slot: [role_id, ...] } }
# 例: {"2026-10-19": {"21": [123456789, 987654321], "22": [...]}}
gestures = {}

intents = discord.Intents.default()
intents.guilds = True
intents.members = True  # メンバー情報を取得するため必須
intents.message_content = True

class MKUCBot(commands.Bot):

    def __init__(self):
        super().__init__(command_prefix="/", intents=intents)

    async def setup_hook(self):
        await self.tree.sync()
        print("スラッシュコマンドが同期されました。")

bot = MKUCBot()

# --- Webスリープ対策用簡易Webサーバー (Render等対応) ---
async def handle(request):
    return web.Response(text="MKUC 2026 Bot is running!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", handle)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()
    print(f"Web server started on port {PORT}")


# --- ユーティリティ関数 ---
def get_today_str():
    return datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%Y-%m-%d")

def get_team_points(role_id: int) -> int:
    db_cursor.execute("SELECT points FROM scores WHERE role_id = ?", (role_id,))
    res = db_cursor.fetchone()
    return res[0] if res else 0

def set_team_points(role_id: int, points: int):
    db_cursor.execute(
        "INSERT OR REPLACE INTO scores (role_id, points) VALUES (?, ?)",
        (role_id, points),
    )
    db_conn.commit()

def add_team_points(role_id: int, add_pts: int):
    current = get_team_points(role_id)
    set_team_points(role_id, current + add_pts)

def get_setting(key: str) -> str:
    db_cursor.execute("SELECT value FROM settings WHERE key = ?", (key,))
    res = db_cursor.fetchone()
    return res[0] if res else "false"

def set_setting(key: str, value: str):
    db_cursor.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)", (key, value)
    )
    db_conn.commit()


# --- 挙手一覧メッセージの送受信管理（チャンネル内過去一覧の削除付き） ---
async def send_gesture_status(channel: discord.TextChannel):
    today = get_today_str()
    if today not in gestures or not gestures[today]:
        embed = discord.Embed(
            title="📅 本日の挙手状況", description="現在、どの時間帯も挙手されていません。", color=discord.Color.blue()
        )
    else:
        embed = discord.Embed(
            title="📅 本日の挙手状況一覧", color=discord.Color.blue()
        )
        for slot in sorted(gestures[today].keys(), key=int):
            role_ids = gestures[today][slot]
            if not role_ids:
                team_list = "なし"
            else:
                team_list = ", ".join([f"<@&{rid}>" for rid in role_ids])
            embed.add_field(name=f"🕒 {slot}時枠", value=team_list, inline=False)

    # チャンネル内の過去のBotメッセージを検索して削除（取り消し処理）
    try:
        async for msg in channel.history(limit=20):
            if msg.author == bot.user and msg.embeds:
                if msg.embeds[0].title and "挙手状況" in msg.embeds[0].title:
                    await msg.delete()
    except Exception as e:
        print(f"メッセージ削除エラー: {e}")

    await channel.send(embed=embed)


# --- 毎日12:00の自動アナウンス ---
@tasks.loop(time=datetime.time(hour=12, minute=0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))
async def daily_announcement():
    # 適切な告知チャンネルをGuildから探して送信（最初に見つかったテキストチャンネル、または特定の名前のチャンネル）
    for guild in bot.guilds:
        target_channel = discord.utils.get(guild.text_channels, name="募集-挙手") # 見つからなければ一般的なチャンネル
        if not target_channel:
            for ch in guild.text_channels:
                if ch.permissions_for(guild.me).send_messages:
                    target_channel = ch
                    break
        if target_channel:
            # サーバーメンバー全員（あるいは参加者ロール）をメンション
            await target_channel.send(
                f"@everyone 12時になりましたので、本日の募集を開始します！\n"
                f"`/c-uc` コマンドで参加したい時間帯（例: `21,22` など）に挙手してください！"
            )

# --- 翌朝8:00の自動クリア ---
@tasks.loop(time=datetime.time(hour=8, minute=0, tzinfo=datetime.timezone(datetime.timedelta(hours=9))))
async def daily_clear():
    today = get_today_str()
    if today in gestures:
        gestures[today] = {}
    print("朝8:00になったため、本日の挙手データをリセットしました。")

# --- マッチング自動処理ループ（毎分チェック: 20:53, 21:53, 22:53） ---
@tasks.loop(minutes=1)
async def matching_check_loop():
    now_jst = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9)))
    current_time_str = now_jst.strftime("%H:%M")
    today = get_today_str()

    # 締め切り時刻の判定マップ（各試合時間の7分前）
    # 21時試合 -> 20:53締切
    # 22時試合 -> 21:53締切
    # 23時試合 -> 22:53締切
    deadline_map = {
        "20:53": "21",
        "21:53": "22",
        "22:53": "23"
    }

    if current_time_str in deadline_map:
        slot = deadline_map[current_time_str]
        if today in gestures and slot in gestures[today] and gestures[today][slot]:
            await execute_matching(slot)

async def execute_matching(slot: str):
    today = get_today_str()
    teams = list(gestures[today][slot]) # 挙手したチームのrole_idリスト
    
    for guild in bot.guilds:
        # 該当チャンネル（例: "募集-挙手" や最初のチャンネル）を探す
        channel = discord.utils.get(guild.text_channels, name="募集-挙手")
        if not channel:
            channel = guild.text_channels[0]

        await channel.send(f"🔒 【{slot}時枠】の挙手を締め切りました！マッチングを実行します...")

        import random
        random.shuffle(teams)

        # 漏れるチームがある場合（奇数など）の処理
        odd_team = None
        if len(teams) % 2 != 0:
            prioritize_2000 = (get_setting("prioritize_2000") == "true")
            if prioritize_2000:
                # 2000点超えているチームを優先して漏れさせる（お休み）
                over_2000_teams = [t for t in teams if get_team_points(t) >= 2000]
                if over_2000_teams:
                    odd_team = random.choice(over_2000_teams)
                else:
                    odd_team = teams[-1]
            else:
                odd_team = teams[-1]
            teams.remove(odd_team)
            role_obj = guild.get_role(odd_team)
            role_name = role_obj.name if role_obj else "不明"
            await channel.send(f"⚠️ チーム数が奇数のため、今回のマッチングから外れる（お休みとなる）チーム：**{role_name}**")

        # ペアを作成してチャンネル作成
        pairs = [(teams[i], teams[i+1]) for i in range(0, len(teams), 2)]
        
        date_tag = datetime.datetime.now(datetime.timezone(datetime.timedelta(hours=9))).strftime("%m%d")

        for idx, (team1_id, team2_id) in enumerate(pairs, start=1):
            t1_role = guild.get_role(team1_id)
            t2_role = guild.get_role(team2_id)
            t1_name = t1_role.name if t1_role else "Team1"
            t2_name = t2_role.name if t2_role else "Team2"

            room_name = f"{date_tag}-{slot}-room{idx}"

            # チャンネルの権限設定（両チームのロールと運営以外は見れないようにする）
            overwrites = {
                guild.default_role: discord.PermissionOverwrite(read_messages=False),
                t1_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                t2_role: discord.PermissionOverwrite(read_messages=True, send_messages=True),
                guild.me: discord.PermissionOverwrite(read_messages=True, send_messages=True)
            }

            # カテゴリーがあればそこに作る、なければそのまま
            room_channel = await guild.create_text_channel(room_name, overwrites=overwrites)

            # ホストをランダムで決定
            host_role = random.choice([t1_role, t2_role])

            # メンションとホスト通知
            await room_channel.send(
                f"{t1_role.mention} {t2_role.mention}\n"
                f"部屋が作成されました！試合を行ってください。\n"
                f"🏠 **Host:** {host_role.mention}"
            )

        # 処理が終わったらその時間枠の挙手をクリア
        gestures[today][slot] = []


# --- スラッシュコマンドの実装 ---

# 1. 挙手コマンド: /c-uc times:21,22 など
@bot.tree.command(name="c-uc", description="指定した時間帯に挙手します（例: 21,22 または 21-23）")
@app_commands.describe(times="挙手する時間帯（例: 21,22 または 21-23、23 など）")
async def c_uc(interaction: discord.Interaction, times: str):
    await interaction.response.defer(ephemeral=True)
    
    # ユーザーが持っているロールの中から、チームロール（サーバー内の他のロール、ここではメンバー全員が属するチームロールと仮定）を特定
    # ※ユーザーのロールのうち、@everyoneやBot自身を除いたロールをチームロールとするか、
    # あるいはユーザーが所属するロールを自動判定します。
    member_roles = [role.id for role in interaction.user.roles if not role.is_default() and not role.managed]
    
    if not member_roles:
        await interaction.followup.send("エラー: あなたにチームロールが割り当てられていません。", ephemeral=True)
        return

    # ユーザーの最初のチームロールを代表ロールとする（あるいは複数ある場合はすべて対応）
    # ここでは仕様に沿って「そのチームのロール」として処理します
    team_role_id = member_roles[0] # 本人のメインチームロール

    # 入力された時間（例: "21,22" や "21-23"）を解析
    time_slots = []
    try:
        if "-" in times:
            start, end = map(int, times.split("-"))
            time_slots = [str(h) for h in range(start, end + 1)]
        elif "," in times:
            time_slots = [t.strip() for t in times.split(",")]
        else:
            time_slots = [times.strip()]
    except Exception:
        await interaction.followup.send("エラー: 時間の指定形式が正しくありません。（例: `21,22` または `21-23`）", ephemeral=True)
        return

    today = get_today_str()
    if today not in gestures:
        gestures[today] = {}

    registered_slots = []
    for slot in time_slots:
        if slot not in ["21", "22", "23"]:
            continue
        if slot not in gestures[today]:
            gestures[today][slot] = []
        if team_role_id not in gestures[today][slot]:
            gestures[today][slot].append(team_role_id)
        registered_slots.append(slot)

    role_obj = interaction.guild.get_role(team_role_id)
    team_name = role_obj.name if role_obj else "あなたのチーム"

    await interaction.followup.send(f"✅ チーム「{team_name}」の {', '.join(registered_slots)}時枠 の挙手を登録しました！", ephemeral=True)
    await send_gesture_status(interaction.channel)


# 2. 挙手を下ろすコマンド: /d-uc time:22
@bot.tree.command(name="d-uc", description="指定した時間帯の挙手を下ろします")
@app_commands.describe(time_slot="取り下げる時間帯（例: 22）")
@app_commands.choices(time_slot=[
    app_commands.Choice(name="21時", value="21"),
    app_commands.Choice(name="22時", value="22"),
    app_commands.Choice(name="23時", value="23"),
])
async def d_uc(interaction: discord.Interaction, time_slot: str):
    await interaction.response.defer(ephemeral=True)
    member_roles = [role.id for role in interaction.user.roles if not role.is_default() and not role.managed]
    if not member_roles:
        await interaction.followup.send("エラー: チームロールが見つかりません。", ephemeral=True)
        return
    team_role_id = member_roles[0]

    today = get_today_str()
    if today in gestures and time_slot in gestures[today]:
        if team_role_id in gestures[today][time_slot]:
            gestures[today][time_slot].remove(team_role_id)

    await interaction.followup.send(f"🗑️ {time_slot}時枠の挙手を取り下げました。", ephemeral=True)
    await send_gesture_status(interaction.channel)


# 3. 挙手完全クリア: /clear-uc
@bot.tree.command(name="clear-uc", description="本日の挙手を完全にクリアします（運営用）")
async def clear_uc(interaction: discord.Interaction):
    today = get_today_str()
    gestures[today] = {}
    await interaction.response.send_message("🧹 本日の挙手をすべてクリアしました。", ephemeral=True)
    await send_gesture_status(interaction.channel)


# 4. スコア加算コマンド: /score-uc @チームA 491 @チームB 493
@bot.tree.command(name="score-uc", description="試合結果のスコアを各チームに加算します（運営用）")
@app_commands.describe(
    team1="チーム1のロール", points1="チーム1の獲得ポイント",
    team2="チーム2のロール", points2="チーム2の獲得ポイント",
    team3="チーム3のロール（任意の3チーム目があれば）", points3="チーム3のポイント",
    team4="チーム4のロール（任意）", points4="チーム4のポイント"
)
async def score_uc(
    interaction: discord.Interaction,
    team1: discord.Role, points1: int,
    team2: discord.Role, points2: int,
    team3: discord.Role = None, points3: int = 0,
    team4: discord.Role = None, points4: int = 0
):
    add_team_points(team1.id, points1)
    add_team_points(team2.id, points2)
    if team3 and points3:
        add_team_points(team3.id, points3)
    if team4 and points4:
        add_team_points(team4.id, points4)

    await interaction.response.send_message(
        f"📊 スコアを記録・加算しました！\n"
        f"・ {team1.name}: +{points1}pt (累計: {get_team_points(team1.id)}pt)\n"
        f"・ {team2.name}: +{points2}pt (累計: {get_team_points(team2.id)}pt)"
        + (f"\n・ {team3.name}: +{points3}pt" if team3 else "")
        + (f"\n・ {team4.name}: +{points4}pt" if team4 else "")
    )


# 5. スコア直接変更コマンド: /scorechange-uc @チーム 2980
@bot.tree.command(name="scorechange-uc", description="チームの総得点を直接書き換えます（運営用バグ修正用）")
@app_commands.describe(team="対象のチームロール", points="変更後の総得点")
async def scorechange_uc(interaction: discord.Interaction, team: discord.Role, points: int):
    set_team_points(team.id, points)
    await interaction.response.send_message(
        f"✏️ チーム「{team.name}」の総得点を **{points}pt** に直接書き換えました。", ephemeral=True
    )


# 6. お休み優先機能の切り替え: /toggle-prioritize
@bot.tree.command(name="toggle-prioritize", description="2000点超えチーム優先お休み機能のON/OFFを切り替えます（運営用）")
@app_commands.choices(status=[
    app_commands.Choice(name="ON (有効)", value="true"),
    app_commands.Choice(name="OFF (無効)", value="false")
])
async def toggle_prioritize(interaction: discord.Interaction, status: str):
    set_setting("prioritize_2000", status)
    await interaction.response.send_message(
        f"⚙️ 2000点超えチーム優先お休み機能を **{status.upper()}** に設定しました。", ephemeral=True
    )


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user.name} (ID: {bot.user.id})")
    daily_announcement.start()
    daily_clear.start()
    matching_check_loop.start()
    await start_web_server()

# Botの起動
if __name__ == "__main__":
    bot.run(TOKEN)