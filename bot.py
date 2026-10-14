import asyncio

import datetime

import logging

import os

import random

import re

import sqlite3

import discord

from discord import app_commands

from discord.ext import commands

from aiohttp import web

# ============================================================

# 設定

# ============================================================

TOKEN = os.getenv("DISCORD_TOKEN")

PORT = int(os.getenv("PORT", "8080"))

DB_PATH = os.getenv("MKUC_DB_PATH", "mkuc_bot.db")

JST = datetime.timezone(datetime.timedelta(hours=9))

TIME_SLOTS = ["21", "22", "23"]

# 受付終了時刻

DEADLINES = {

    "21": (20, 53),

    "22": (21, 53),

    "23": (22, 53),

}

# マッチング実行時刻

MATCH_MINUTE = 55

# 対戦部屋作成時刻

ROOM_MINUTE = 58

# 募集開始・挙手削除時刻

OPEN_HOUR = 12

CLEAR_HOUR = 8

# 2000点超えのチームを優先的に組み分けから外す

# True: 2000点超えのチームを優先して組み分け漏れにする

# False: 得点を考慮せずランダムに組み分ける

PRIORITIZE_HIGH_SCORE_BYE = True

# 挙手一覧を探す際に確認する過去メッセージ数

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

        room_category_id INTEGER

    );

    CREATE TABLE IF NOT EXISTS gestures (

        guild_id INTEGER NOT NULL,

        date TEXT NOT NULL,

        slot TEXT NOT NULL,

        role_id INTEGER NOT NULL,

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

        """

        SELECT signup_channel_id, participant_role_id, room_category_id

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

def set_config(

    guild_id,

    signup_channel_id,

    participant_role_id,

    room_category_id,

):

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

        (

            guild_id,

            signup_channel_id,

            participant_role_id,

            room_category_id,

        ),

    )

def get_team_role_ids(guild_id):

    rows = db.execute(

        """

        SELECT role_id

        FROM team_roles

        WHERE guild_id=?

        """,

        (guild_id,),

    ).fetchall()

    return {row[0] for row in rows}

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

def get_last_opponent_count(guild_id, role_a, role_b):

    """

    過去の対戦回数。

    回数が少ない相手を優先するために使用する。

    """

    return get_previous_meetings(guild_id, role_a, role_b)

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

def high_score_bye_enabled(guild_id):

    value = get_setting(

        guild_id,

        "prioritize_high_score_bye",

        str(PRIORITIZE_HIGH_SCORE_BYE),

    )

    return value.lower() in ("true", "1", "yes", "on")

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

def is_admin(interaction: discord.Interaction):

    return (

        interaction.guild is not None

        and interaction.user.guild_permissions.manage_guild

    )

async def require_admin(interaction: discord.Interaction):

    if not is_admin(interaction):

        await interaction.response.send_message(

            "運営のみ使用できます。サーバー管理権限が必要です。",

            ephemeral=True,

        )

        return False

    return True

def get_team_role_from_interaction(interaction):

    """

    実行者が持つ登録済みチームロールを取得する。

    チームロールが0個または複数ならエラー。

    """

    guild_id = interaction.guild_id

    registered = get_team_role_ids(guild_id)

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

    if isinstance(channel, discord.TextChannel):

        return channel

    return None

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

    if isinstance(channel, discord.CategoryChannel):

        return channel

    return None

# ============================================================

# 挙手一覧

# ============================================================

def create_gesture_embed(guild_id):

    """

    挙手一覧には時間帯とチーム数だけを表示する。

    チーム名・ロール名・ロールIDは表示しない。

    """

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

        # 各時間帯を縦並びにして見やすくする

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

    """

    過去のBotが投稿した挙手一覧を整理する。

    挙手一覧以外のBotメッセージは削除しない。

    """

    try:

        async for message in channel.history(

            limit=HISTORY_LIMIT

        ):

            if message.author.id != bot.user.id:

                continue

            is_status = any(

                embed.title == STATUS_TITLE

                for embed in message.embeds

            )

            if not is_status:

                continue

            try:

                await message.delete()

            except discord.NotFound:

                pass

            except discord.Forbidden:

                log.warning(

                    "過去の挙手一覧を削除できません。"

                    "メッセージ管理権限を確認してください。"

                )

            except discord.HTTPException as exc:

                log.warning(

                    "挙手一覧の削除に失敗しました: %s",

                    exc,

                )

    except discord.Forbidden:

        log.warning(

            "チャンネル履歴を確認できません。"

            "メッセージ履歴を読む権限を確認してください。"

        )

    except discord.HTTPException as exc:

        log.warning(

            "チャンネル履歴の取得に失敗しました: %s",

            exc,

        )

async def refresh_gesture_status(guild, current_channel=None):

    """

    挙手一覧を更新する。

    更新するたびに古い一覧を削除し、

    新しい一覧を投稿する。

    """

    if guild is None:

        return

    config = get_config(guild.id)

    if not config:

        log.warning(

            "サーバー %s の設定がありません。",

            guild.id,

        )

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

        log.warning(

            "挙手一覧チャンネルを取得できません。guild_id=%s",

            guild.id,

        )

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

        # DBに記録されている一覧を削除

        if old_row:

            old_channel_id, old_message_id = old_row

            old_channel = await get_text_channel(

                guild,

                old_channel_id,

            )

            if old_channel:

                try:

                    old_message = await old_channel.fetch_message(

                        old_message_id

                    )

                    await old_message.delete()

                except discord.NotFound:

                    pass

                except discord.Forbidden:

                    log.warning(

                        "以前の一覧を削除できません。"

                        "メッセージ管理権限を確認してください。"

                    )

                except discord.HTTPException as exc:

                    log.warning(

                        "以前の一覧の削除に失敗しました: %s",

                        exc,

                    )

        # 古いバージョンの一覧も整理する

        await cleanup_old_status_messages(channel)

        embed = create_gesture_embed(guild.id)

        try:

            message = await channel.send(

                embed=embed,

                allowed_mentions=discord.AllowedMentions.none(),

            )

            db_execute(

                """

                INSERT INTO status_messages (

                    guild_id,

                    channel_id,

                    message_id,

                    date

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

        except discord.Forbidden:

            log.error(

                "挙手一覧を投稿できません。"

                "閲覧・送信・埋め込みリンク・メッセージ管理権限を確認してください。"

            )

        except discord.HTTPException as exc:

            log.error(

                "挙手一覧の投稿に失敗しました: %s",

                exc,

            )

# ============================================================

# 挙手コマンド

# ============================================================

def parse_times(times):

    """

    入力例：

    21

    21,22

    21-23

    21,23

    """

    times = times.strip().replace(" ", "")

    if not times:

        raise ValueError("時間を指定してください。")

    if "-" in times:

        parts = times.split("-")

        if len(parts) != 2:

            raise ValueError(

                "形式が正しくありません。例：21-23"

            )

        try:

            start, end = map(int, parts)

        except ValueError:

            raise ValueError(

                "時間は数字で指定してください。例：21-23"

            )

        if start > end:

            raise ValueError(

                "開始時間は終了時間以前にしてください。"

            )

        slots = [

            str(hour)

            for hour in range(start, end + 1)

        ]

    else:

        slots = times.split(",")

    if any(slot not in TIME_SLOTS for slot in slots):

        raise ValueError(

            "指定できる時間は21、22、23です。"

        )

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

    times="例：21,22 または 21-23 または 23"

)

async def c_uc(

    interaction: discord.Interaction,

    times: str,

):

    if interaction.guild is None:

        await interaction.response.send_message(

            "サーバー内で使用してください。",

            ephemeral=True,

        )

        return

    team_role, error = get_team_role_from_interaction(

        interaction

    )

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

    registered = []

    rejected = []

    already_registered = []

    for slot in slots:

        if is_slot_closed(slot, current):

            rejected.append(slot)

            continue

        cursor = db_execute(

            """

            INSERT OR IGNORE INTO gestures (

                guild_id,

                date,

                slot,

                role_id

            )

            VALUES (?, ?, ?, ?)

            """,

            (

                interaction.guild.id,

                today,

                slot,

                team_role.id,

            ),

        )

        if cursor.rowcount > 0:

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

    if not messages:

        messages.append("登録できる時間帯がありませんでした。")

    await interaction.response.send_message(

        "\n".join(messages),

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

    time_slot="取り下げる時間帯"

)

@app_commands.choices(

    time_slot=[

        app_commands.Choice(name="21時", value="21"),

        app_commands.Choice(name="22時", value="22"),

        app_commands.Choice(name="23時", value="23"),

    ]

)

async def d_uc(

    interaction: discord.Interaction,

    time_slot: app_commands.Choice[str],

):

    if interaction.guild is None:

        await interaction.response.send_message(

            "サーバー内で使用してください。",

            ephemeral=True,

        )

        return

    team_role, error = get_team_role_from_interaction(

        interaction

    )

    if error:

        await interaction.response.send_message(

            error,

            ephemeral=True,

        )

        return

    slot = time_slot.value

    current = now_jst()

    if is_slot_closed(slot, current):

        await interaction.response.send_message(

            "この時間帯はすでに締切済みです。",

            ephemeral=True,

        )

        return

    cursor = db_execute(

        """

        DELETE FROM gestures

        WHERE guild_id=?

          AND date=?

          AND slot=?

          AND role_id=?

        """,

        (

            interaction.guild.id,

            current.strftime("%Y-%m-%d"),

            slot,

            team_role.id,

        ),

    )

    if cursor.rowcount == 0:

        await interaction.response.send_message(

            "この時間帯には挙手していません。",

            ephemeral=True,

        )

        return

    await interaction.response.send_message(

        f"🗑️ {slot}時枠の挙手を取り下げました。",

        ephemeral=True,

    )

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

)

