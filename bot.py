import asyncio
import datetime
import os
import logging
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

JST = datetime.timezone(datetime.timedelta(hours=9))

TIME_SLOTS = ["21", "22", "23"]

DEADLINES = {
    "21": (20, 53),
    "22": (21, 53),
    "23": (22, 53),
}

DB_PATH = os.getenv("MKUC_DB_PATH", "mkuc_bot.db")

# 一覧更新時に削除対象とする過去のBotメッセージを調べる件数
HISTORY_LIMIT = 100

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
    """
)
db.commit()


def db_execute(query, params=()):
    """データベースを更新する。"""
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


def get_gesture_counts(guild_id, date):
    """
    時間帯ごとの挙手チーム数を取得する。

    ロールIDやチーム名は表示用データとして返さず、
    各時間帯の件数だけを返す。
    """
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

# 同じサーバーで一覧更新が同時に走ることを防ぐ
refresh_locks = {}


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
            "運営のみ使用できます。",
            ephemeral=True,
        )
        return False

    return True


def get_team_role_from_interaction(interaction):
    """
    実行者が持つ登録済みチームロールを取得する。
    チームロールが0個または複数の場合はエラーにする。
    """
    guild_id = interaction.guild_id

    registered = get_team_role_ids(guild_id)

    roles = [
        role
        for role in interaction.user.roles
        if role.id in registered
    ]

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
        except (discord.NotFound, discord.Forbidden, discord.HTTPException):
            return None

    if isinstance(channel, discord.TextChannel):
        return channel

    return None


# ============================================================
# 挙手一覧の作成・更新
# ============================================================

def create_gesture_embed(guild_id):
    """
    チーム名・ロール名・ロールIDを表示せず、
    時間帯とチーム数だけを表示する。
    """
    date = today_str()
    counts = get_gesture_counts(guild_id, date)
    current = now_jst()

    embed = discord.Embed(
        title="📅 MKUC 本日の挙手状況",
        description=(
            f"**日付：{date}（日本時間）**\n\n"
            "各時間帯の参加可能チーム数です。\n"
            "チーム名・ロール名は表示されません。"
        ),
        color=discord.Color.blue(),
        timestamp=current,
    )

    for slot in TIME_SLOTS:
        deadline_hour, deadline_minute = DEADLINES[slot]

        is_closed = (
            current.hour,
            current.minute,
        ) >= (
            deadline_hour,
            deadline_minute,
        )

        status = "🔒 受付終了" if is_closed else "🟢 受付中"

        embed.add_field(
            name=f"🕒 {slot}時枠",
            value=f"**{counts[slot]}チーム**\n{status}",
            inline=True,
        )

    embed.set_footer(
        text="挙手・取り下げ時に自動更新されます。"
    )

    return embed


async def refresh_gesture_status(guild, current_channel=None):
    """
    挙手一覧を更新する。

    通常時：
    既存メッセージを編集して更新するため、
    一覧が何個も増えることを防ぐ。

    既存メッセージが見つからない場合：
    新しく投稿する。

    前のバージョンで投稿された一覧も可能な範囲で整理する。
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
            "サーバー %s の挙手一覧チャンネルを取得できません。",
            guild.id,
        )
        return

    lock = get_refresh_lock(guild.id)

    async with lock:
        embed = create_gesture_embed(guild.id)

        row = db.execute(
            """
            SELECT channel_id, message_id, date
            FROM status_messages
            WHERE guild_id=?
            """,
            (guild.id,),
        ).fetchone()

        # 前回の一覧を編集して更新する
        if row:
            old_channel_id, old_message_id, old_date = row

            try:
                old_channel = await get_text_channel(
                    guild,
                    old_channel_id,
                )

                if old_channel is not None:
                    old_message = await old_channel.fetch_message(
                        old_message_id
                    )

                    if old_channel.id == channel.id:
                        await old_message.edit(
                            embed=embed,
                            allowed_mentions=discord.AllowedMentions.none(),
                        )

                        db_execute(
                            """
                            UPDATE status_messages
                            SET date=?
                            WHERE guild_id=?
                            """,
                            (today_str(), guild.id),
                        )

                        return

                    # 設定変更でチャンネルが変わった場合
                    try:
                        await old_message.delete()
                    except discord.HTTPException:
                        pass

            except discord.NotFound:
                pass
            except discord.Forbidden:
                log.warning(
                    "既存の一覧メッセージを取得・編集できません。"
                    "チャンネル権限を確認してください。"
                )
            except discord.HTTPException as exc:
                log.warning(
                    "既存の一覧メッセージの更新に失敗しました: %s",
                    exc,
                )

        # 記録済みメッセージがない、または編集できない場合は新規投稿
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
                "Botにチャンネルの閲覧・送信・埋め込みリンク権限が必要です。"
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

    不正な入力や範囲外の時間はエラーにする。
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

        slots = [str(hour) for hour in range(start, end + 1)]

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
        current.hour,
        current.minute,
    ) >= (
        deadline_hour,
        deadline_minute,
    )


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

    msg_parts = []

    if registered:
        msg_parts.append(
            f"✅ 挙手を登録しました：{', '.join(registered)}時"
        )

    if already_registered:
        msg_parts.append(
            f"ℹ️ すでに挙手済みです：{', '.join(already_registered)}時"
        )

    if rejected:
        msg_parts.append(
            f"⚠️ 締切済みのため登録できません：{', '.join(rejected)}時"
        )

    if not msg_parts:
        msg_parts.append("処理対象の時間帯がありませんでした。")

    await interaction.response.send_message(
        "\n".join(msg_parts),
        ephemeral=True,
    )

    # 実際に挙手状況が変わった場合だけ一覧を更新
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
# 運営用コマンド
# ============================================================

