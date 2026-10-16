import asyncio
import datetime
import logging
import os
import random
import re
import sqlite3
from itertools import combinations

import discord
from discord import app_commands
from discord.ext import commands
from aiohttp import web

# ============================================================
# 基本設定
# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")
PORT = int(os.getenv("PORT", "8080"))
DB_PATH = os.getenv("MKUC_DB_PATH", "mkuc_bot.db")

JST = datetime.timezone(datetime.timedelta(hours=9))

TIME_SLOTS = ["21", "22", "23"]

# 各時間帯の受付締切
DEADLINES = {
    "21": (20, 53),
    "22": (21, 53),
    "23": (22, 53),
}

MATCH_MINUTE = 54
ROOM_MINUTE = 55

OPEN_HOUR = 12
CLEAR_HOUR = 8

# 2000点モードの初期設定
DEFAULT_HIGH_SCORE_MODE = False
HIGH_SCORE_THRESHOLD = 2000

# 対戦カードの候補を比較する回数
PAIRING_ATTEMPTS = 500

HISTORY_LIMIT = 100
STATUS_TITLE = "📅 MKUC 本日の挙手状況"

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)

log = logging.getLogger("MKUC")


# ============================================================
# データベース
# ============================================================

db = sqlite3.connect(
    DB_PATH,
    check_same_thread=False,
)

db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")

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
        room_category_id INTEGER,
        staff_role_id INTEGER
    );

    CREATE TABLE IF NOT EXISTS gestures (
        guild_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        slot TEXT NOT NULL,
        role_id INTEGER NOT NULL,
        created_at TEXT,
        PRIMARY KEY (guild_id, date, slot, role_id)
    );

    CREATE TABLE IF NOT EXISTS status_messages (
        guild_id INTEGER PRIMARY KEY,
        channel_id INTEGER NOT NULL,
        message_id INTEGER NOT NULL,
        date TEXT NOT NULL
    );

    CREATE TABLE IF NOT EXISTS match_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        slot TEXT NOT NULL,
        role_a INTEGER NOT NULL,
        role_b INTEGER NOT NULL
    );

    CREATE TABLE IF NOT EXISTS scheduled_matches (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        slot TEXT NOT NULL,
        room_no INTEGER NOT NULL,
        role_a INTEGER NOT NULL,
        role_b INTEGER NOT NULL,
        host_role_id INTEGER NOT NULL,
        channel_id INTEGER,
        status TEXT NOT NULL DEFAULT 'matched',
        UNIQUE (guild_id, date, slot, room_no)
    );

    CREATE TABLE IF NOT EXISTS job_runs (
        guild_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        job TEXT NOT NULL,
        completed_at TEXT NOT NULL,
        PRIMARY KEY (guild_id, date, job)
    );

    CREATE TABLE IF NOT EXISTS unmatched_teams (
        guild_id INTEGER NOT NULL,
        date TEXT NOT NULL,
        slot TEXT NOT NULL,
        role_id INTEGER NOT NULL,
        PRIMARY KEY (guild_id, date, slot, role_id)
    );
    """
)


def migrate_database():
    """既存DBに不足している列を追加する。"""

    migrations = {
        "guild_config": {
            "staff_role_id": "INTEGER",
        },
        "gestures": {
            "created_at": "TEXT",
        },
    }

    for table, columns in migrations.items():
        existing = {
            row[1]
            for row in db.execute(
                f"PRAGMA table_info({table})"
            ).fetchall()
        }

        for column, column_type in columns.items():
            if column not in existing:
                db.execute(
                    f"ALTER TABLE {table} "
                    f"ADD COLUMN {column} {column_type}"
                )

    # 既存の挙手には、時刻がない場合のみ仮の時刻を設定する。
    db.execute(
        """
        UPDATE gestures
        SET created_at = date || 'T00:00:00+09:00'
        WHERE created_at IS NULL
        """
    )

    db.commit()


migrate_database()


def db_execute(query, params=()):
    cursor = db.execute(query, params)
    db.commit()
    return cursor


def now_jst():
    return datetime.datetime.now(JST)


def today_str():
    return now_jst().strftime("%Y-%m-%d")


def get_config(guild_id):
    row = db.execute(
        """
        SELECT
            signup_channel_id,
            participant_role_id,
            room_category_id,
            staff_role_id
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
        "staff_role_id": row[3],
    }


def set_config(
    guild_id,
    signup_channel_id,
    participant_role_id,
    room_category_id,
    staff_role_id,
):
    db_execute(
        """
        INSERT INTO guild_config (
            guild_id,
            signup_channel_id,
            participant_role_id,
            room_category_id,
            staff_role_id
        )
        VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id)
        DO UPDATE SET
            signup_channel_id=excluded.signup_channel_id,
            participant_role_id=excluded.participant_role_id,
            room_category_id=excluded.room_category_id,
            staff_role_id=excluded.staff_role_id
        """,
        (
            guild_id,
            signup_channel_id,
            participant_role_id,
            room_category_id,
            staff_role_id,
        ),
    )


def get_team_role_ids(guild_id):
    return {
        row[0]
        for row in db.execute(
            """
            SELECT role_id
            FROM team_roles
            WHERE guild_id=?
            """,
            (guild_id,),
        ).fetchall()
    }


def register_team_role(guild_id, role_id):
    db_execute(
        """
        INSERT OR IGNORE INTO team_roles (guild_id, role_id)
        VALUES (?, ?)
        """,
        (guild_id, role_id),
    )


def unregister_team_role(guild_id, role_id):
    db_execute(
        """
        DELETE FROM team_roles
        WHERE guild_id=? AND role_id=?
        """,
        (guild_id, role_id),
    )


def get_team_score(guild_id, role_id):
    row = db.execute(
        """
        SELECT points
        FROM scores
        WHERE guild_id=? AND role_id=?
        """,
        (guild_id, role_id),
    ).fetchone()

    return row[0] if row else 0


def set_team_score(guild_id, role_id, points):
    db_execute(
        """
        INSERT INTO scores (guild_id, role_id, points)
        VALUES (?, ?, ?)
        ON CONFLICT(guild_id, role_id)
        DO UPDATE SET points=excluded.points
        """,
        (guild_id, role_id, points),
    )


