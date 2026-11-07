import asyncio
import datetime
import logging
import os
import re
import sqlite3

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
HISTORY_LIMIT = 20

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("MKUC")


# ============================================================
# データベース
#
# guild_config と team_roles は旧Botと同じ構造を維持しています。
# 既存のBotデータベースをそのまま引き継げます。
# ============================================================

db = sqlite3.connect(DB_PATH, check_same_thread=False)
db.execute("PRAGMA journal_mode=WAL")
db.execute("PRAGMA busy_timeout=5000")
db.executescript(
    """
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

    CREATE TABLE IF NOT EXISTS tournament_room_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        guild_id INTEGER NOT NULL,
        created_by INTEGER NOT NULL,
        created_at TEXT NOT NULL,
        stage TEXT NOT NULL,
        opening_time TEXT NOT NULL,
        room_no INTEGER NOT NULL,
        role_a_id INTEGER NOT NULL,
        role_b_id INTEGER NOT NULL,
        channel_id INTEGER NOT NULL
    );
    CREATE INDEX IF NOT EXISTS idx_tournament_room_history
        ON tournament_room_history(guild_id, id DESC);
    """
)

# 旧DBに staff_role_id がまだない場合に備えて列を追加します。
config_columns = {
    row[1] for row in db.execute("PRAGMA table_info(guild_config)").fetchall()
}
if "staff_role_id" not in config_columns:
    db.execute("ALTER TABLE guild_config ADD COLUMN staff_role_id INTEGER")
    db.commit()


def db_execute(query, params=()):
    cursor = db.execute(query, params)
    db.commit()
    return cursor