async def setup_uc(

    interaction: discord.Interaction,

    signup_channel: discord.TextChannel,

    participant_role: discord.Role,

    room_category: discord.CategoryChannel = None,

):

    if not await require_admin(interaction):

        return

    set_config(

        interaction.guild.id,

        signup_channel.id,

        participant_role.id,

        room_category.id if room_category else None,

    )

    await interaction.response.send_message(

        "✅ 設定を保存しました。\n"

        f"挙手一覧・募集通知用チャンネル：{signup_channel.mention}\n"

        f"参加者ロール：{participant_role.mention}\n"

        f"対戦部屋カテゴリ："

        f"{room_category.mention if room_category else '未設定'}",

        ephemeral=True,

    )

    await refresh_gesture_status(

        interaction.guild,

        signup_channel,

    )

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

    ]

)

async def teamrole_uc(

    interaction: discord.Interaction,

    action: app_commands.Choice[str],

    role: discord.Role,

):

    if not await require_admin(interaction):

        return

    if action.value == "add":

        register_team_role(

            interaction.guild.id,

            role.id,

        )

        # 未登録なら得点0で作成

        set_team_score(

            interaction.guild.id,

            role.id,

            get_team_score(interaction.guild.id, role.id),

        )

        await interaction.response.send_message(

            f"✅ チームロール {role.mention} を登録しました。",

            ephemeral=True,

        )

    else:

        unregister_team_role(

            interaction.guild.id,

            role.id,

        )

        await interaction.response.send_message(

            f"🗑️ チームロール {role.mention} の登録を解除しました。",

            ephemeral=True,

        )