def get_setting(guild_id, key, default=None):
    row = db.execute(
        """
        SELECT value
        FROM settings
        WHERE guild_id=? AND key=?
        """,
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


def high_score_mode_enabled(guild_id):
    value = get_setting(
        guild_id,
        "high_score_bye",
        str(DEFAULT_HIGH_SCORE_MODE),
    )

    return value.lower() in ("true", "1", "yes", "on")


def get_required_games(guild_id):
    try:
        return max(
            0,
            int(
                get_setting(
                    guild_id,
                    "required_games",
                    "0",
                )
            ),
        )
    except (TypeError, ValueError):
        return 0


def get_total_matches(guild_id, role_id):
    row = db.execute(
        """
        SELECT COUNT(*)
        FROM match_history
        WHERE guild_id=?
          AND (role_a=? OR role_b=?)
        """,
        (guild_id, role_id, role_id),
    ).fetchone()

    return row[0]


def get_daily_matches(guild_id, date, role_id):
    row = db.execute(
        """
        SELECT COUNT(*)
        FROM match_history
        WHERE guild_id=?
          AND date=?
          AND (role_a=? OR role_b=?)
        """,
        (guild_id, date, role_id, role_id),
    ).fetchone()

    return row[0]


def get_daily_pair_count(guild_id, date, role_a, role_b):
    row = db.execute(
        """
        SELECT COUNT(*)
        FROM match_history
        WHERE guild_id=?
          AND date=?
          AND (
              (role_a=? AND role_b=?)
              OR
              (role_a=? AND role_b=?)
          )
        """,
        (guild_id, date, role_a, role_b, role_b, role_a),
    ).fetchone()

    return row[0]


def get_previous_meetings(guild_id, role_a, role_b):
    row = db.execute(
        """
        SELECT COUNT(*)
        FROM match_history
        WHERE guild_id=?
          AND (
              (role_a=? AND role_b=?)
              OR
              (role_a=? AND role_b=?)
          )
        """,
        (guild_id, role_a, role_b, role_b, role_a),
    ).fetchone()

    return row[0]


def get_latest_meeting(guild_id, role_a, role_b):
    row = db.execute(
        """
        SELECT date, slot
        FROM match_history
        WHERE guild_id=?
          AND (
              (role_a=? AND role_b=?)
              OR
              (role_a=? AND role_b=?)
          )
        ORDER BY date DESC, slot DESC, id DESC
        LIMIT 1
        """,
        (guild_id, role_a, role_b, role_b, role_a),
    ).fetchone()

    return row


# ============================================================
# Bot初期化
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
intents.members = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
)

refresh_locks = {}
scheduler_task = None


def get_refresh_lock(guild_id):
    if guild_id not in refresh_locks:
        refresh_locks[guild_id] = asyncio.Lock()

    return refresh_locks[guild_id]


def is_admin(interaction):
    return (
        interaction.guild is not None
        and interaction.user.guild_permissions.manage_guild
    )


async def require_admin(interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "運営のみ使用できます。サーバー管理権限が必要です。",
            ephemeral=True,
        )
        return False

    return True


def get_team_role_from_interaction(interaction):
    registered = get_team_role_ids(interaction.guild_id)

    member_roles = getattr(interaction.user, "roles", [])

    roles = [
        role
        for role in member_roles
        if role.id in registered
    ]

    if len(roles) == 0:
        return None, (
            "あなたにチームロールが設定されていません。\n"
            "運営にチームロールの登録を確認してください。"
        )

    if len(roles) > 1:
        return None, (
            "複数のチームロールを持っています。\n"
            "挙手するチームのロールを1つだけ持つよう、運営に確認してください。"
        )

    return roles[0], None


async def get_text_channel(guild, channel_id):
    if not channel_id:
        return None

    channel = guild.get_channel(channel_id)

    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
        ):
            return None

    return channel if isinstance(channel, discord.TextChannel) else None


async def get_category(guild, category_id):
    if not category_id:
        return None

    channel = guild.get_channel(category_id)

    if channel is None:
        try:
            channel = await guild.fetch_channel(category_id)
        except (
            discord.NotFound,
            discord.Forbidden,
            discord.HTTPException,
        ):
            return None

    return channel if isinstance(channel, discord.CategoryChannel) else None


# ============================================================
# 挙手一覧
# ============================================================

def get_gesture_counts(guild_id, date):
    counts = {}

    for slot in TIME_SLOTS:
        row = db.execute(
            """
            SELECT COUNT(*)
            FROM gestures
            WHERE guild_id=? AND date=? AND slot=?
            """,
            (guild_id, date, slot),
        ).fetchone()

        counts[slot] = row[0]

    return counts


def create_gesture_embed(guild_id):
    date = today_str()
    counts = get_gesture_counts(guild_id, date)
    current = now_jst()

    embed = discord.Embed(
        title=STATUS_TITLE,
        description=(
            f"**日付：{date}（日本時間）**\n\n"
            "各時間帯の参加可能チーム数です。"
        ),
        color=discord.Color.blue(),
        timestamp=current,
    )

    for slot in TIME_SLOTS:
        deadline_hour, deadline_minute = DEADLINES[slot]

        closed = (
            (current.hour, current.minute)
            >= (deadline_hour, deadline_minute)
        )

        status = "受付終了" if closed else "受付中"

        embed.add_field(
            name=f"{slot}時（{status}）",
            value=f"**{counts[slot]}チーム**",
            inline=False,
        )

    embed.set_footer(
        text="挙手・取り下げ時に自動更新されます。"
    )

    return embed


async def cleanup_old_status_messages(channel):
    try:
        async for message in channel.history(limit=HISTORY_LIMIT):
            if message.author.id != bot.user.id:
                continue

            if not any(
                embed.title == STATUS_TITLE
                for embed in message.embeds
            ):
                continue

            try:
                await message.delete()
            except discord.NotFound:
                pass
            except discord.HTTPException as exc:
                log.warning("古い挙手一覧の削除に失敗: %s", exc)

    except discord.HTTPException as exc:
        log.warning("チャンネル履歴の取得に失敗: %s", exc)


async def refresh_gesture_status(guild, current_channel=None):
    if guild is None:
        return

    config = get_config(guild.id)

    if not config:
        return

    channel = await get_text_channel(
        guild,
        config["signup_channel_id"],
    )

    if channel is None and isinstance(
        current_channel,
        discord.TextChannel,
    ):
        channel = current_channel

    if channel is None:
        log.warning("挙手一覧チャンネルを取得できません。")
        return

    async with get_refresh_lock(guild.id):
        old_row = db.execute(
            """
            SELECT channel_id, message_id
            FROM status_messages
            WHERE guild_id=?
            """,
            (guild.id,),
        ).fetchone()

        if old_row:
            old_channel = await get_text_channel(guild, old_row[0])

            if old_channel:
                try:
                    old_message = await old_channel.fetch_message(old_row[1])
                    await old_message.delete()
                except (
                    discord.NotFound,
                    discord.Forbidden,
                    discord.HTTPException,
                ):
                    pass

        await cleanup_old_status_messages(channel)

        try:
            message = await channel.send(
                embed=create_gesture_embed(guild.id),
                allowed_mentions=discord.AllowedMentions.none(),
            )

            db_execute(
                """
                INSERT INTO status_messages (
                    guild_id, channel_id, message_id, date
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(guild_id)
                DO UPDATE SET
                    channel_id=excluded.channel_id,
                    message_id=excluded.message_id,
                    date=excluded.date
                """,
                (
                    guild.id,
                    channel.id,
                    message.id,
                    today_str(),
                ),
            )

        except discord.HTTPException:
            log.exception("挙手一覧の投稿に失敗しました。")


