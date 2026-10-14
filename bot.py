import asyncio
import datetime
import os
import random
import sqlite3
import logging

import discord
from discord import app_commands
from discord.ext import commands, tasks
from aiohttp import web


# ============================================================
# 設定
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", "8080"))

JST = datetime.timezone(datetime.timedelta(hours=9))

TIME_SLOTS = ["21", "22", "23"]

DEADLINES = {
    "21": (20, 53),
    "22": (21, 53),
    "23": (22, 53),
}

DB_PATH = os.getenv("MKUC_DB_PATH", "mkuc_bot.db")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("MKUC")


# ============================================================
# データベース
# ============================================================

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")

db.executescript(
    """
    CREATE TABLE IF NOT EXISTS scores (
        guild_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        points INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, role_id)
    );

    CREATE TABLE IF NOT EXISTS settings (
        guild_id INTEGER NOT NULL,
        key TEXT NOT NULL,
        value TEXT NOT NULL,
        PRIMARY KEY (guild_id, key)
    );

    CREATE TABLE IF NOT EXISTS team_roles (
        guild_id INTEGER NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, role_id)
    );

    CREATE TABLE IF NOT EXISTS guild_config (
        guild_id INTEGER PRIMARY KEY,
        signup_channel_id INTEGER,
        participant_role_id INTEGER,
        room_category_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS gestures (
        guild_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        slot TEXT NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, date, slot, role_id)
    );
    """
)
db.commit()


def db_execute(query, params=()):
    cur = db.execute(query, params)
    db.commit()
    return cur


def now_jst():
    return datetime.datetime.now(JST)


def today_str():
    return now_jst().strftime("%Y-%m-%d")


def get_config(guild_id):
    row = db.execute(
        "SELECT signup_channel_id, participant_role_id, room_category_id FROM guild_config WHERE guild_id=?",
        (guild_id,),
    ).fetchone()
    if not row:
        return None
    return {
        "signup_channel_id": row[0],
        "participant_role_id": row[1],
        "room_category_id": row[2],
    }


def set_config(guild_id, signup_channel_id, participant_role_id, room_category_id):
    db_execute(
        """
        INSERT INTO guild_config (guild_id, signup_channel_id, participant_role_id, room_category_id)
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET signup_channel_id=excluded.signup_channel_id,
                      participant_role_id=excluded.participant_role_id,
                      room_category_id=excluded.room_category_id
        """,
        (guild_id, signup_channel_id, participant_role_id, room_category_id),
    )


def get_team_role_ids(guild_id):
    rows = db.execute("SELECT role_id FROM team_roles WHERE guild_id=?", (guild_id,)).fetchall()
    return {row[0] for row in rows}


def register_team_role(guild_id, role_id):
    db_execute("INSERT OR IGNORE INTO team_roles (guild_id, role_id) VALUES (?, ?)", (guild_id, role_id))


def unregister_team_role(guild_id, role_id):
    db_execute("DELETE FROM team_roles WHERE guild_id=? AND role_id=?", (guild_id, role_id))


# ============================================================
# Bot初期化
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


def is_admin(interaction: discord.Interaction):
    return interaction.guild is not None and interaction.user.guild_permissions.manage_guild