@bot.tree.command(

    name="refresh-uc",

    description="挙手一覧を手動で更新します",

)

async def refresh_uc(

    interaction: discord.Interaction,

):

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

@bot.tree.command(

    name="clear-uc",

    description="本日の挙手をすべてクリアします",

)

async def clear_uc(

    interaction: discord.Interaction,

):

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

        (

            interaction.guild.id,

            today_str(),

        ),

    )

    db_execute(

        """

        DELETE FROM unmatched_teams

        WHERE guild_id=? AND date=?

        """,

        (

            interaction.guild.id,

            today_str(),

        ),

    )

    await refresh_gesture_status(

        interaction.guild,

        interaction.channel,

    )

    await interaction.response.send_message(

        "🧹 本日の挙手をすべてクリアしました。",

        ephemeral=True,

    )

# ============================================================

# 得点管理

# ============================================================

def parse_score_entries(guild, text):

    """

    例：

    @CHU 491 @KU 493

    ロールメンションと得点の組を複数指定できる。

    実際のロールメンション <@&123456789> に対応。

    ロール名を @CHU のように入力する形式にも対応する。

    """

    text = text.strip()

    if not text:

        raise ValueError(

            "入力してください。例：@CHU 491 @KU 493"

        )

    results = []

    # Discordの実際のロールメンション形式

    mention_pattern = re.compile(

        r"<@&(\d+)>\s*(-?\d+)"

    )

    for match in mention_pattern.finditer(text):

        role_id = int(match.group(1))

        points = int(match.group(2))

        role = guild.get_role(role_id)

        if role is None:

            raise ValueError(

                f"ロールID {role_id} が見つかりません。"

            )

        results.append((role, points))

    if results:

        return results

    # 手入力形式：@CHU 491 @KU 493

    # ロール名に空白がある場合は実際のロールメンションを使う。

    tokens = text.split()

    if len(tokens) % 2 != 0:

        raise ValueError(

            "形式が正しくありません。\n"

            "例：@CHU 491 @KU 493\n"

            "可能であればロールをメンションして入力してください。"

        )

    for index in range(0, len(tokens), 2):

        role_token = tokens[index]

        points_token = tokens[index + 1]

        try:

            points = int(points_token)

        except ValueError:

            raise ValueError(

                f"得点「{points_token}」が整数ではありません。"

            )

        role_name = role_token.removeprefix("@")

        role = next(

            (

                candidate

                for candidate in guild.roles

                if candidate.name == role_name

            ),

            None,

        )

        if role is None:

            raise ValueError(

                f"ロール「{role_name}」が見つかりません。"

            )

        results.append((role, points))

    return results

