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

# 試合開始時刻の7分前に締切（時と分で管理）
DEADLINES = {
    "21": (20, 53),
    "22": (21, 53),
    "23": (22, 53),
}

MATCH_TIMES = {
    "21": (20, 55),
    "22": (21, 55),
    "23": (22, 55),
}

ROOM_TIMES = {
    "21": (20, 58),
    "22": (21, 58),
    "23": (22, 58),
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

    CREATE TABLE IF NOT EXISTS daily_status (
        guild_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        slot TEXT NOT NULL,
        matched INTEGER NOT NULL DEFAULT 0,
        rooms_created INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY (guild_id, date, slot)
    );

    CREATE TABLE IF NOT EXISTS matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        slot TEXT NOT NULL,
        room_number INTEGER NOT NULL,
        team1_id INTEGER NOT NULL,
        team2_id INTEGER NOT NULL,
        host_id INTEGER NOT NULL,
        channel_id INTEGER,
        UNIQUE (guild_id, date, slot, room_number)
    );

    CREATE TABLE IF NOT EXISTS match_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        slot TEXT NOT NULL,
        team1_id INTEGER NOT NULL,
        team2_id INTEGER NOT NULL
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


def get_setting(guild_id, key, default="false"):
    row = db.execute(
        "SELECT value FROM settings WHERE guild_id=? AND key=?",
        (guild_id, key),
    ).fetchone()
    return row[0] if row else default


def set_setting(guild_id, key, value):
    db_execute(
        """
        INSERT INTO settings (guild_id, key, value)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, key)
        DO UPDATE SET value=excluded.value
        """,
        (guild_id, key, str(value)),
    )


def get_points(guild_id, role_id):
    row = db.execute(
        "SELECT points FROM scores WHERE guild_id=? AND role_id=?",
        (guild_id, role_id),
    ).fetchone()
    return row[0] if row else 0


def add_points(guild_id, role_id, points):
    current = get_points(guild_id, role_id)
    db_execute(
        """
        INSERT INTO scores (guild_id, role_id, points)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, role_id)
        DO UPDATE SET points=excluded.points
        """,
        (guild_id, role_id, current + points),
    )
    return current + points


def set_points(guild_id, role_id, points):
    db_execute(
        """
        INSERT INTO scores (guild_id, role_id, points)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, role_id)
        DO UPDATE SET points=excluded.points
        """,
        (guild_id, role_id, points),
    )