# ============================================================
# 挙手受付
# ============================================================

def parse_times(times):
    times = times.strip().replace(" ", "")

    if not times:
        raise ValueError("時間を指定してください。")

    if "-" in times:
        parts = times.split("-")

        if len(parts) != 2:
            raise ValueError("形式が正しくありません。例：21-23")

        try:
            start, end = map(int, parts)
        except ValueError:
            raise ValueError("時間は数字で指定してください。")

        if start > end:
            raise ValueError("開始時間は終了時間以前にしてください。")

        slots = [
            str(hour)
            for hour in range(start, end + 1)
        ]
    else:
        slots = times.split(",")

    if any(slot not in TIME_SLOTS for slot in slots):
        raise ValueError("指定できる時間は21、22、23です。")

    return list(dict.fromkeys(slots))


def is_slot_closed(slot, current=None):
    if current is None:
        current = now_jst()

    deadline_hour, deadline_minute = DEADLINES[slot]

    return (
        (current.hour, current.minute)
        >= (deadline_hour, deadline_minute)
    )


def signup_has_opened(current=None):
    if current is None:
        current = now_jst()

    return current.hour >= OPEN_HOUR or current.hour < CLEAR_HOUR


@bot.tree.command(
    name="c-uc",
    description="参加可能な時間帯に挙手します",
)
@app_commands.describe(
    times="例：21,22 または 21-23 または 23",
)
async def c_uc(interaction: discord.Interaction, times: str):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
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

    current = now_jst()

    if not signup_has_opened(current):
        await interaction.response.send_message(
            "本日の挙手受付は12時に開始します。",
            ephemeral=True,
        )
        return

    today = current.strftime("%Y-%m-%d")

    registered = []
    already_registered = []
    rejected = []

    for slot in slots:
        if is_slot_closed(slot, current):
            rejected.append(slot)
            continue

        cursor = db_execute(
            """
            INSERT OR IGNORE INTO gestures (
                guild_id, date, slot, role_id, created_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                interaction.guild.id,
                today,
                slot,
                team_role.id,
                current.isoformat(),
            ),
        )

        if cursor.rowcount:
            registered.append(slot)
        else:
            already_registered.append(slot)

    messages = []

    if registered:
        messages.append(
            f"✅ 挙手を登録しました：{', '.join(registered)}時"
        )

    if already_registered:
        messages.append(
            f"ℹ️ すでに挙手済みです：{', '.join(already_registered)}時"
        )

    if rejected:
        messages.append(
            f"⚠️ 締切済みのため登録できません：{', '.join(rejected)}時"
        )

    await interaction.response.send_message(
        "\n".join(messages) if messages else "登録できる時間帯がありませんでした。",
        ephemeral=True,
    )

    if registered:
        await refresh_gesture_status(
            interaction.guild,
            interaction.channel,
        )


@bot.tree.command(
    name="d-uc",
    description="指定した時間帯の挙手を取り下げます",
)
@app_commands.describe(
    times="取り下げる時間帯。例：21、21,22、21-23",
)
async def d_uc(
    interaction: discord.Interaction,
    times: str,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    team_role, error = get_team_role_from_interaction(interaction)

    if error:
        await interaction.response.send_message(
            error,
            ephemeral=True,
        )
        return

    try:
        slots = parse_times(times)
    except ValueError as exc:
        await interaction.response.send_message(
            str(exc),
            ephemeral=True,
        )
        return

    current = now_jst()

    if not signup_has_opened(current):
        await interaction.response.send_message(
            "本日の挙手受付は12時に開始します。",
            ephemeral=True,
        )
        return

    today = current.strftime("%Y-%m-%d")

    removed = []
    not_registered = []
    rejected = []

    for slot in slots:
        if is_slot_closed(slot, current):
            rejected.append(slot)
            continue

        cursor = db_execute(
            """
            DELETE FROM gestures
            WHERE guild_id=? AND date=? AND slot=? AND role_id=?
            """,
            (
                interaction.guild.id,
                today,
                slot,
                team_role.id,
            ),
        )

        if cursor.rowcount:
            removed.append(slot)
        else:
            not_registered.append(slot)

    messages = []

    if removed:
        messages.append(
            f"🗑️ 挙手を取り下げました：{', '.join(removed)}時"
        )

    if not_registered:
        messages.append(
            f"ℹ️ 挙手していません：{', '.join(not_registered)}時"
        )

    if rejected:
        messages.append(
            f"⚠️ 締切済みのため取り下げできません："
            f"{', '.join(rejected)}時"
        )

    await interaction.response.send_message(
        "\n".join(messages)
        if messages
        else "取り下げできる時間帯がありませんでした。",
        ephemeral=True,
    )

    if removed:
        await refresh_gesture_status(
            interaction.guild,
            interaction.channel,
        )


# ============================================================
# 運営設定
# ============================================================

@bot.tree.command(
    name="setup-uc",
    description="MKUC Botの設定を行います",
)
@app_commands.describe(
    signup_channel="挙手一覧・募集通知用チャンネル",
    participant_role="参加者ロール",
    room_category="対戦部屋用カテゴリ（任意）",
    staff_role="対戦部屋を閲覧できる運営ロール（任意）",
)
async def setup_uc(
    interaction: discord.Interaction,
    signup_channel: discord.TextChannel,
    participant_role: discord.Role,
    room_category: discord.CategoryChannel = None,
    staff_role: discord.Role = None,
):
    if not await require_admin(interaction):
        return

    set_config(
        interaction.guild.id,
        signup_channel.id,
        participant_role.id,
        room_category.id if room_category else None,
        staff_role.id if staff_role else None,
    )

    await interaction.response.send_message(
        "✅ 設定を保存しました。\n"
        f"挙手一覧・募集通知用チャンネル：{signup_channel.mention}\n"
        f"参加者ロール：{participant_role.mention}\n"
        f"対戦部屋カテゴリ：{room_category.mention if room_category else '未設定'}\n"
        f"運営ロール：{staff_role.mention if staff_role else '未設定'}",
        ephemeral=True,
    )

    await refresh_gesture_status(interaction.guild, signup_channel)


@bot.tree.command(
    name="teamrole-uc",
    description="チームロールを登録・解除します",
)
@app_commands.describe(
    action="登録または解除",
    role="チームロール",
)
@app_commands.choices(
    action=[
        app_commands.Choice(name="登録", value="add"),
        app_commands.Choice(name="解除", value="remove"),
    ],
)
async def teamrole_uc(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    role: discord.Role,
):
    if not await require_admin(interaction):
        return

    if action.value == "add":
        register_team_role(interaction.guild.id, role.id)

        if not db.execute(
            """
            SELECT 1 FROM scores
            WHERE guild_id=? AND role_id=?
            """,
            (interaction.guild.id, role.id),
        ).fetchone():
            set_team_score(interaction.guild.id, role.id, 0)

        await interaction.response.send_message(
            f"✅ チームロール {role.mention} を登録しました。",
            ephemeral=True,
        )
    else:
        unregister_team_role(interaction.guild.id, role.id)

        await interaction.response.send_message(
            f"🗑️ チームロール {role.mention} の登録を解除しました。",
            ephemeral=True,
        )


@bot.tree.command(
    name="refresh-uc",
    description="挙手一覧を手動で更新します",
)
async def refresh_uc(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    if not await require_admin(interaction):
        return

    await interaction.response.defer(ephemeral=True)

    await refresh_gesture_status(
        interaction.guild,
        interaction.channel,
    )

    await interaction.followup.send(
        "🔄 挙手一覧の更新処理を実行しました。",
        ephemeral=True,
    )

# ============================================================
# 挙手チーム一覧（運営用）
# ============================================================

@bot.tree.command(
    name="list-uc",
    description="本日、挙手しているチームを時間帯ごとに確認します（運営用）",
)
async def list_uc(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    if not await require_admin(interaction):
        return

    guild = interaction.guild
    guild_id = guild.id
    date = today_str()

    embed = discord.Embed(
        title="📋 MKUC 本日の挙手チーム一覧",
        description=f"日付：{date}（日本時間）",
        color=discord.Color.blue(),
        timestamp=now_jst(),
    )

    total_teams = set()

    for slot in TIME_SLOTS:
        rows = db.execute(
            """
            SELECT role_id, created_at
            FROM gestures
            WHERE guild_id=? AND date=? AND slot=?
            ORDER BY created_at ASC, role_id ASC
            """,
            (guild_id, date, slot),
        ).fetchall()

        team_names = []

        for role_id, created_at in rows:
            role = guild.get_role(role_id)

            # ロールが削除されている場合
            if role is None:
                team_names.append(
                    f"⚠️ 削除済みロール（ID: {role_id}）"
                )
                continue

            # チームロール登録が解除されている場合も、
            # 挙手データが残っていれば確認できるようにする。
            team_names.append(role.mention)
            total_teams.add(role_id)

        if team_names:
            value = "\n".join(
                f"{index}. {name}"
                for index, name in enumerate(team_names, start=1)
            )
        else:
            value = "挙手しているチームはありません。"

        embed.add_field(
            name=f"🕒 {slot}時枠（{len(rows)}チーム）",
            value=value,
            inline=False,
        )

    embed.set_footer(
        text=f"挙手データがあるチーム数（重複除く）：{len(total_teams)}"
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="clear-uc",
    description="本日の挙手をすべてクリアします",
)
async def clear_uc(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    if not await require_admin(interaction):
        return

    db_execute(
        """
        DELETE FROM gestures
        WHERE guild_id=? AND date=?
        """,
        (interaction.guild.id, today_str()),
    )

    db_execute(
        """
        DELETE FROM unmatched_teams
        WHERE guild_id=? AND date=?
        """,
        (interaction.guild.id, today_str()),
    )

    await refresh_gesture_status(interaction.guild, interaction.channel)

    await interaction.response.send_message(
        "🧹 本日の挙手をすべてクリアしました。",
        ephemeral=True,
    )


# ============================================================
# 得点・試合数の管理
# ============================================================

def parse_score_entries(guild, text):
    """
    入力例：
    @CHU 491 @KU 493

    ロールは、可能な限りDiscord上のロールメンションを使う。
    """

    text = text.strip()

    if not text:
        raise ValueError("例：@CHU 491 @KU 493")

    results = []

    mention_pattern = re.compile(r"<@&(\d+)>\s*(-?\d+)")

    for match in mention_pattern.finditer(text):
        role_id = int(match.group(1))
        points = int(match.group(2))
        role = guild.get_role(role_id)

        if role is None:
            raise ValueError(f"ロールID {role_id} が見つかりません。")

        results.append((role, points))

    if results:
        return results

    tokens = text.split()

    if len(tokens) % 2:
        raise ValueError(
            "形式が正しくありません。\n"
            "例：@CHU 491 @KU 493"
        )

    for index in range(0, len(tokens), 2):
        role_name = tokens[index].removeprefix("@")

        try:
            points = int(tokens[index + 1])
        except ValueError:
            raise ValueError(
                f"得点「{tokens[index + 1]}」が整数ではありません。"
            )

        role = next(
            (
                candidate
                for candidate in guild.roles
                if candidate.name == role_name
            ),
            None,
        )

        if role is None:
            raise ValueError(f"ロール「{role_name}」が見つかりません。")

        results.append((role, points))

    return results


@bot.tree.command(
    name="score-uc",
    description="複数チームの得点を登録・更新します",
)
@app_commands.describe(
    entries="例：@CHU 491 @KU 493（複数指定可能）",
)
async def score_uc(interaction: discord.Interaction, entries: str):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    if not await require_admin(interaction):
        return

    try:
        parsed = parse_score_entries(interaction.guild, entries)
    except ValueError as exc:
        await interaction.response.send_message(
            f"❌ {exc}",
            ephemeral=True,
        )
        return

    registered = get_team_role_ids(interaction.guild.id)

    for role, _ in parsed:
        if role.id not in registered:
            await interaction.response.send_message(
                f"❌ {role.mention} はチームロールとして登録されていません。",
                ephemeral=True,
            )
            return

    # 全ロールを検証してから更新する。
    for role, points in parsed:
        set_team_score(interaction.guild.id, role.id, points)

    await interaction.response.send_message(
        "✅ 得点を登録・更新しました。\n"
        + "\n".join(
            f"{role.mention}：**{points}点**"
            for role, points in parsed
        ),
        ephemeral=True,
    )


@bot.tree.command(
    name="scorechange-uc",
    description="チームの総得点を直接変更します",
)
@app_commands.describe(
    role="得点を変更するチームロール",
    points="変更後の総得点",
)
async def scorechange_uc(
    interaction: discord.Interaction,
    role: discord.Role,
    points: int,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    if not await require_admin(interaction):
        return

    if role.id not in get_team_role_ids(interaction.guild.id):
        await interaction.response.send_message(
            "このロールはチームロールとして登録されていません。",
            ephemeral=True,
        )
        return

    set_team_score(interaction.guild.id, role.id, points)

    await interaction.response.send_message(
        f"✅ {role.mention} の総得点を **{points}点** に変更しました。",
        ephemeral=True,
    )


@bot.tree.command(
    name="scoreview-uc",
    description="チームの得点ランキング、または指定チームの得点を確認します",
)
@app_commands.describe(
    role="得点を確認するチームロール（省略するとランキングを表示）",
)
async def scoreview_uc(
    interaction: discord.Interaction,
    role: discord.Role = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    guild_id = interaction.guild.id
    registered = get_team_role_ids(guild_id)

    if not registered:
        await interaction.response.send_message(
            "チームロールがまだ登録されていません。",
            ephemeral=True,
        )
        return

    # 特定のチームの得点を表示
    if role is not None:
        if role.id not in registered:
            await interaction.response.send_message(
                "このロールはチームロールとして登録されていません。",
                ephemeral=True,
            )
            return

        points = get_team_score(guild_id, role.id)

        embed = discord.Embed(
            title="🏆 チーム得点",
            description=(
                f"{role.mention}\n"
                f"**{points:,}点**"
            ),
            color=discord.Color.gold(),
        )

        await interaction.response.send_message(
            embed=embed,
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    # 全チームの得点ランキングを表示
    rows = db.execute(
        """
        SELECT role_id, points
        FROM scores
        WHERE guild_id=?
        """,
        (guild_id,),
    ).fetchall()

    score_map = {
        role_id: points
        for role_id, points in rows
    }

    ranking = []

    for role_id in registered:
        team_role = interaction.guild.get_role(role_id)

        # 削除済みロールはスキップ
        if team_role is None:
            continue

        points = score_map.get(role_id, 0)
        ranking.append((team_role, points))

    ranking.sort(
        key=lambda item: (-item[1], item[0].name.lower())
    )

    medals = ["🥇", "🥈", "🥉"]

    lines = []

    for index, (team_role, points) in enumerate(ranking, start=1):
        if index <= 3:
            rank_label = medals[index - 1]
        else:
            rank_label = f"**{index}位**"

        lines.append(
            f"{rank_label} {team_role.mention} — **{points:,}点**"
        )

    embed = discord.Embed(
        title="🏆 MKUC チーム得点ランキング",
        description="\n".join(lines),
        color=discord.Color.gold(),
        timestamp=now_jst(),
    )

    embed.set_footer(
        text=f"登録チーム数：{len(ranking)}"
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="byepriority-uc",
    description="2000点以上のチームを優先的に除外する設定を切り替えます",
)
@app_commands.describe(enabled="有効にするかどうか")
async def byepriority_uc(
    interaction: discord.Interaction,
    enabled: bool,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    if not await require_admin(interaction):
        return

    set_setting(
        interaction.guild.id,
        "high_score_bye",
        "true" if enabled else "false",
    )

    status = "有効" if enabled else "無効"

    await interaction.response.send_message(
        f"✅ 2000点モードを **{status}** にしました。\n"
        "有効時は、除外候補の中に2000点以上のチームがいれば、"
        "その中から挙手が最も遅いチームを優先して除外します。",
        ephemeral=True,
    )


@bot.tree.command(
    name="requiredgames-uc",
    description="規定試合数を設定します（0で判定を無効化）",
)
@app_commands.describe(
    count="規定試合数。0なら規定試合数による除外判定を無効化",
)
async def requiredgames_uc(
    interaction: discord.Interaction,
    count: app_commands.Range[int, 0, 100],
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    if not await require_admin(interaction):
        return

    set_setting(
        interaction.guild.id,
        "required_games",
        count,
    )

    if count == 0:
        message = "規定試合数による除外判定を無効にしました。"
    else:
        message = (
            f"規定試合数を **{count}試合** に設定しました。\n"
            "大会全体でこの試合数に到達したチームを、"
            "奇数時の除外候補として優先します。"
        )

    await interaction.response.send_message(
        f"✅ {message}",
        ephemeral=True,
    )


# ============================================================
# マッチング用データ
# ============================================================

def get_eligible_teams(guild_id, date, slot):
    rows = db.execute(
        """
        SELECT role_id
        FROM gestures
        WHERE guild_id=? AND date=? AND slot=?
        """,
        (guild_id, date, slot),
    ).fetchall()

    registered = get_team_role_ids(guild_id)

    # 挙手順を保持する。created_atが同じ場合はrole_idで安定化する。
    ordered_rows = db.execute(
        """
        SELECT role_id
        FROM gestures
        WHERE guild_id=? AND date=? AND slot=?
        ORDER BY created_at ASC, role_id ASC
        """,
        (guild_id, date, slot),
    ).fetchall()

    return [
        row[0]
        for row in ordered_rows
        if row[0] in registered
    ]


def get_exclusion_candidates(guild_id, teams):
    """
    規定試合数に到達したチームが存在する場合は、
    到達済みチームのみを除外候補とする。

    到達済みチームがいない場合は、全チームを候補とする。
    """

    required = get_required_games(guild_id)

    if required <= 0:
        return list(teams)

    reached = [
        role_id
        for role_id in teams
        if get_total_matches(guild_id, role_id) >= required
    ]

    return reached if reached else list(teams)


def get_signup_order(guild_id, date, slot, role_id):
    row = db.execute(
        """
        SELECT created_at
        FROM gestures
        WHERE guild_id=? AND date=? AND slot=? AND role_id=?
        """,
        (guild_id, date, slot, role_id),
    ).fetchone()

    return row[0] if row and row[0] else "9999-12-31T23:59:59+09:00"


def choose_bye_team(guild_id, date, slot, teams):
    """
    奇数チームの除外先を決定する。

    通常モード：
      規定試合数による除外候補のうち、挙手が最も遅いチーム。

    2000点モード：
      除外候補の中に2000点以上のチームがいれば、
      その中で挙手が最も遅いチーム。
      いなければ除外候補全体から挙手が最も遅いチーム。

    3チームの場合の再戦回避は、この関数の後に別途処理する。
    """

    candidates = get_exclusion_candidates(guild_id, teams)

    if high_score_mode_enabled(guild_id):
        high_score_candidates = [
            role_id
            for role_id in candidates
            if get_team_score(guild_id, role_id) >= HIGH_SCORE_THRESHOLD
        ]

        if high_score_candidates:
            candidates = high_score_candidates

    # 挙手時刻が遅い順。同時刻ならrole_idで安定化。
    return max(
        candidates,
        key=lambda role_id: (
            get_signup_order(guild_id, date, slot, role_id),
            role_id,
        ),
    )


def choose_bye_for_three(guild_id, date, slot, teams):
    """
    3チーム専用処理。

    1. 規定試合数による除外候補を決める。
    2. 2000点モードが有効なら、候補内の2000点以上を優先する。
    3. 原則として候補のうち挙手が最も遅いチームを仮除外する。
    4. 各候補を除外した場合の残り2チームの当日対戦回数を比較。
    5. 最小回数が同じ場合は、挙手が最も遅いチームを除外する。
    """

    candidates = get_exclusion_candidates(guild_id, teams)

    if high_score_mode_enabled(guild_id):
        high_score_candidates = [
            role_id
            for role_id in candidates
            if get_team_score(guild_id, role_id) >= HIGH_SCORE_THRESHOLD
        ]

        if high_score_candidates:
            candidates = high_score_candidates

    # 仮除外チームを最初に決める。
    provisional = max(
        candidates,
        key=lambda role_id: (
            get_signup_order(guild_id, date, slot, role_id),
            role_id,
        ),
    )

    options = []

    for bye_role in candidates:
        remaining = [
            role_id
            for role_id in teams
            if role_id != bye_role
        ]

        if len(remaining) != 2:
            continue

        same_day_count = get_daily_pair_count(
            guild_id,
            date,
            remaining[0],
            remaining[1],
        )

        options.append(
            (
                same_day_count,
                get_signup_order(
                    guild_id,
                    date,
                    slot,
                    bye_role,
                ),
                bye_role,
            )
        )

    if not options:
        return provisional

    # 同日対戦回数が最小の案を優先。
    minimum_count = min(item[0] for item in options)

    best_options = [
        item
        for item in options
        if item[0] == minimum_count
    ]

    # 同点なら挙手が最も遅いチームを除外。
    selected = max(
        best_options,
        key=lambda item: (item[1], item[2]),
    )[2]

    return selected


# ============================================================
# 対戦カード最適化
# ============================================================

def pair_cost(guild_id, date, role_a, role_b):
    """
    対戦カード1組の評価。

    優先順位：
      1. 同日再戦を避ける
      2. 同じ相手との3回目の対戦を避ける
      3. 未対戦の組み合わせを増やす
      4. 過去の対戦回数を減らす
      5. 直近に対戦した相手を避ける
    """

    same_day = get_daily_pair_count(
        guild_id,
        date,
        role_a,
        role_b,
    )

    total_meetings = get_previous_meetings(
        guild_id,
        role_a,
        role_b,
    )

    # 同じ日にすでに2回対戦していれば、今回が同日3回目。
    third_encounter = 1 if same_day >= 2 else 0

    # 大会全体で対戦したことがない相手を優先。
    never_met = 0 if total_meetings == 0 else 1

    # 直近の時間帯に対戦した相手を避ける。
    latest = get_latest_meeting(
        guild_id,
        role_a,
        role_b,
    )

    recent_penalty = 0

    if latest and latest[0] == date:
        if latest[1] in ("21", "22", "23"):
            recent_penalty = 1

    return (
        1 if same_day > 0 else 0,
        third_encounter,
        never_met,
        total_meetings,
        recent_penalty,
    )


def add_cost(left, right):
    return tuple(a + b for a, b in zip(left, right))


def create_pairing_candidate(guild_id, date, teams):
    """
    1つの候補を生成する。

    各段階で最も評価のよい相手を選びつつ、
    同点時にランダム性を持たせる。
    """

    remaining = teams[:]
    random.shuffle(remaining)

    pairs = []

    while len(remaining) >= 2:
        team_a = remaining.pop(0)

        scored = []

        for team_b in remaining:
            cost = pair_cost(
                guild_id,
                date,
                team_a,
                team_b,
            )

            scored.append(
                (cost, random.random(), team_b)
            )

        scored.sort(key=lambda item: (item[0], item[1]))

        team_b = scored[0][2]
        remaining.remove(team_b)

        pairs.append((team_a, team_b))

    return pairs


def evaluate_pairing(guild_id, date, pairs):
    total = (0, 0, 0, 0, 0)

    for role_a, role_b in pairs:
        total = add_cost(
            total,
            pair_cost(guild_id, date, role_a, role_b),
        )

    return total


def create_pairs(guild_id, date, teams):
    """
    複数の組み合わせ候補を生成し、
    評価が最もよいものを採用する。

    評価値が完全に同じ候補の中ではランダム性を残す。
    """

    if len(teams) < 2:
        return []

    best_pairs = None
    best_cost = None

    attempts = min(
        PAIRING_ATTEMPTS,
        max(50, len(teams) * 10),
    )

    for _ in range(attempts):
        candidate = create_pairing_candidate(
            guild_id,
            date,
            teams,
        )

        cost = evaluate_pairing(
            guild_id,
            date,
            candidate,
        )

        if best_cost is None or cost < best_cost:
            best_cost = cost
            best_pairs = candidate

    return best_pairs or []


# ============================================================
# 定時ジョブ管理
# ============================================================

def job_completed(guild_id, date, job):
    row = db.execute(
        """
        SELECT 1
        FROM job_runs
        WHERE guild_id=? AND date=? AND job=?
        """,
        (guild_id, date, job),
    ).fetchone()

    return row is not None


def mark_job_completed(guild_id, date, job):
    db_execute(
        """
        INSERT OR IGNORE INTO job_runs (
            guild_id, date, job, completed_at
        )
        VALUES (?, ?, ?, ?)
        """,
        (
            guild_id,
            date,
            job,
            now_jst().isoformat(),
        ),
    )


async def announce_matching_result(
    guild,
    slot,
    match_count,
    bye_count,
):
    config = get_config(guild.id)

    if not config:
        return

    channel = await get_text_channel(
        guild,
        config["signup_channel_id"],
    )

    if channel is None:
        return

    await channel.send(
        f"🔀 **{slot}時枠の組み分けが完了しました。**\n"
        f"対戦数：{match_count}組\n"
        f"組み分け漏れ：{bye_count}チーム\n"
        f"対戦部屋は **{slot}時58分** に作成されます。",
        allowed_mentions=discord.AllowedMentions.none(),
    )


async def run_matching(guild, date, slot):
    """
    各時間帯の55分に実行する。
    確定した対戦カードはDBに保存し、以降変更しない。
    """

    job_name = f"match-{slot}"

    if job_completed(guild.id, date, job_name):
        return

    # すでにカードが存在する場合は、再生成しない。
    existing = db.execute(
        """
        SELECT COUNT(*)
        FROM scheduled_matches
        WHERE guild_id=? AND date=? AND slot=?
        """,
        (guild.id, date, slot),
    ).fetchone()[0]

    if existing:
        mark_job_completed(guild.id, date, job_name)
        return

    teams = get_eligible_teams(guild.id, date, slot)

    if not teams:
        mark_job_completed(guild.id, date, job_name)
        await announce_matching_result(guild, slot, 0, 0)
        return

    bye_team = None

    if len(teams) % 2 == 1:
        if len(teams) == 3:
            bye_team = choose_bye_for_three(
                guild.id,
                date,
                slot,
                teams,
            )
        else:
            bye_team = choose_bye_team(
                guild.id,
                date,
                slot,
                teams,
            )

        teams.remove(bye_team)

        db_execute(
            """
            INSERT OR IGNORE INTO unmatched_teams (
                guild_id, date, slot, role_id
            )
            VALUES (?, ?, ?, ?)
            """,
            (guild.id, date, slot, bye_team),
        )

    pairs = create_pairs(guild.id, date, teams)

    # カードと履歴をまとめて保存する。
    # 途中で失敗した場合に重複履歴が生じにくいよう、トランザクションを使う。
    try:
        db.execute("BEGIN")

        for room_no, (role_a, role_b) in enumerate(pairs, start=1):
            host_role_id = random.choice([role_a, role_b])

            db.execute(
                """
                INSERT OR IGNORE INTO scheduled_matches (
                    guild_id, date, slot, room_no,
                    role_a, role_b, host_role_id, status
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, 'matched')
                """,
                (
                    guild.id,
                    date,
                    slot,
                    room_no,
                    role_a,
                    role_b,
                    host_role_id,
                ),
            )

            db.execute(
                """
                INSERT INTO match_history (
                    guild_id, date, slot, role_a, role_b
                )
                VALUES (?, ?, ?, ?, ?)
                """,
                (
                    guild.id,
                    date,
                    slot,
                    role_a,
                    role_b,
                ),
            )

        db.commit()

    except Exception:
        db.rollback()
        log.exception("対戦カードの保存に失敗しました。")
        return

    mark_job_completed(guild.id, date, job_name)

    await announce_matching_result(
        guild,
        slot,
        len(pairs),
        1 if bye_team is not None else 0,
    )

    log.info(
        "組み分け完了: guild=%s date=%s slot=%s matches=%s bye=%s",
        guild.id,
        date,
        slot,
        len(pairs),
        bye_team,
    )


# ============================================================
# 追加対戦
# ============================================================

@bot.tree.command(
    name="late-match-uc",
    description="締切後の追加対戦カードを登録します",
)
@app_commands.describe(
    time_slot="追加する時間帯",
    team_a="対戦するチームA",
    team_b="対戦するチームB",
)
@app_commands.choices(
    time_slot=[
        app_commands.Choice(name="21時", value="21"),
        app_commands.Choice(name="22時", value="22"),
        app_commands.Choice(name="23時", value="23"),
    ],
)
async def late_match_uc(
    interaction: discord.Interaction,
    time_slot: app_commands.Choice[str],
    team_a: discord.Role,
    team_b: discord.Role,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。",
            ephemeral=True,
        )
        return

    if not await require_admin(interaction):
        return

    guild_id = interaction.guild.id
    date = today_str()
    slot = time_slot.value

    if team_a.id == team_b.id:
        await interaction.response.send_message(
            "同じチーム同士の対戦は登録できません。",
            ephemeral=True,
        )
        return

    registered = get_team_role_ids(guild_id)

    if team_a.id not in registered or team_b.id not in registered:
        await interaction.response.send_message(
            "両方のロールをチームロールとして登録してください。",
            ephemeral=True,
        )
        return

    # 同じ時間帯にすでにカードが確定しているチームは登録不可。
    used = db.execute(
        """
        SELECT 1
        FROM scheduled_matches
        WHERE guild_id=? AND date=? AND slot=?
          AND (
              role_a IN (?, ?)
              OR role_b IN (?, ?)
          )
        LIMIT 1
        """,
        (
            guild_id,
            date,
            slot,
            team_a.id,
            team_b.id,
            team_a.id,
            team_b.id,
        ),
    ).fetchone()

    if used:
        await interaction.response.send_message(
            "どちらかのチームは、この時間帯の対戦カードにすでに登録されています。",
            ephemeral=True,
        )
        return

    # 追加カードは確定済みカードを変更せず、末尾のRoom番号を使う。
    row = db.execute(
        """
        SELECT COALESCE(MAX(room_no), 0)
        FROM scheduled_matches
        WHERE guild_id=? AND date=? AND slot=?
        """,
        (guild_id, date, slot),
    ).fetchone()

    room_no = row[0] + 1
    host_role_id = random.choice([team_a.id, team_b.id])

    try:
        db.execute("BEGIN")

        db.execute(
            """
            INSERT INTO scheduled_matches (
                guild_id, date, slot, room_no,
                role_a, role_b, host_role_id, status
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, 'matched')
            """,
            (
                guild_id,
                date,
                slot,
                room_no,
                team_a.id,
                team_b.id,
                host_role_id,
            ),
        )

        db.execute(
            """
            INSERT INTO match_history (
                guild_id, date, slot, role_a, role_b
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                guild_id,
                date,
                slot,
                team_a.id,
                team_b.id,
            ),
        )

        db.commit()

    except Exception:
        db.rollback()
        log.exception("追加対戦の登録に失敗しました。")

        await interaction.response.send_message(
            "追加対戦の登録に失敗しました。",
            ephemeral=True,
        )
        return

    # 58分を過ぎていれば、その場で部屋を作成する。
    current = now_jst()
    deadline_hour, _ = DEADLINES[slot]

    if (
        current.hour > deadline_hour
        or (
            current.hour == deadline_hour
            and current.minute >= ROOM_MINUTE
        )
    ):
        channel = await create_match_room(
            interaction.guild,
            date,
            slot,
            room_no,
            team_a.id,
            team_b.id,
            host_role_id,
        )

        if channel:
            db_execute(
                """
                UPDATE scheduled_matches
                SET channel_id=?, status='created'
                WHERE guild_id=? AND date=? AND slot=? AND room_no=?
                """,
                (
                    channel.id,
                    guild_id,
                    date,
                    slot,
                    room_no,
                ),
            )

    await interaction.response.send_message(
        f"✅ 追加対戦を登録しました。\n"
        f"{team_a.mention} vs {team_b.mention}\n"
        f"時間帯：{slot}時枠 / Room {room_no}\n"
        "確定済みの対戦カードは変更していません。",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ============================================================
# 対戦部屋作成
# ============================================================

async def create_match_room(
    guild,
    date,
    slot,
    room_no,
    role_a_id,
    role_b_id,
    host_role_id,
):
    config = get_config(guild.id)

    if not config:
        log.error("サーバー設定がないため部屋を作成できません。")
        return None

    role_a = guild.get_role(role_a_id)
    role_b = guild.get_role(role_b_id)
    host_role = guild.get_role(host_role_id)

    if role_a is None or role_b is None or host_role is None:
        log.error("対戦ロールが見つかりません。")
        return None

    category = await get_category(
        guild,
        config["room_category_id"],
    )

    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False,
        ),
    }

    bot_member = guild.me

    if bot_member:
        overwrites[bot_member] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        )

    for role in {role_a, role_b}:
        overwrites[role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            attach_files=True,
            embed_links=True,
            use_application_commands=True,
        )

    # 運営ロールを指定している場合、そのロールも閲覧可能にする。
    staff_role_id = config.get("staff_role_id")
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None

    if staff_role:
        overwrites[staff_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
            manage_channels=True,
        )

    channel_name = f"{date}-{slot}-room-{room_no}".lower()

    try:
        channel = await guild.create_text_channel(
            name=channel_name,
            category=category,
            overwrites=overwrites,
            reason=f"MKUC {date} {slot}時枠 Room {room_no}",
        )

    except discord.Forbidden:
        log.error(
            "対戦部屋を作成できません。Botにチャンネル管理権限が必要です。"
        )
        return None

    except discord.HTTPException:
        log.exception("対戦部屋の作成に失敗しました。")
        return None

    message = (
        f"{role_a.mention} {role_b.mention}\n\n"
        f"**MKUC 対戦部屋**\n"
        f"日程：{date}\n"
        f"時間：{slot}時枠\n"
        f"Room：{room_no}\n\n"
        f"**Host：** {host_role.mention}\n\n"
        "150cc / 2チーム戦 / ノーマルアイテム / CPUなし\n"
        "12レースで対戦してください。\n"
        "インターバルは10秒です。\n\n"
        "リザルト提出は、MKUCサーバーのリザルト投稿用チャンネルで行ってください。"
    )

    try:
        await channel.send(
            message,
            allowed_mentions=discord.AllowedMentions(
                roles=True,
                users=False,
                everyone=False,
            ),
        )
    except discord.HTTPException:
        log.exception("対戦部屋の案内メッセージ送信に失敗しました。")

    return channel