@bot.tree.command(

    name="score-uc",

    description="複数チームの得点を登録・更新します",

)

@app_commands.describe(

    entries="例：@CHU 491 @KU 493（複数指定可能）"

)

async def score_uc(

    interaction: discord.Interaction,

    entries: str,

):

    if interaction.guild is None:

        await interaction.response.send_message(

            "サーバー内で使用してください。",

            ephemeral=True,

        )

        return

    if not await require_admin(interaction):

        return

    try:

        parsed = parse_score_entries(

            interaction.guild,

            entries,

        )

    except ValueError as exc:

        await interaction.response.send_message(

            f"❌ {exc}",

            ephemeral=True,

        )

        return

    registered_team_ids = get_team_role_ids(

        interaction.guild.id

    )

    updated = []

    for role, points in parsed:

        if role.id not in registered_team_ids:

            await interaction.response.send_message(

                f"❌ {role.mention} はチームロールとして登録されていません。\n"

                "先に /teamrole-uc で登録してください。",

                ephemeral=True,

            )

            return

        set_team_score(

            interaction.guild.id,

            role.id,

            points,

        )

        updated.append(

            f"{role.mention}：**{points}点**"

        )

    await interaction.response.send_message(

        "✅ 得点を登録・更新しました。\n"

        + "\n".join(updated),

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

    set_team_score(

        interaction.guild.id,

        role.id,

        points,

    )

    await interaction.response.send_message(

        f"✅ {role.mention} の総得点を **{points}点** に変更しました。",

        ephemeral=True,

    )

@bot.tree.command(

    name="scoreview-uc",

    description="チームの登録得点を確認します",

)

@app_commands.describe(

    role="確認するチームロール",

)

async def scoreview_uc(

    interaction: discord.Interaction,

    role: discord.Role,

):

    if interaction.guild is None:

        await interaction.response.send_message(

            "サーバー内で使用してください。",

            ephemeral=True,

        )

        return

    if not await require_admin(interaction):

        return

    points = get_team_score(

        interaction.guild.id,

        role.id,

    )

    await interaction.response.send_message(

        f"{role.mention}：**{points}点**",

        ephemeral=True,

    )

@bot.tree.command(

    name="byepriority-uc",

    description="2000点超えのチームを組み分けから外す優先設定を切り替えます",

)

@app_commands.describe(

    enabled="有効にするかどうか",

)

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

        "prioritize_high_score_bye",

        "true" if enabled else "false",

    )

    status = "有効" if enabled else "無効"

    await interaction.response.send_message(

        f"✅ 2000点超え優先の組み分け漏れ設定を **{status}** にしました。",

        ephemeral=True,

    )