def get_config(guild_id):
    row = db.execute(
        """
        SELECT signup_channel_id,
               participant_role_id,
               room_category_id
        FROM guild_config
        WHERE guild_id=?
        """,
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
        INSERT INTO guild_config (
            guild_id,
            signup_channel_id,
            participant_role_id,
            room_category_id
        )
        VALUES (?, ?, ?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET
            signup_channel_id=excluded.signup_channel_id,
            participant_role_id=excluded.participant_role_id,
            room_category_id=excluded.room_category_id
        """,
        (guild_id, signup_channel_id, participant_role_id, room_category_id),
    )


def get_team_role_ids(guild_id):
    rows = db.execute(
        "SELECT role_id FROM team_roles WHERE guild_id=?",
        (guild_id,),
    ).fetchall()
    return {row[0] for row in rows}


def register_team_role(guild_id, role_id):
    db_execute(
        "INSERT OR IGNORE INTO team_roles (guild_id, role_id) VALUES (?, ?)",
        (guild_id, role_id),
    )


def unregister_team_role(guild_id, role_id):
    db_execute(
        "DELETE FROM team_roles WHERE guild_id=? AND role_id=?",
        (guild_id, role_id),
    )


# ============================================================
# Bot初期化
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(command_prefix="!", intents=intents)


# ============================================================
# 共通処理
# ============================================================

def is_admin(interaction: discord.Interaction):
    return interaction.guild is not None and interaction.user.guild_permissions.manage_guild


async def require_admin(interaction: discord.Interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "このコマンドはサーバー管理権限を持つ運営のみ使用できます。",
            ephemeral=True,
        )
        return False
    return True


def get_member_team_roles(interaction: discord.Interaction):
    guild_id = interaction.guild_id
    registered = get_team_role_ids(guild_id)
    return [role for role in interaction.user.roles if role.id in registered]


def get_team_role_from_interaction(interaction):
    roles = get_member_team_roles(interaction)
    if len(roles) == 0:
        return None, "あなたにチームロールが設定されていません。"
    if len(roles) > 1:
        return None, "複数のチームロールを持っています。運営に確認してください。"
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
    if not isinstance(channel, discord.TextChannel):
        return None
    return channel


# ============================================================
# 挙手一覧
# ============================================================

async def refresh_gesture_status(guild):
    config = get_config(guild.id)
    if not config:
        return

    channel = await get_text_channel(guild, config["signup_channel_id"])
    if channel is None:
        return

    try:
        async for msg in channel.history(limit=50):
            if msg.author == bot.user and msg.embeds and msg.embeds[0].title == "MKUC 本日の挙手状況":
                try:
                    await msg.delete()
                except Exception:
                    pass
    except Exception:
        pass

    today = today_str()
    embed = discord.Embed(
        title="MKUC 本日の挙手状況",
        description="チームごとの挙手状況です。",
        color=discord.Color.blue(),
        timestamp=now_jst(),
    )

    current = now_jst()

    for slot in TIME_SLOTS:
        rows = db.execute(
            """
            SELECT role_id FROM gestures
            WHERE guild_id=? AND date=? AND slot=?
            ORDER BY role_id
            """,
            (guild.id, today, slot),
        ).fetchall()
        role_ids = [row[0] for row in rows]

        d_hour, d_min = DEADLINES[slot]
        if (current.hour, current.minute) >= (d_hour, d_min):
            status = "受付終了"
        else:
            status = "受付中"

        team_text = "\n".join([guild.get_role(rid).mention for rid in role_ids if guild.get_role(rid)]) if role_ids else "なし"

        embed.add_field(
            name=f"{slot}時枠（{status}）・{len(role_ids)}チーム",
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

    registered = []
    rejected = []

    for slot in slots:
        d_hour, d_min = DEADLINES[slot]
        if (current.hour, current.minute) >= (d_hour, d_min):
            rejected.append(slot)
            continue

        db_execute(
            """
            INSERT OR IGNORE INTO gestures (guild_id, date, slot, role_id)
            VALUES (?, ?, ?, ?)
            """,
            (interaction.guild.id, today, slot, team_role.id),
        )
        registered.append(slot)

    if registered:
        await refresh_gesture_status(interaction.guild)

    messages = []
    if registered:
        messages.append(f"✅ {team_role.name} の挙手を登録しました（{', '.join(registered)}時）")
    if rejected:
        messages.append(f"⚠️ 以下の時間帯は締切済みです: {', '.join(rejected)}時")

    await interaction.response.send_message("\n".join(messages), ephemeral=True)


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

    await refresh_gesture_status(interaction.guild)
    await interaction.response.send_message(f"🗑️ {team_role.name} の {slot}時枠の挙手を取り下げました。", ephemeral=True)


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


@bot.tree.command(name="score-uc", description="ポイントを加算します")
async def score_uc(interaction: discord.Interaction, team1: discord.Role, points1: int, team2: discord.Role, points2: int):
    if not await require_admin(interaction):
        return
    add_points(interaction.guild.id, team1.id, points1)
    add_points(interaction.guild.id, team2.id, points2)
    await interaction.response.send_message("📊 スコアを加算しました。", ephemeral=True)


@bot.tree.command(name="scorechange-uc", description="ポイントを直接変更します")
async def scorechange_uc(interaction: discord.Interaction, team: discord.Role, points: int):
    if not await require_admin(interaction):
        return
    set_points(interaction.guild.id, team.id, points)
    await interaction.response.send_message("✏️ ポイントを変更しました。", ephemeral=True)


@bot.tree.command(name="clear-uc", description="本日の挙手をクリアします")
async def clear_uc(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return
    db_execute("DELETE FROM gestures WHERE guild_id=? AND date=?", (interaction.guild.id, today_str()))
    await refresh_gesture_status(interaction.guild)
    await interaction.response.send_message("🧹 クリアしました。", ephemeral=True)


# ============================================================
# 定期ループ処理（マッチング等）
# ============================================================

@tasks.loop(seconds=30)
async def scheduler():
    current = now_jst()
    date_str = current.strftime("%Y-%m-%d")
    current_time = current.time()

    for guild in bot.guilds:
        config = get_config(guild.id)
        if not config:
            continue

        # 12時募集
        if current.hour == 12 and current.minute == 0:
            key = f"announcement_{date_str}"
            if get_setting(guild.id, key) != "true":
                channel = await get_text_channel(guild, config["signup_channel_id"])
                role = guild.get_role(config["participant_role_id"])
                if channel and role:
                    await channel.send(f"{role.mention}\n📢 12時になりましたので、本日の募集を開始します！\n`/c-uc` で挙手してください。")
                set_setting(guild.id, key, "true")

        # 8時クリア
        if current.hour == 8 and current.minute == 0:
            key = f"clear_{date_str}"
            if get_setting(guild.id, key) != "true":
                yesterday = (current.date() - datetime.timedelta(days=1)).isoformat()
                db_execute("DELETE FROM gestures WHERE guild_id=? AND date=?", (guild.id, yesterday))
                set_setting(guild.id, key, "true")

        # 時間枠ごとの処理
        for slot in TIME_SLots if 'TIME_SLOTS' in globals() else TIME_SLOTS:
            m_h, m_m = MATCH_TIMES[slot]
            if current.hour == m_h and current.minute == m_m:
                # 簡易マッチング処理実行
                pass


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


# ============================================================
# Webサーバー
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
    if not scheduler.is_running():
        scheduler.start()


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