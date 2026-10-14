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

# 使用する時間帯
TIME_SLOTS = ["21", "22", "23"]

# 試合開始時刻の7分前に締切
DEADLINES = {
    "21": datetime.time(20, 53, tzinfo=JST),
    "22": datetime.time(21, 53, tzinfo=JST),
    "23": datetime.time(22, 53, tzinfo=JST),
}

# マッチング時刻
MATCH_TIMES = {
    "21": datetime.time(20, 55, tzinfo=JST),
    "22": datetime.time(21, 55, tzinfo=JST),
    "23": datetime.time(22, 55, tzinfo=JST),
}

# 部屋作成時刻
ROOM_TIMES = {
    "21": datetime.time(20, 58, tzinfo=JST),
    "22": datetime.time(21, 58, tzinfo=JST),
    "23": datetime.time(22, 58, tzinfo=JST),
}

# 運営設定
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
        "SELECT role_id FROM team_roles WHERE guild_id=?",
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
        "DELETE FROM team_roles WHERE guild_id=? AND role_id=?",
        (guild_id, role_id),
    )


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


# ============================================================
# 共通処理
# ============================================================

def is_admin(interaction: discord.Interaction):
    return (
        interaction.guild is not None
        and interaction.user.guild_permissions.manage_guild
    )


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

    return [
        role
        for role in interaction.user.roles
        if role.id in registered
    ]


def get_team_role_from_interaction(interaction):
    roles = get_member_team_roles(interaction)

    if len(roles) == 0:
        return None, "あなたにチームロールが設定されていません。"

    if len(roles) > 1:
        return None, (
            "複数のチームロールを持っています。"
            "運営にチームロールの設定を確認してもらってください。"
        )

    return roles[0], None


async def get_text_channel(guild, channel_id):
    if not channel_id:
        return None

    channel = guild.get_channel(channel_id)

    if channel is None:
        try:
            channel = await guild.fetch_channel(channel_id)
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
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

    channel = await get_text_channel(
        guild,
        config["signup_channel_id"],
    )

    if channel is None:
        log.warning("挙手チャンネルが見つかりません: %s", guild.id)
        return

    # 前回の一覧メッセージを削除
    # 直近のメッセージのみを調べる
    try:
        async for msg in channel.history(limit=50):
            if msg.author != bot.user:
                continue

            if not msg.embeds:
                continue

            embed = msg.embeds[0]

            if embed.title == "MKUC 本日の挙手状況":
                try:
                    await msg.delete()
                except discord.HTTPException:
                    pass

    except discord.HTTPException:
        log.exception("挙手一覧の削除に失敗しました")

    today = today_str()

    embed = discord.Embed(
        title="MKUC 本日の挙手状況",
        description=(
            "チームごとの挙手状況です。\n"
            "個人名は表示されません。"
        ),
        color=discord.Color.blue(),
        timestamp=now_jst(),
    )

    for slot in TIME_SLOTS:
        rows = db.execute(
            """
            SELECT role_id
            FROM gestures
            WHERE guild_id=? AND date=? AND slot=?
            ORDER BY role_id
            """,
            (guild.id, today, slot),
        ).fetchall()

        role_ids = [row[0] for row in rows]

        # 締切時刻を過ぎた時間帯には締切済みと表示
        current_time = now_jst().time()

        if current_time >= DEADLINES[slot]:
            status = "受付終了"
        else:
            status = "受付中"

        if role_ids:
            team_lines = []

            for role_id in role_ids:
                role = guild.get_role(role_id)

                if role is not None:
                    team_lines.append(role.mention)

            team_text = "\n".join(team_lines) if team_lines else "なし"
        else:
            team_text = "なし"

        embed.add_field(
            name=f"{slot}時枠（{status}）・{len(role_ids)}チーム",
            value=team_text,
            inline=False,
        )

    embed.set_footer(text="MKUC 2026 | 挙手状況")

    try:
        await channel.send(
            embed=embed,
            allowed_mentions=discord.AllowedMentions.none(),
        )
    except discord.HTTPException:
        log.exception("挙手一覧の投稿に失敗しました")


# ============================================================
# チームの挙手
# ============================================================