# ============================================================

# マッチング

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

    # 解除済みのチームロールは除外

    registered = get_team_role_ids(guild_id)

    return [

        row[0]

        for row in rows

        if row[0] in registered

    ]

def choose_bye_team(guild_id, teams):

    """

    奇数チームの場合、1チームを組み分け漏れにする。

    優先設定ON：

    2000点を超えているチームを優先して漏れさせる。

    該当チームが複数なら、その中からランダムに選ぶ。

    優先設定OFF：

    全チームからランダムに選ぶ。

    """

    if not teams:

        return None

    if high_score_bye_enabled(guild_id):

        high_score_teams = [

            role_id

            for role_id in teams

            if get_team_score(guild_id, role_id) > 2000

        ]

        if high_score_teams:

            return random.choice(high_score_teams)

    return random.choice(teams)

def create_pairs(guild_id, teams):

    """

    ランダム性を保ちつつ、過去に対戦していない相手や

    対戦回数が少ない相手を優先して組み合わせる。

    小規模なチーム数を想定したランダム貪欲法。

    """

    remaining = teams[:]

    random.shuffle(remaining)

    pairs = []

    while len(remaining) >= 2:

        team_a = remaining.pop(0)

        opponent_counts = [

            (

                get_last_opponent_count(

                    guild_id,

                    team_a,

                    candidate,

                ),

                random.random(),

                candidate,

            )

            for candidate in remaining

        ]

        opponent_counts.sort(

            key=lambda item: (item[0], item[1])

        )

        team_b = opponent_counts[0][2]

        remaining.remove(team_b)

        pairs.append((team_a, team_b))

    return pairs

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

            guild_id,

            date,

            job,

            completed_at

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

    message = (

        f"🔀 **{slot}時枠の組み分けが完了しました。**\n"

        f"対戦数：{match_count}組\n"

        f"組み分け漏れ：{bye_count}チーム\n"

        f"対戦部屋は **{slot}時58分** に作成されます。"

    )

    try:

        await channel.send(

            message,

            allowed_mentions=discord.AllowedMentions.none(),

        )

    except discord.HTTPException:

        log.exception(

            "組み分け結果の通知に失敗しました。"

        )