async def run_room_creation(guild, date, slot):
    """
    58分の対戦部屋作成処理。

    作成済みの部屋が存在すれば再作成しない。
    作成失敗したカードは次回の処理で再試行できるようにする。
    """

    job_name = f"rooms-{slot}"

    rows = db.execute(
        """
        SELECT
            id, room_no, role_a, role_b,
            host_role_id, channel_id, status
        FROM scheduled_matches
        WHERE guild_id=? AND date=? AND slot=?
        ORDER BY room_no
        """,
        (guild.id, date, slot),
    ).fetchall()

    all_successful = True

    for (
        match_id,
        room_no,
        role_a,
        role_b,
        host_role_id,
        existing_channel_id,
        status,
    ) in rows:

        if existing_channel_id:
            existing_channel = guild.get_channel(existing_channel_id)

            if existing_channel is not None:
                continue

        channel = await create_match_room(
            guild,
            date,
            slot,
            room_no,
            role_a,
            role_b,
            host_role_id,
        )

        if channel is None:
            all_successful = False
            continue

        db_execute(
            """
            UPDATE scheduled_matches
            SET channel_id=?, status='created'
            WHERE id=?
            """,
            (channel.id, match_id),
        )

    if all_successful:
        mark_job_completed(guild.id, date, job_name)

    log.info(
        "対戦部屋作成処理: guild=%s date=%s slot=%s success=%s",
        guild.id,
        date,
        slot,
        all_successful,
    )