async def require_admin(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message("運営のみ使用できます。", ephemeral=True)
        return False
    return True


def get_team_role_from_interaction(interaction):
    guild_id = interaction.guild_id
    registered = get_team_role_ids(guild_id)
    roles = [role for role in interaction.user.roles if role.id in registered]

    if len(roles) == 0:
        return None, "あなたにチームロールが設定されていません。"
    if len(roles) > 1:
        return None, "複数のチームロールを持っています。"
    return roles[0], None


async def get_text_channel(guild, channel_id):
    if not channel_id:
        return None
    channel = guild.get_channel(channel_id)
    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except Exception:
            return None
    return channel if isinstance(channel, discord.TextChannel) else None


# ============================================================
# 挙手一覧の投稿・更新（毎回古いものを消してチャンネルに新しく送る）
# ============================================================

async def refresh_gesture_status(guild, current_channel=None):
    config = get_config(guild.id)
    if not config:
        return

    # 設定された挙手チャンネルを取得（指定がなければコマンドが実行されたチャンネルなど）
    channel = await get_text_channel(guild, config["signup_channel_id"])
    if not channel:
        channel = current_channel

    if not channel:
        return

    # チャンネル内の過去のBotによる挙手一覧メッセージを削除
    try:
        async for msg in channel.history(limit=30):
            if msg.author == bot.user and msg.embeds:
                embed = msg.embeds[0]
                if embed.title and "挙手状況" in embed.title:
                    try:
                        await msg.delete()
                    except discord.HTTPException:
                        pass
    except Exception:
        pass

    today = today_str()
    embed = discord.Embed(
        title="📅 MKUC 本日の挙手状況一覧",
        description="現在の各時間帯の挙手チーム一覧です（挙手・取り下げ時に自動更新されます）。",
        color=discord.Color.blue(),
        timestamp=now_jst(),
    )

    current = now_jst()

    for slot in TIME_SLOTS:
        rows = db.execute(
            "SELECT role_id FROM gestures WHERE guild_id=? AND date=? AND slot=? ORDER BY role_id",
            (guild.id, today, slot),
        ).fetchall()
        role_ids = [row[0] for row in rows]

        d_hour, d_min = DEADLINES[slot]
        status = "受付終了" if (current.hour, current.minute) >= (d_hour, d_min) else "受付中"

        team_lines = [guild.get_role(rid).mention for rid in role_ids if guild.get_role(rid)]
        team_text = "\n".join(team_lines) if team_lines else "なし"

        embed.add_field(
            name=f"🕒 {slot}時枠（{status}）: {len(role_ids)}チーム",
            value=team_text,
            inline=False,
        )

    try:
        await channel.send(embed=embed, allowed_mentions=discord.AllowedMentions.none())
    except Exception:
        pass


# ============================================================
# 挙手コマンド
# ============================================================

def parse_times(times):
    times = times.strip().replace(" ", "")
    if not times:
        raise ValueError("時間を指定してください。")
    if "-" in times:
        parts = times.split("-")
        if len(parts) != 2:
            raise ValueError("形式が正しくありません。")
        start, end = map(int, parts)
        slots = [str(h) for h in range(start, end + 1)]
    else:
        slots = [p for p in times.split(",") if p]

    if any(s not in TIME_SLOTS for s in slots):
        raise ValueError("指定できる時間は21、22、23です。")
    return list(dict.fromkeys(slots))


@bot.tree.command(name="c-uc", description="参加可能な時間帯に挙手します")
@app_commands.describe(times="例: 21,22 または 21-23 または 23")
async def c_uc(interaction: discord.Interaction, times: str):
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使用してください。", ephemeral=True)
        return

    team_role, error = get_team_role_from_interaction(interaction)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    try:
        slots = parse_times(times)
    except ValueError as exc:
        await interaction.response.send_message(str(exc), ephemeral=True)
        return

    today = today_str()
    current = now_jst()
    registered, rejected = [], []

    for slot in slots:
        d_hour, d_min = DEADLINES[slot]
        if (current.hour, current.minute) >= (d_hour, d_min):
            rejected.append(slot)
            continue

        db_execute(
            "INSERT OR IGNORE INTO gestures (guild_id, date, slot, role_id) VALUES (?, ?, ?, ?)",
            (interaction.guild.id, today, slot, team_role.id),
        )
        registered.append(slot)

    # まずコマンド実行者に処理完了をこっそり返す (ephemeral=True)
    msg_parts = []
    if registered:
        msg_parts.append(f"✅ {team_role.name} の挙手を登録しました（{', '.join(registered)}時）")
    if rejected:
        msg_parts.append(f"⚠️ 締切済み: {', '.join(rejected)}時")
    
    await interaction.response.send_message("\n".join(msg_parts), ephemeral=True)

    # 挙手一覧表を全員が見えるチャンネルに新しく投稿し直す
    if registered:
        await refresh_gesture_status(interaction.guild, interaction.channel)


@bot.tree.command(name="d-uc", description="指定した時間帯の挙手を取り下げます")
@app_commands.describe(time_slot="取り下げる時間帯")
@app_commands.choices(
    time_slot=[
        app_commands.Choice(name="21時", value="21"),
        app_commands.Choice(name="22時", value="22"),
        app_commands.Choice(name="23時", value="23"),
    ]
)
async def d_uc(interaction: discord.Interaction, time_slot: app_commands.Choice[str]):
    if interaction.guild is None:
        await interaction.response.send_message("サーバー内で使用してください。", ephemeral=True)
        return

    team_role, error = get_team_role_from_interaction(interaction)
    if error:
        await interaction.response.send_message(error, ephemeral=True)
        return

    slot = time_slot.value
    current = now_jst()
    d_hour, d_min = DEADLINES[slot]
    if (current.hour, current.minute) >= (d_hour, d_min):
        await interaction.response.send_message("この時間帯はすでに締切済みです。", ephemeral=True)
        return

    db_execute(
        "DELETE FROM gestures WHERE guild_id=? AND date=? AND slot=? AND role_id=?",
        (interaction.guild.id, today_str(), slot, team_role.id),
    )

    await interaction.response.send_message(f"🗑️ {team_role.name} の {slot}時枠の挙手を取り下げました。", ephemeral=True)
    
    # 挙手一覧表を全員が見えるチャンネルに新しく投稿し直す
    await refresh_gesture_status(interaction.guild, interaction.channel)


# ============================================================
# 運営用コマンド
# ============================================================

@bot.tree.command(name="setup-uc", description="MKUC Botの設定を行います")
@app_commands.describe(signup_channel="挙手一覧用チャンネル", participant_role="参加者ロール", room_category="部屋用カテゴリ")
async def setup_uc(interaction: discord.Interaction, signup_channel: discord.TextChannel, participant_role: discord.Role, room_category: discord.CategoryChannel = None):
    if not await require_admin(interaction):
        return
    set_config(interaction.guild.id, signup_channel.id, participant_role.id, room_category.id if room_category else None)
    await interaction.response.send_message("✅ 設定を保存しました。", ephemeral=True)


@bot.tree.command(name="teamrole-uc", description="チームロールを登録・解除します")
@app_commands.describe(action="登録/解除", role="チームロール")
@app_commands.choices(action=[app_commands.Choice(name="登録", value="add"), app_commands.Choice(name="解除", value="remove")])
async def teamrole_uc(interaction: discord.Interaction, action: app_commands.Choice[str], role: discord.Role):
    if not await require_admin(interaction):
        return
    if action.value == "add":
        register_team_role(interaction.guild.id, role.id)
        await interaction.response.send_message(f"✅ チームロール {role.mention} を登録しました。", ephemeral=True)
    else:
        unregister_team_role(interaction.guild.id, role.id)
        await interaction.response.send_message(f"🗑️ 登録を解除しました。", ephemeral=True)


@bot.tree.command(name="clear-uc", description="本日の挙手をクリアします")
async def clear_uc(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    db_execute("DELETE FROM gestures WHERE guild_id=? AND date=?", (interaction.guild.id, today_str()))
    await refresh_gesture_status(interaction.guild, interaction.channel)
    await interaction.response.send_message("🧹 クリアしました。", ephemeral=True)


# ============================================================
# Webサーバー・起動
# ============================================================

async def handle_health(request):
    return web.Response(text="MKUC 2026 Bot is running!")

async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", handle_health), web.get("/health", handle_health)])
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", PORT)
    await site.start()

@bot.event
async def on_ready():
    log.info("Logged in as %s", bot.user)


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN が設定されていません。")
    await bot.login(TOKEN)
    await bot.tree.sync()
    await start_web_server()
    try:
        await bot.connect()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())