async def run_matching(guild, date, slot):

    """

    55分のマッチング処理。

    対戦組をDBに保存する。

    """

    job_name = f"match-{slot}"

    if job_completed(guild.id, date, job_name):

        return

    teams = get_eligible_teams(

        guild.id,

        date,

        slot,

    )

    if not teams:

        log.info(

            "%s %s時枠：挙手チームなし",

            date,

            slot,

        )

        mark_job_completed(

            guild.id,

            date,

            job_name,

        )

        return

    bye_team = None

    if len(teams) % 2 == 1:

        bye_team = choose_bye_team(

            guild.id,

            teams,

        )

        teams.remove(bye_team)

        db_execute(

            """

            INSERT OR IGNORE INTO unmatched_teams (

                guild_id,

                date,

                slot,

                role_id

            )

            VALUES (?, ?, ?, ?)

            """,

            (

                guild.id,

                date,

                slot,

                bye_team,

            ),

        )

    pairs = create_pairs(

        guild.id,

        teams,

    )

    for room_no, (role_a, role_b) in enumerate(

        pairs,

        start=1,

    ):

        host_role_id = random.choice(

            [role_a, role_b]

        )

        db_execute(

            """

            INSERT OR IGNORE INTO scheduled_matches (

                guild_id,

                date,

                slot,

                room_no,

                role_a,

                role_b,

                host_role_id,

                status

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

        # 対戦履歴を記録

        db_execute(

            """

            INSERT INTO match_history (

                guild_id,

                date,

                slot,

                role_a,

                role_b

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

    mark_job_completed(

        guild.id,

        date,

        job_name,

    )

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

        log.error(

            "サーバー %s の設定がないため部屋を作成できません。",

            guild.id,

        )

        return None

    role_a = guild.get_role(role_a_id)

    role_b = guild.get_role(role_b_id)

    host_role = guild.get_role(host_role_id)

    if role_a is None or role_b is None or host_role is None:

        log.error(

            "対戦ロールが見つかりません。guild=%s room=%s",

            guild.id,

            room_no,

        )

        return None

    category = await get_category(

        guild,

        config["room_category_id"],

    )

    # @everyoneには非公開

    overwrites = {

        guild.default_role: discord.PermissionOverwrite(

            view_channel=False,

        ),

        guild.me: discord.PermissionOverwrite(

            view_channel=True,

            send_messages=True,

            read_message_history=True,

            manage_channels=True,

        ),

    }

    # 対戦する2チームのメンバーのみ閲覧可能

    for role in {role_a, role_b}:

        overwrites[role] = discord.PermissionOverwrite(

            view_channel=True,

            send_messages=True,

            read_message_history=True,

            attach_files=True,

            embed_links=True,

            use_application_commands=True,

        )

    # サーバー管理権限を持つ運営は閲覧可能

    for member in guild.members:

        if member.guild_permissions.manage_guild:

            overwrites[member] = discord.PermissionOverwrite(

                view_channel=True,

                send_messages=True,

                read_message_history=True,

                manage_channels=True,

            )

    channel_name = (

        f"{date}-{slot}-Room-{room_no}"

    )

    # Discordのチャンネル名に使える文字へ変換

    channel_name = channel_name.lower().replace(" ", "-")

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

    except discord.HTTPException as exc:

        log.exception(

            "対戦部屋の作成に失敗しました: %s",

            exc,

        )

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

    except discord.HTTPException as exc:

        log.exception(

            "対戦部屋の案内メッセージ送信に失敗しました: %s",

            exc,

        )

    return channel

async def run_room_creation(guild, date, slot):

    """

    58分の対戦部屋作成処理。

    作成済みの部屋を重複作成しない。

    """

    job_name = f"rooms-{slot}"

    if job_completed(guild.id, date, job_name):

        return

    rows = db.execute(

        """

        SELECT id, room_no, role_a, role_b, host_role_id, channel_id, status

        FROM scheduled_matches

        WHERE guild_id=? AND date=? AND slot=?

        ORDER BY room_no

        """,

        (guild.id, date, slot),

    ).fetchall()

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

            existing_channel = guild.get_channel(

                existing_channel_id

            )

            if existing_channel is not None:

                continue

        if status == "created":

            # 作成済みと記録されているがチャンネルが存在しない場合は再作成

            pass

        channel = await create_match_room(

            guild,

            date,

            slot,

            room_no,

            role_a,

            role_b,

            host_role_id,

        )

        if channel is not None:

            db_execute(

                """

                UPDATE scheduled_matches

                SET channel_id=?, status='created'

                WHERE id=?

                """,

                (

                    channel.id,

                    match_id,

                ),

            )

    mark_job_completed(

        guild.id,

        date,

        job_name,

    )

    log.info(

        "対戦部屋作成処理完了: guild=%s date=%s slot=%s",

        guild.id,

        date,

        slot,

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

    participant_role_id = config["participant_role_id"]

    participant_role = guild.get_role(

        participant_role_id

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

            "🕛 **12時になりましたので、本日の募集を開始します！**\n"

            "参加可能な時間帯に `/c-uc` で挙手してください。\n"

            "複数の時間帯に挙手することもできます。\n\n"

            "21時枠：20:53締切\n"

            "22時枠：21:53締切\n"

            "23時枠：22:53締切",

            allowed_mentions=discord.AllowedMentions(

                roles=True,

                users=False,

                everyone=False,

            ),

        )

        mark_job_completed(

            guild.id,

            date,

            job_name,

        )

        await refresh_gesture_status(guild)

    except discord.HTTPException:

        log.exception(

            "募集開始通知に失敗しました。guild_id=%s",

            guild.id,

        )

async def clear_old_gestures(guild, date):

    job_name = "daily-clear"

    if job_completed(guild.id, date, job_name):

        return

    # 当日より前の挙手データを削除

    db_execute(

        """

        DELETE FROM gestures

        WHERE guild_id=? AND date<?

        """,

        (

            guild.id,

            date,

        ),

    )

    db_execute(

        """

        DELETE FROM unmatched_teams

        WHERE guild_id=? AND date<?

        """,

        (

            guild.id,

            date,

        ),

    )

    mark_job_completed(

        guild.id,

        date,

        job_name,

    )

    await refresh_gesture_status(guild)

    log.info(

        "前日までの挙手データを削除しました。guild=%s",

        guild.id,

    )

# ============================================================

# 定時処理

# ============================================================

async def scheduler_loop():

    """

    日本時間で定時処理を実行する。

    毎日12:00：募集開始通知

    毎日08:00：前日までの挙手削除

    20:55：21時枠マッチング

    20:58：21時枠の部屋作成

    21:55：22時枠マッチング

    21:58：22時枠の部屋作成

    22:55：23時枠マッチング

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

                # 毎朝8時：古い挙手を削除

                if (

                    current.hour == CLEAR_HOUR

                    and current.minute == 0

                ):

                    await clear_old_gestures(

                        guild,

                        date,

                    )

                # 毎日12時：募集開始

                if (

                    current.hour == OPEN_HOUR

                    and current.minute == 0

                ):

                    await open_daily_signup(

                        guild,

                        date,

                    )

                for slot in TIME_SLOTS:

                    deadline_hour, _ = DEADLINES[slot]

                    # 各時間帯の55分にマッチング

                    if (

                        current.hour == deadline_hour

                        and current.minute == MATCH_MINUTE

                    ):

                        await run_matching(

                            guild,

                            date,

                            slot,

                        )

                    # 各時間帯の58分に部屋を作成

                    if (

                        current.hour == deadline_hour

                        and current.minute == ROOM_MINUTE

                    ):

                        await run_room_creation(

                            guild,

                            date,

                            slot,

                        )

            except Exception:

                log.exception(

                    "定時処理でエラーが発生しました。guild=%s",

                    guild.id,

                )

        await asyncio.sleep(2)

# ============================================================

# Webサーバー

# ============================================================

async def handle_health(request):

    return web.Response(

        text="MKUC 2026 Bot is running!"

    )

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

    log.info(

        "Webサーバーがポート %s で起動しました。",

        PORT,

    )

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

        log.info(

            "スラッシュコマンドを同期しました。"

        )

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