def get_config(guild_id):
    row = db.execute(
        """
        SELECT signup_channel_id, participant_role_id,
               room_category_id, staff_role_id
        FROM guild_config WHERE guild_id=?
        """,
        (guild_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "signup_channel_id": row[0],
        "participant_role_id": row[1],
        "room_category_id": row[2],
        "staff_role_id": row[3],
    }


def set_config(guild_id, signup_channel_id, participant_role_id,
               room_category_id, staff_role_id):
    db_execute(
        """
        INSERT INTO guild_config (
            guild_id, signup_channel_id, participant_role_id,
            room_category_id, staff_role_id
        ) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(guild_id) DO UPDATE SET
            signup_channel_id=excluded.signup_channel_id,
            participant_role_id=excluded.participant_role_id,
            room_category_id=excluded.room_category_id,
            staff_role_id=excluded.staff_role_id
        """,
        (guild_id, signup_channel_id, participant_role_id,
         room_category_id, staff_role_id),
    )


def get_team_role_ids(guild_id):
    return {
        row[0] for row in db.execute(
            "SELECT role_id FROM team_roles WHERE guild_id=?", (guild_id,)
        ).fetchall()
    }


def now_jst():
    return datetime.datetime.now(JST)


# ============================================================
# Bot / 運営権限
# ============================================================

intents = discord.Intents.default()
intents.guilds = True
bot = commands.Bot(command_prefix="!", intents=intents)


def is_admin(interaction):
    if interaction.guild is None:
        return False
    if interaction.user.guild_permissions.manage_guild:
        return True
    config = get_config(interaction.guild.id)
    staff_role_id = config.get("staff_role_id") if config else None
    return bool(
        staff_role_id
        and any(role.id == staff_role_id
                for role in getattr(interaction.user, "roles", []))
    )


async def require_admin(interaction):
    if not is_admin(interaction):
        await interaction.response.send_message(
            "運営のみ使用できます。運営ロールまたはサーバー管理権限が必要です。",
            ephemeral=True,
        )
        return False
    return True


# ============================================================
# 既存の運営設定・チームロール登録
# ============================================================

@bot.tree.command(name="setup-uc", description="MKUC Botの設定を行います")
@app_commands.describe(
    signup_channel="旧予選用の設定欄（現在の部屋作成では使用しません）",
    participant_role="旧予選用の設定欄（現在の部屋作成では使用しません）",
    room_category="対戦部屋を作成するカテゴリ",
    staff_role="対戦部屋を閲覧できる運営ロール",
)
async def setup_uc(
    interaction: discord.Interaction,
    signup_channel: discord.TextChannel,
    participant_role: discord.Role,
    room_category: discord.CategoryChannel = None,
    staff_role: discord.Role = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。", ephemeral=True
        )
        return
    if not await require_admin(interaction):
        return
    if staff_role is not None and interaction.guild.get_role(staff_role.id) is None:
        await interaction.response.send_message(
            "運営ロールを取得できませんでした。", ephemeral=True
        )
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
        f"対戦部屋カテゴリ：{room_category.mention if room_category else '未設定'}\n"
        f"運営ロール：{staff_role.mention if staff_role else '未設定'}",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="teamrole-uc", description="チームロールを一括登録・解除します"
)
@app_commands.describe(action="登録または解除", roles="対象のチームロール。複数指定できます")
@app_commands.choices(action=[
    app_commands.Choice(name="登録", value="add"),
    app_commands.Choice(name="解除", value="remove"),
])
async def teamrole_uc(
    interaction: discord.Interaction,
    action: app_commands.Choice[str],
    roles: str,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。", ephemeral=True
        )
        return
    if not await require_admin(interaction):
        return

    role_ids = list(dict.fromkeys(map(int, re.findall(r"<@&(\d+)>", roles))))
    if not role_ids:
        await interaction.response.send_message(
            "チームロールを1つ以上指定してください。例：`@チームA @チームB`",
            ephemeral=True,
        )
        return

    changed, unchanged, invalid = [], [], []
    for role_id in role_ids:
        role = interaction.guild.get_role(role_id)
        if role is None:
            invalid.append(f"削除済みロール（ID: {role_id}）")
            continue
        exists = role_id in get_team_role_ids(interaction.guild.id)
        if action.value == "add" and not exists:
            db_execute(
                "INSERT OR IGNORE INTO team_roles (guild_id, role_id) VALUES (?, ?)",
                (interaction.guild.id, role_id),
            )
            changed.append(f"登録：{role.name}")
        elif action.value == "remove" and exists:
            db_execute(
                "DELETE FROM team_roles WHERE guild_id=? AND role_id=?",
                (interaction.guild.id, role_id),
            )
            changed.append(f"解除：{role.name}")
        else:
            unchanged.append(f"変更なし：{role.name}")

    lines = changed + unchanged + invalid
    await interaction.response.send_message(
        "\n".join(lines) if lines else "処理対象がありませんでした。",
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ============================================================
# トーナメント部屋の入力チェック・作成
# ============================================================

def clean_stage_for_channel(stage):
    name = re.sub(r"[^\w-]+", "-", stage.strip(), flags=re.UNICODE)
    name = re.sub(r"-+", "-", name).strip("-")
    return name[:70] or "match"


def validate_opening_time(value):
    match = re.fullmatch(r"([01]\d|2[0-3]):([0-5]\d)", value.strip())
    return f"{match.group(1)}:{match.group(2)}" if match else None


def make_room_announcement(stage, opening_time, room_no, role_a, role_b):
    return (
        f"{role_a.mention} {role_b.mention}\n"
        f"**MKUC {stage}**\n"
        f"開設時間：{opening_time}\n"
        f"Room：{room_no}\n\n"
        f"**Host：** {role_a.mention}\n\n"
        "150cc / 2チーム戦 / ノーマルアイテム / CPUなし / インターバル 10秒\n"
        "12レースで対戦してください。\n"
        "リザルト提出は、リザルト投稿用のチャンネルで行ってください。"
    )


async def create_tournament_rooms(guild, operator_id, stage, opening_time, roles):
    config = get_config(guild.id)
    if not config or not config.get("room_category_id"):
        raise RuntimeError("`/setup-uc` で対戦部屋カテゴリを設定してください。")
    category = guild.get_channel(config["room_category_id"])
    if not isinstance(category, discord.CategoryChannel):
        raise RuntimeError("設定された部屋カテゴリが見つかりません。`/setup-uc` を確認してください。")

    staff_role_id = config.get("staff_role_id")
    staff_role = guild.get_role(staff_role_id) if staff_role_id else None
    if staff_role_id and staff_role is None:
        raise RuntimeError("設定された運営ロールが見つかりません。`/setup-uc` を確認してください。")
    bot_member = guild.me
    if bot_member is None:
        raise RuntimeError("Botのサーバーメンバー情報を取得できませんでした。")

    try:
        me_permissions = category.permissions_for(bot_member)
        if not me_permissions.manage_channels or not me_permissions.view_channel:
            raise RuntimeError("Botにカテゴリ内のチャンネル管理・閲覧権限がありません。")

        overwrites = {
            guild.default_role: discord.PermissionOverwrite(view_channel=False),
            bot_member: discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, manage_channels=True,
            ),
        }
        if staff_role:
            overwrites[staff_role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, manage_channels=True,
            )
        for role in roles:
            overwrites[role] = discord.PermissionOverwrite(
                view_channel=True, send_messages=True,
                read_message_history=True, attach_files=True,
                embed_links=True, use_application_commands=True,
            )
        created = []
        stage_slug = clean_stage_for_channel(stage)
        try:
            for room_no, (role_a, role_b) in enumerate(zip(roles[::2], roles[1::2]), start=1):
                channel = await guild.create_text_channel(
                    name=f"{stage_slug}-room{room_no}"[:100],
                    category=category,
                    overwrites=overwrites,
                    reason=f"MKUC {stage} Room {room_no} by {operator_id}",
                )
                created.append((channel, room_no, role_a, role_b))
                await channel.send(
                    make_room_announcement(stage, opening_time, room_no, role_a, role_b),
                    allowed_mentions=discord.AllowedMentions(
                        roles=[role_a, role_b], users=False, everyone=False
                    ),
                )

            created_at = now_jst().isoformat(timespec="seconds")
            db.execute("BEGIN")
            for channel, room_no, role_a, role_b in created:
                db.execute(
                    """INSERT INTO tournament_room_history (
                        guild_id, created_by, created_at, stage, opening_time,
                        room_no, role_a_id, role_b_id, channel_id
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                    (guild.id, operator_id, created_at, stage, opening_time,
                     room_no, role_a.id, role_b.id, channel.id),
                )
            db.commit()
            return created
        except Exception:
            db.rollback()
            for channel, _, _, _ in reversed(created):
                try:
                    await channel.delete(reason="部屋作成全体の取消し")
                except discord.HTTPException:
                    log.exception("作成失敗後のチャンネル削除に失敗: %s", channel.id)
            raise
    except discord.Forbidden as exc:
        raise RuntimeError(
            "Discordの権限不足で部屋を作成できませんでした。Botのチャンネル管理権限とカテゴリ権限を確認してください。"
        ) from exc
    except discord.HTTPException as exc:
        raise RuntimeError(
            "Discord APIでエラーが発生しました。作成した部屋は可能な限り取り消しました。"
        ) from exc


class CreateRoomsConfirmView(discord.ui.View):
    def __init__(self, operator_id, guild, stage, opening_time, roles):
        super().__init__(timeout=180)
        self.operator_id = operator_id
        self.guild = guild
        self.stage = stage
        self.opening_time = opening_time
        self.roles = roles
        self.finished = False

    async def interaction_check(self, interaction):
        if interaction.user.id != self.operator_id:
            await interaction.response.send_message(
                "この確認ボタンはコマンドを実行した人だけが操作できます。",
                ephemeral=True,
            )
            return False
        return True

    @discord.ui.button(label="作成する", style=discord.ButtonStyle.success)
    async def confirm(self, interaction, button):
        if self.finished:
            await interaction.response.send_message(
                "この確認はすでに完了しています。", ephemeral=True
            )
            return
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content="部屋を作成しています…", view=None
        )
        try:
            created = await create_tournament_rooms(
                self.guild, self.operator_id, self.stage,
                self.opening_time, self.roles,
            )
        except RuntimeError as exc:
            await interaction.followup.send(f"❌ {exc}", ephemeral=True)
            return
        except Exception:
            log.exception("トーナメント部屋の作成に失敗しました。")
            await interaction.followup.send(
                "❌ 部屋の作成に失敗しました。Botログを確認してください。",
                ephemeral=True,
            )
            return

        summary = "\n".join(
            f"Room {room_no}：{role_a.mention} vs {role_b.mention} → {channel.mention}"
            for channel, room_no, role_a, role_b in created
        )
        await interaction.followup.send(
            f"✅ **MKUC {self.stage}** の部屋を作成しました。\n"
            f"開設時間：{self.opening_time}\n{summary}",
            ephemeral=True,
            allowed_mentions=discord.AllowedMentions.none(),
        )

    @discord.ui.button(label="キャンセル", style=discord.ButtonStyle.secondary)
    async def cancel(self, interaction, button):
        if self.finished:
            await interaction.response.send_message(
                "この確認はすでに完了しています。", ephemeral=True
            )
            return
        self.finished = True
        self.stop()
        await interaction.response.edit_message(
            content="部屋の作成をキャンセルしました。", view=None
        )

    async def on_timeout(self):
        if not self.finished:
            self.stop()


@bot.tree.command(
    name="createroom-uc", description="トーナメントの対戦部屋を作成します"
)
@app_commands.describe(
    stage="ステージ名（例：準決勝、決勝）",
    opening_time="部屋の開設時刻（24時間表記、例：22:10）",
    team1="Room 1の左側・Hostチーム",
    team2="Room 1の対戦チーム",
    team3="Room 2の左側・Hostチーム（任意）",
    team4="Room 2の対戦チーム（任意）",
    team5="Room 3の左側・Hostチーム（任意）",
    team6="Room 3の対戦チーム（任意）",
    team7="Room 4の左側・Hostチーム（任意）",
    team8="Room 4の対戦チーム（任意）",
)
async def createroom_uc(
    interaction: discord.Interaction,
    stage: str,
    opening_time: str,
    team1: discord.Role,
    team2: discord.Role,
    team3: discord.Role = None,
    team4: discord.Role = None,
    team5: discord.Role = None,
    team6: discord.Role = None,
    team7: discord.Role = None,
    team8: discord.Role = None,
):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。", ephemeral=True
        )
        return
    if not await require_admin(interaction):
        return

    stage = stage.strip()
    if not stage or len(stage) > 60 or "@everyone" in stage or "@here" in stage:
        await interaction.response.send_message(
            "ステージ名は1〜60文字で入力してください。", ephemeral=True
        )
        return
    normalized_time = validate_opening_time(opening_time)
    if normalized_time is None:
        await interaction.response.send_message(
            "開設時刻は `HH:MM` 形式で入力してください（例：22:10）。",
            ephemeral=True,
        )
        return

    roles = [team1, team2, team3, team4, team5, team6, team7, team8]
    if any(role is None for role in roles[2:]) and any(role is not None for role in roles[2:]):
        first_missing = next(i for i, role in enumerate(roles[2:], start=2) if role is None)
        if any(role is not None for role in roles[first_missing + 1:]):
            await interaction.response.send_message(
                "チームは team1 から順番に指定してください。途中を空欄にはできません。",
                ephemeral=True,
            )
            return
    roles = [role for role in roles if role is not None]
    if not 2 <= len(roles) <= 8 or len(roles) % 2:
        await interaction.response.send_message(
            "チームは2〜8チームを、2チームずつの偶数で指定してください。",
            ephemeral=True,
        )
        return
    role_ids = [role.id for role in roles]
    if len(set(role_ids)) != len(role_ids):
        await interaction.response.send_message(
            "同じチームロールを重複して指定できません。", ephemeral=True
        )
        return
    registered = get_team_role_ids(interaction.guild.id)
    if any(role_id not in registered for role_id in role_ids):
        await interaction.response.send_message(
            "指定されたチームの中に未登録のロールがあります。先に `/teamrole-uc` で登録してください。",
            ephemeral=True,
        )
        return
    if any(interaction.guild.get_role(role_id) is None for role_id in role_ids):
        await interaction.response.send_message(
            "指定されたチームロールを取得できませんでした。", ephemeral=True
        )
        return

    config = get_config(interaction.guild.id)
    if not config or not config.get("room_category_id"):
        await interaction.response.send_message(
            "先に `/setup-uc` で対戦部屋カテゴリを設定してください。",
            ephemeral=True,
        )
        return

    cards = list(zip(roles[::2], roles[1::2]))
    preview = "\n".join(
        f"Room {room_no}：{left.mention} vs {right.mention}（Host：{left.mention}）"
        for room_no, (left, right) in enumerate(cards, start=1)
    )
    view = CreateRoomsConfirmView(
        interaction.user.id, interaction.guild, stage, normalized_time, roles
    )
    await interaction.response.send_message(
        f"次の内容で対戦部屋を作成しますか？\n"
        f"**MKUC {stage}** / 開設時間：{normalized_time}\n{preview}\n\n"
        "この確認はあなたにだけ表示されています。",
        view=view,
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


@bot.tree.command(
    name="roomhistory-uc", description="トーナメント部屋の作成履歴を表示します"
)
async def roomhistory_uc(interaction: discord.Interaction):
    if interaction.guild is None:
        await interaction.response.send_message(
            "サーバー内で使用してください。", ephemeral=True
        )
        return
    if not await require_admin(interaction):
        return

    rows = db.execute(
        """SELECT created_at, stage, opening_time, room_no,
                  role_a_id, role_b_id, channel_id
           FROM tournament_room_history
           WHERE guild_id=? AND created_by=?
           ORDER BY id DESC LIMIT ?""",
        (interaction.guild.id, interaction.user.id, HISTORY_LIMIT),
    ).fetchall()
    if not rows:
        await interaction.response.send_message(
            "あなたが作成した対戦部屋の履歴はありません。", ephemeral=True
        )
        return

    lines = []
    for created_at, stage, opening_time, room_no, role_a_id, role_b_id, channel_id in rows:
        role_a = interaction.guild.get_role(role_a_id)
        role_b = interaction.guild.get_role(role_b_id)
        channel = interaction.guild.get_channel(channel_id)
        team_a = role_a.mention if role_a else f"削除済みロール ({role_a_id})"
        team_b = role_b.mention if role_b else f"削除済みロール ({role_b_id})"
        room = channel.mention if channel else f"削除済みチャンネル ({channel_id})"
        try:
            timestamp = datetime.datetime.fromisoformat(created_at).strftime("%m/%d %H:%M")
        except ValueError:
            timestamp = created_at
        lines.append(
            f"{timestamp}：**MKUC {stage}** / 開設 {opening_time} / Room {room_no}\n"
            f"{team_a} vs {team_b} → {room}"
        )
    await interaction.response.send_message(
        "**あなたの対戦部屋作成履歴（新しい順・最大20件）**\n\n"
        + "\n\n".join(lines),
        ephemeral=True,
        allowed_mentions=discord.AllowedMentions.none(),
    )


# ============================================================
# ヘルスチェック・Bot起動
# ============================================================

async def handle_health(request):
    return web.Response(text="MKUC Tournament Bot is running!")


async def start_web_server():
    app = web.Application()
    app.add_routes([web.get("/", handle_health), web.get("/health", handle_health)])
    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", PORT).start()
    log.info("Webサーバーがポート %s で起動しました。", PORT)
    return runner


@bot.event
async def on_ready():
    log.info("Logged in as %s (ID: %s)", bot.user, bot.user.id if bot.user else "unknown")


async def main():
    if not TOKEN:
        raise RuntimeError("DISCORD_TOKEN が設定されていません。")
    runner = None
    try:
        await bot.login(TOKEN)
        await bot.tree.sync()
        log.info("スラッシュコマンドを同期しました。")
        runner = await start_web_server()
        await bot.connect()
    finally:
        if runner:
            await runner.cleanup()
        await bot.close()
        db.close()


if __name__ == "__main__":
    asyncio.run(main())