def parse_times(times):
    times = times.strip().replace(" ", "")

    if not times:
        raise ValueError("時間を指定してください。")

    if "-" in times:
        parts = times.split("-")

        if len(parts) != 2:
            raise ValueError("時間の指定形式が正しくありません。")

        start, end = map(int, parts)

        if start > end:
            raise ValueError("時間の範囲が正しくありません。")

        slots = [str(h) for h in range(start, end + 1)]

    else:
        slots = [part for part in times.split(",") if part]

    if not slots:
        raise ValueError("時間を指定してください。")

    if any(slot not in TIME_SLOTS for slot in slots):
        raise ValueError(
            "指定できる時間は21、22、23です。"
            "例: `21,22`、`21-23`、`23`"
        )

    # 重複を除去
    return list(dict.fromkeys(slots))


@bot.tree.command(
    name="c-uc",
    description="参加可能な時間帯に挙手します",
)
@app_commands.describe(
    times="例: 21,22 または 21-23 または 23"
)
async def c_uc(
    interaction: discord.Interaction,
    times: str,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "このコマンドはサーバー内で使用してください。",
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

    today = today_str()
    current = now_jst().time().replace(tzinfo=None)

    registered = []
    rejected = []

    for slot in slots:
        # その時間帯だけを判定する
        # 21時の締切後でも22時・23時には挙手できる
        if current >= DEADLINES[slot]:
            rejected.append(slot)
            continue

        db_execute(
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

        registered.append(slot)

    if registered:
        await refresh_gesture_status(interaction.guild)

    messages = []

    if registered:
        messages.append(
            f"✅ {team_role.name} の挙手を登録しました。\n"
            f"時間帯: {', '.join(registered)}時"
        )

    if rejected:
        messages.append(
            f"⚠️ 以下の時間帯は締切済みです: "
            f"{', '.join(rejected)}時"
        )

    if not messages:
        messages.append("挙手内容に変更はありません。")

    await interaction.response.send_message(
        "\n\n".join(messages),
        ephemeral=True,
    )


# ============================================================
# 挙手取り下げ
# ============================================================

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

    team_role, error = get_team_role_from_interaction(interaction)

    if error:
        await interaction.response.send_message(
            error,
            ephemeral=True,
        )
        return

    slot = time_slot.value

    # 締切後は取り下げ不可
    if now_jst().time() >= DEADLINES[slot]:
        await interaction.response.send_message(
            "この時間帯はすでに締切済みです。",
            ephemeral=True,
        )
        return

    db_execute(
        """
        DELETE FROM gestures
        WHERE guild_id=? AND date=? AND slot=? AND role_id=?
        """,
        (
            interaction.guild.id,
            today_str(),
            slot,
            team_role.id,
        ),
    )

    await refresh_gesture_status(interaction.guild)

    await interaction.response.send_message(
        f"🗑️ {team_role.name} の {slot}時枠の挙手を取り下げました。",
        ephemeral=True,
    )


# ============================================================
# 運営設定
# ============================================================

@bot.tree.command(
    name="setup-uc",
    description="MKUC Botの使用チャンネルなどを設定します",
)
@app_commands.describe(
    signup_channel="挙手一覧を投稿するテキストチャンネル",
    participant_role="募集開始時にメンションする参加者ロール",
    room_category="対戦部屋を作成するカテゴリ",
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
        "✅ MKUC Botの設定を保存しました。\n"
        f"挙手チャンネル: {signup_channel.mention}\n"
        f"参加者ロール: {participant_role.mention}\n"
        f"対戦部屋カテゴリ: "
        f"{room_category.name if room_category else '未指定'}",
        ephemeral=True,
    )


@bot.tree.command(
    name="teamrole-uc",
    description="チームロールを登録・解除します",
)
@app_commands.describe(
    action="登録または解除",
    role="対象のチームロール",
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
        register_team_role(interaction.guild.id, role.id)

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


# ============================================================
# 得点管理
# ============================================================

@bot.tree.command(
    name="score-uc",
    description="2チームのポイントを加算します",
)
@app_commands.describe(
    team1="1チーム目のロール",
    points1="1チーム目の加算ポイント",
    team2="2チーム目のロール",
    points2="2チーム目の加算ポイント",
)
async def score_uc(
    interaction: discord.Interaction,
    team1: discord.Role,
    points1: int,
    team2: discord.Role,
    points2: int,
):
    if not await require_admin(interaction):
        return

    registered = get_team_role_ids(interaction.guild.id)

    if team1.id not in registered or team2.id not in registered:
        await interaction.response.send_message(
            "指定したロールのいずれかがチームロールとして登録されていません。",
            ephemeral=True,
        )
        return

    if points1 < 0 or points2 < 0:
        await interaction.response.send_message(
            "加算ポイントは0以上を指定してください。",
            ephemeral=True,
        )
        return

    total1 = add_points(
        interaction.guild.id,
        team1.id,
        points1,
    )

    total2 = add_points(
        interaction.guild.id,
        team2.id,
        points2,
    )

    await interaction.response.send_message(
        f"📊 スコアを加算しました。\n\n"
        f"**{team1.name}**: +{points1}pt → 累計 **{total1}pt**\n"
        f"**{team2.name}**: +{points2}pt → 累計 **{total2}pt**",
        ephemeral=True,
    )


@bot.tree.command(
    name="scorechange-uc",
    description="チームの累計ポイントを直接変更します",
)
@app_commands.describe(
    team="対象のチームロール",
    points="変更後の累計ポイント",
)
async def scorechange_uc(
    interaction: discord.Interaction,
    team: discord.Role,
    points: int,
):
    if not await require_admin(interaction):
        return

    if team.id not in get_team_role_ids(interaction.guild.id):
        await interaction.response.send_message(
            "このロールはチームロールとして登録されていません。",
            ephemeral=True,
        )
        return

    if points < 0:
        await interaction.response.send_message(
            "ポイントは0以上を指定してください。",
            ephemeral=True,
        )
        return

    set_points(
        interaction.guild.id,
        team.id,
        points,
    )

    await interaction.response.send_message(
        f"✏️ {team.name} の累計ポイントを **{points}pt** に変更しました。",
        ephemeral=True,
    )


@bot.tree.command(
    name="toggle-prioritize-uc",
    description="2000点超えチーム優先お休み機能を切り替えます",
)
@app_commands.describe(
    enabled="機能を有効にするかどうか",
)
async def toggle_prioritize_uc(
    interaction: discord.Interaction,
    enabled: bool,
):
    if not await require_admin(interaction):
        return

    set_setting(
        interaction.guild.id,
        "prioritize_2000",
        "true" if enabled else "false",
    )

    await interaction.response.send_message(
        "⚙️ 2000点超えチーム優先お休み機能を "
        f"**{'ON' if enabled else 'OFF'}** に設定しました。",
        ephemeral=True,
    )


# ============================================================
# 挙手の手動クリア
# ============================================================

@bot.tree.command(
    name="clear-uc",
    description="本日の挙手をすべて削除します",
)
async def clear_uc(interaction: discord.Interaction):
    if not await require_admin(interaction):
        return

    db_execute(
        """
        DELETE FROM gestures
        WHERE guild_id=? AND date=?
        """,
        (interaction.guild.id, today_str()),
    )

    await refresh_gesture_status(interaction.guild)

    await interaction.response.send_message(
        "🧹 本日の挙手をすべて削除しました。",
        ephemeral=True,
    )


# ============================================================
# マッチング
# ============================================================

def has_played_recently(guild_id, team1_id, team2_id, days=2):
    cutoff = (
        now_jst().date()
        - datetime.timedelta(days=days)
    ).isoformat()

    a, b = sorted([team1_id, team2_id])

    row = db.execute(
        """
        SELECT COUNT(*)
        FROM match_history
        WHERE guild_id=?
          AND date>=?
          AND (
            (team1_id=? AND team2_id=?)
            OR
            (team1_id=? AND team2_id=?)
          )
        """,
        (
            guild_id,
            cutoff,
            a, b,
            b, a,
        ),
    ).fetchone()

    return row[0] > 0


def choose_resting_team(guild_id, teams):
    """
    奇数の場合、休みになるチームを選ぶ。
    2000点超え優先機能がONなら、該当チームから選ぶ。
    """

    prioritize = (
        get_setting(guild_id, "prioritize_2000", "true")
        == "true"
    )

    if prioritize:
        over_2000 = [
            team_id
            for team_id in teams
            if get_points(guild_id, team_id) > 2000
        ]

        if over_2000:
            return random.choice(over_2000)

    return random.choice(teams)


def make_pairs(guild_id, teams):
    """
    同じチームとの連続対戦を避けるようにペアを作る。

    完全最適化ではなく、ランダム性を残しつつ
    最近対戦したチームとの組み合わせを避ける方式。
    """

    remaining = list(teams)
    random.shuffle(remaining)

    pairs = []

    while len(remaining) >= 2:
        team1 = remaining.pop(0)

        candidates = list(remaining)
        random.shuffle(candidates)

        # 最近対戦していないチームを優先
        opponents = [
            team
            for team in candidates
            if not has_played_recently(
                guild_id,
                team1,
                team,
            )
        ]

        if opponents:
            team2 = opponents[0]
        else:
            team2 = candidates[0]

        remaining.remove(team2)

        pairs.append((team1, team2))

    return pairs


async def execute_matching(guild, slot, date_str):
    """
    マッチングをDBに保存する。
    保存後に再実行されても二重作成しない。
    """

    existing = db.execute(
        """
        SELECT matched
        FROM daily_status
        WHERE guild_id=? AND date=? AND slot=?
        """,
        (guild.id, date_str, slot),
    ).fetchone()

    if existing and existing[0] == 1:
        return

    teams = [
        row[0]
        for row in db.execute(
            """
            SELECT role_id
            FROM gestures
            WHERE guild_id=? AND date=? AND slot=?
            ORDER BY role_id
            """,
            (guild.id, date_str, slot),
        ).fetchall()
    ]

    # まず処理済み状態を保存
    db_execute(
        """
        INSERT INTO daily_status (
            guild_id, date, slot, matched, rooms_created
        )
        VALUES (?, ?, ?, 1, 0)
        ON CONFLICT(guild_id, date, slot)
        DO UPDATE SET matched=1
        """,
        (guild.id, date_str, slot),
    )

    config = get_config(guild.id)

    if not config:
        log.error("サーバー設定がありません: %s", guild.id)
        return

    channel = await get_text_channel(
        guild,
        config["signup_channel_id"],
    )

    if channel is None:
        return

    # チームが0の場合
    if len(teams) == 0:
        await channel.send(
            f"⚠️ {slot}時枠: 挙手チームがありませんでした。"
        )
        return

    resting = None

    # 奇数なら1チームを休みにする
    if len(teams) % 2 == 1:
        resting = choose_resting_team(guild.id, teams)
        teams.remove(resting)

    pairs = make_pairs(guild.id, teams)

    # マッチング結果を保存
    for idx, (team1, team2) in enumerate(pairs, start=1):
        host = random.choice([team1, team2])

        db_execute(
            """
            INSERT OR IGNORE INTO matches (
                guild_id,
                date,
                slot,
                room_number,
                team1_id,
                team2_id,
                host_id,
                channel_id
            )
            VALUES (?, ?, ?, ?, ?, ?, ?, NULL)
            """,
            (
                guild.id,
                date_str,
                slot,
                idx,
                team1,
                team2,
                host,
            ),
        )

        a, b = sorted([team1, team2])

        db_execute(
            """
            INSERT INTO match_history (
                guild_id,
                date,
                slot,
                team1_id,
                team2_id
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                guild.id,
                date_str,
                slot,
                a,
                b,
            ),
        )

    lines = [
        f"🔒 **{slot}時枠のマッチング結果**",
    ]

    if resting is not None:
        role = guild.get_role(resting)
        name = role.mention if role else f"不明なロール ({resting})"

        lines.append(
            f"💤 今回のお休み: {name}"
        )

    if not pairs:
        lines.append("対戦カードはありません。")

    for idx, (team1, team2) in enumerate(pairs, start=1):
        r1 = guild.get_role(team1)
        r2 = guild.get_role(team2)

        n1 = r1.mention if r1 else str(team1)
        n2 = r2.mention if r2 else str(team2)

        lines.append(
            f"Room {idx}: {n1} vs {n2}"
        )

    await channel.send(
        "\n".join(lines),
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ============================================================
# 対戦部屋作成
# ============================================================

async def create_match_rooms(guild, slot, date_str):
    status = db.execute(
        """
        SELECT matched, rooms_created
        FROM daily_status
        WHERE guild_id=? AND date=? AND slot=?
        """,
        (guild.id, date_str, slot),
    ).fetchone()

    # マッチングが完了していなければ部屋は作らない
    if not status or status[0] != 1:
        return

    # すでに部屋作成済み
    if status[1] == 1:
        return

    config = get_config(guild.id)

    if not config:
        return

    category = None

    if config["room_category_id"]:
        category = guild.get_channel(
            config["room_category_id"]
        )

    matches = db.execute(
        """
        SELECT id, room_number, team1_id, team2_id, host_id, channel_id
        FROM matches
        WHERE guild_id=? AND date=? AND slot=?
        ORDER BY room_number
        """,
        (guild.id, date_str, slot),
    ).fetchall()

    if not matches:
        db_execute(
            """
            UPDATE daily_status
            SET rooms_created=1
            WHERE guild_id=? AND date=? AND slot=?
            """,
            (guild.id, date_str, slot),
        )
        return

    team_role_ids = get_team_role_ids(guild.id)

    # @everyone以外のロールからも、明示的に閲覧権限を外す
    overwrites = {
        guild.default_role: discord.PermissionOverwrite(
            view_channel=False,
        )
    }

    for role in guild.roles:
        if role.is_default():
            continue

        if role.id in team_role_ids:
            continue

        overwrites[role] = discord.PermissionOverwrite(
            view_channel=False,
        )

    # Bot本人には閲覧・投稿権限を付与
    overwrites[guild.me] = discord.PermissionOverwrite(
        view_channel=True,
        send_messages=True,
        read_message_history=True,
        manage_channels=True,
    )

    failed = False

    for match_id, room_number, team1_id, team2_id, host_id, saved_channel_id in matches:
        # Bot再起動前に作成済みなら、そのチャンネルを再利用
        if saved_channel_id:
            existing_channel = guild.get_channel(saved_channel_id)

            if existing_channel is not None:
                continue

        team1_role = guild.get_role(team1_id)
        team2_role = guild.get_role(team2_id)
        host_role = guild.get_role(host_id)

        if not team1_role or not team2_role or not host_role:
            log.error("チームロールが見つかりません")
            failed = True
            continue

        # 対戦する2チームのみ閲覧・投稿可能
        room_overwrites = dict(overwrites)

        room_overwrites[team1_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )

        room_overwrites[team2_role] = discord.PermissionOverwrite(
            view_channel=True,
            send_messages=True,
            read_message_history=True,
        )

        date_tag = date_str.replace("-", "")
        room_name = f"{date_tag}-{slot}-room{room_number}"

        try:
            room = await guild.create_text_channel(
                room_name,
                category=category,
                overwrites=room_overwrites,
                reason="MKUC 2026 自動マッチング",
            )

            db_execute(
                """
                UPDATE matches
                SET channel_id=?
                WHERE id=?
                """,
                (room.id, match_id),
            )

            await room.send(
                f"{team1_role.mention} {team2_role.mention}\n\n"
                f"🏁 **MKUC 2026 対戦部屋**\n"
                f"時間帯: **{slot}時**\n"
                f"Room: **{room_number}**\n\n"
                f"🏠 **Host:** {host_role.mention}\n\n"
                "両チームで連絡を取り、交流戦を開始してください。",
                allowed_mentions=discord.AllowedMentions(
                    roles=True,
                ),
            )

        except discord.HTTPException:
            log.exception("対戦部屋の作成に失敗しました")
            failed = True

    if not failed:
        db_execute(
            """
            UPDATE daily_status
            SET rooms_created=1
            WHERE guild_id=? AND date=? AND slot=?
            """,
            (guild.id, date_str, slot),
        )


# ============================================================
# 自動募集
# ============================================================

async def send_daily_announcement(guild):
    config = get_config(guild.id)

    if not config:
        return

    channel = await get_text_channel(
        guild,
        config["signup_channel_id"],
    )

    if channel is None:
        return

    role_id = config["participant_role_id"]
    participant_role = guild.get_role(role_id)

    if participant_role is None:
        await channel.send(
            "⚠️ 参加者ロールが見つかりません。"
            "運営は `/setup-uc` で設定を確認してください。"
        )
        return

    await channel.send(
        f"{participant_role.mention}\n\n"
        "📢 **12時になりましたので、本日の募集を開始します！**\n\n"
        "参加できる時間帯に、以下のコマンドで挙手してください。\n"
        "`/c-uc times:21,22`\n"
        "`/c-uc times:21-23`\n"
        "`/c-uc times:23`\n\n"
        "挙手を取り下げる場合は `/d-uc` を使用してください。",
        allowed_mentions=discord.AllowedMentions(
            roles=True,
        ),
    )


# ============================================================
# 定期処理
# ============================================================

@tasks.loop(seconds=10)
async def scheduler():
    current = now_jst()
    date_str = current.strftime("%Y-%m-%d")
    current_time = current.time()

    for guild in bot.guilds:
        config = get_config(guild.id)

        if not config:
            continue

        # 12時の募集開始
        if current.hour == 12 and current.minute == 0:
            key = f"announcement_{date_str}"

            if get_setting(guild.id, key, "false") != "true":
                await send_daily_announcement(guild)
                set_setting(guild.id, key, "true")

        # 翌朝8時の挙手クリア
        if current.hour == 8 and current.minute == 0:
            key = f"clear_{date_str}"

            if get_setting(guild.id, key, "false") != "true":
                yesterday = (
                    current.date() - datetime.timedelta(days=1)
                ).isoformat()

                db_execute(
                    """
                    DELETE FROM gestures
                    WHERE guild_id=? AND date=?
                    """,
                    (guild.id, yesterday),
                )

                set_setting(guild.id, key, "true")

                await refresh_gesture_status(guild)

        # 時間帯ごとの締切・マッチング・部屋作成
        for slot in TIME_SLOTS:
            deadline = DEADLINES[slot]
            match_time = MATCH_TIMES[slot]
            room_time = ROOM_TIMES[slot]

            # 締切後に挙手一覧を更新
            if (
                current.hour == deadline.hour
                and current.minute == deadline.minute
                and current.second < 10
            ):
                key = f"deadline_{date_str}_{slot}"

                if get_setting(guild.id, key, "false") != "true":
                    await refresh_gesture_status(guild)
                    set_setting(guild.id, key, "true")

            # マッチング
            if current_time >= match_time:
                await execute_matching(
                    guild,
                    slot,
                    date_str,
                )

            # 部屋作成
            if current_time >= room_time:
                await create_match_rooms(
                    guild,
                    slot,
                    date_str,
                )


@scheduler.before_loop
async def before_scheduler():
    await bot.wait_until_ready()


# ============================================================
# ヘルスチェック用Webサーバー
# ============================================================

async def handle_health(request):
    return web.Response(
        text="MKUC 2026 Bot is running!",
    )


async def start_web_server():
    app = web.Application()

    app.add_routes([
        web.get("/", handle_health),
        web.get("/health", handle_health),
    ])

    runner = web.AppRunner(app)
    await runner.setup()

    site = web.TCPSite(
        runner,
        "0.0.0.0",
        PORT,
    )

    await site.start()

    log.info("Web server started on port %s", PORT)


# ============================================================
# 起動
# ============================================================

@bot.event
async def on_ready():
    log.info(
        "Logged in as %s (%s)",
        bot.user,
        bot.user.id,
    )

    if not scheduler.is_running():
        scheduler.start()


@bot.event
async def setup_hook():
    pass


async def main():
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN が設定されていません。"
        )

    await bot.login(TOKEN)

    # コマンドを同期
    await bot.tree.sync()

    # WebサーバーとBotを起動
    await start_web_server()

    try:
        await bot.connect()
    finally:
        await bot.close()


if __name__ == "__main__":
    asyncio.run(main())