@bot.tree.command(
    name="setup-uc",
    description="MKUC Botの設定を行います",
)
@app_commands.describe(
    signup_channel="挙手一覧用チャンネル",
    participant_role="参加者ロール",
    room_category="部屋用カテゴリ（任意）",
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
        f"挙手一覧チャンネル：{signup_channel.mention}\n"
        f"参加者ロール：{participant_role.mention}",
        ephemeral=True,
    )

    # 設定したチャンネルに一覧を作成
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
    name="clear-uc",
    description="本日の挙手をクリアします",
)
async def clear_uc(
    interaction: discord.Interaction,
):
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

    await refresh_gesture_status(
        interaction.guild,
        interaction.channel,
    )

    await interaction.response.send_message(
        "🧹 本日の挙手をすべてクリアしました。",
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


# ============================================================
# 日付変更時の処理
# ============================================================

async def refresh_daily_status():
    """
    日付が変わった後も、前日の一覧が残り続けないようにする。

    毎分動作させ、日付が変わった場合に一覧を更新する。
    """
    await bot.wait_until_ready()

    last_date = today_str()

    while not bot.is_closed():
        await asyncio.sleep(30)

        current_date = today_str()

        if current_date == last_date:
            continue

        last_date = current_date

        for guild in bot.guilds:
            try:
                await refresh_gesture_status(guild)
            except Exception:
                log.exception(
                    "日付変更後の一覧更新に失敗しました。guild_id=%s",
                    guild.id,
                )


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
# Bot起動
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


async def main():
    if not TOKEN:
        raise RuntimeError(
            "DISCORD_TOKEN が設定されていません。"
        )

    web_runner = None
    daily_task = None

    try:
        # Discordに接続
        await bot.login(TOKEN)

        # スラッシュコマンドを同期
        await bot.tree.sync()

        log.info("スラッシュコマンドを同期しました。")

        # Webサーバー起動
        web_runner = await start_web_server()

        # 日付変更監視タスク
        daily_task = asyncio.create_task(
            refresh_daily_status()
        )

        # Discordに接続し続ける
        await bot.connect()

    finally:
        if daily_task is not None:
            daily_task.cancel()

            try:
                await daily_task
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