# ============================================================
# 募集開始・挙手削除
# ============================================================

async def open_daily_signup(guild, date):
    job_name = "daily-open"

    if job_completed(guild.id, date, job_name):
        return

    config = get_config(guild.id)

    if not config:
        return

    channel = await get_text_channel(
        guild,
        config["signup_channel_id"],
    )

    if channel is None:
        return

    participant_role = guild.get_role(
        config["participant_role_id"]
    )

    if participant_role is None:
        await channel.send(
            "⚠️ 参加者ロールが見つかりません。運営に設定を確認してください。",
            allowed_mentions=discord.AllowedMentions.none(),
        )
        return

    try:
        await channel.send(
            f"{participant_role.mention}\n"
            "**12時になりましたので、本日の募集を開始します！**\n"
            "参加可能な時間帯に `/c-uc` で挙手してください。\n"
            "21時枠：20:53締切\n"
            "22時枠：21:53締切\n"
            "23時枠：22:53締切",
            allowed_mentions=discord.AllowedMentions(
                roles=True,
                users=False,
                everyone=False,
            ),
        )

        mark_job_completed(guild.id, date, job_name)
        await refresh_gesture_status(guild)

    except discord.HTTPException:
        log.exception("募集開始通知に失敗しました。")


async def clear_old_gestures(guild, date):
    job_name = "daily-clear"

    if job_completed(guild.id, date, job_name):
        return

    db_execute(
        """
        DELETE FROM gestures
        WHERE guild_id=? AND date<?
        """,
        (guild.id, date),
    )

    db_execute(
        """
        DELETE FROM unmatched_teams
        WHERE guild_id=? AND date<?
        """,
        (guild.id, date),
    )

    mark_job_completed(guild.id, date, job_name)

    await refresh_gesture_status(guild)

    log.info("前日までの挙手データを削除しました。guild=%s", guild.id)


# ============================================================
# 定時処理
# ============================================================

async def scheduler_loop():
    """
    日本時間で定時処理を実行する。

    毎日08:00：前日までの挙手削除
    毎日12:00：募集開始通知

    20:54：21時枠マッチング
    20:58：21時枠の部屋作成

    21:54：22時枠マッチング
    21:58：22時枠の部屋作成

    22:54：23時枠マッチング
    22:58：23時枠の部屋作成
    """

    await bot.wait_until_ready()

    last_minute_key = None

    while not bot.is_closed():
        current = now_jst()
        minute_key = current.strftime("%Y-%m-%d %H:%M")

        if minute_key == last_minute_key:
            await asyncio.sleep(2)
            continue

        last_minute_key = minute_key
        date = current.strftime("%Y-%m-%d")

        for guild in bot.guilds:
            try:
                if current.hour == CLEAR_HOUR and current.minute == 0:
                    await clear_old_gestures(guild, date)

                if current.hour == OPEN_HOUR and current.minute == 0:
                    await open_daily_signup(guild, date)

                for slot in TIME_SLOTS:
                    deadline_hour, _ = DEADLINES[slot]

                    if (
                        current.hour == deadline_hour
                        and current.minute == MATCH_MINUTE
                    ):
                        await run_matching(guild, date, slot)

                    if (
                        current.hour == deadline_hour
                        and current.minute == ROOM_MINUTE
                    ):
                        await run_room_creation(guild, date, slot)

            except Exception:
                log.exception(
                    "定時処理でエラーが発生しました。guild=%s",
                    guild.id,
                )

        await asyncio.sleep(2)


# ============================================================
# Webサーバー（Render用）
# ============================================================

async def handle_health(request):
    return web.Response(text="MKUC 2026 Bot is running!")


async def start_web_server():
    app = web.Application()

    app.add_routes(
        [
            web.get("/", handle_health),
            web.get("/health", handle_health),
        ]
    )

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    log.info("Webサーバーがポート %s で起動しました。", PORT)

    return runner


# ============================================================
# Discordイベント
# ============================================================

@bot.event
async def on_ready():
    log.info(
        "Logged in as %s (ID: %s)",
        bot.user,
        bot.user.id if bot.user else "unknown",
    )


@bot.event
async def on_guild_join(guild):
    log.info(
        "サーバーに参加しました: %s (%s)",
        guild.name,
        guild.id,
    )


# ============================================================
# Bot起動
# ============================================================

async def main():
    global scheduler_task

    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN が設定されていません。"
        )

    web_runner = None

    try:
        await bot.login(TOKEN)

        await bot.tree.sync()

        log.info("スラッシュコマンドを同期しました。")

        web_runner = await start_web_server()

        scheduler_task = asyncio.create_task(
            scheduler_loop()
        )

        await bot.connect()

    finally:
        if scheduler_task is not None:
            scheduler_task.cancel()

            try:
                await scheduler_task
            except asyncio.CancelledError:
                pass

        if web_runner is not None:
            await web_runner.cleanup()

        await bot.close()
        db.close()


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        log.info("Botを終了しました。")