import os
import io
import json
import asyncio
import discord
import pdfplumber
from datetime import datetime, timezone
from discord.ext import commands
from aiohttp import web
from google.auth.transport.requests import Request
from google.oauth2.credentials import Credentials
from google_auth_oauthlib.flow import Flow
from googleapiclient.discovery import build
import logging
from pythonjsonlogger import jsonlogger

# SQLAlchemy
from sqlalchemy import (
    create_engine, MetaData, Table, Column,
    String, Integer, Text, DateTime,
    insert, select, update, delete
)
from sqlalchemy.dialects.postgresql import insert as pg_insert  # used only when needed
from sqlalchemy.engine import Engine

# ─────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────

LOG_LEVEL = os.getenv("LOG_LEVEL", "INFO").upper()

root_logger = logging.getLogger()
root_logger.setLevel(LOG_LEVEL)

if root_logger.hasHandlers():
    root_logger.handlers.clear()

log_handler = logging.StreamHandler()
formatter = jsonlogger.JsonFormatter(
    fmt='%(asctime)s %(levelname)s %(name)s %(message)s',
    datefmt='%Y-%m-%dT%H:%M:%SZ'
)
log_handler.setFormatter(formatter)
root_logger.addHandler(log_handler)

# Reduce noise from chatty libraries
logging.getLogger('discord').setLevel(logging.WARNING)
logging.getLogger('googleapiclient.discovery').setLevel(logging.WARNING)
logging.getLogger('sqlalchemy.engine').setLevel(logging.WARNING)

logger = logging.getLogger(__name__)
logger.info("Structured JSON logging initialized")


# ─────────────────────────────────────────
#  CONFIG
# ─────────────────────────────────────────

CREDENTIALS_PATH = 'credentials.json'
SCOPES = [
    'https://www.googleapis.com/auth/calendar',
    'https://www.googleapis.com/auth/calendar.events',
]

# Google sometimes returns scopes in a different order or with additions.
# This tells oauthlib to accept the token instead of raising on scope changes.
os.environ.setdefault("OAUTHLIB_RELAX_TOKEN_SCOPE", "1")
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "")
PUBLIC_BASE_URL = os.getenv("PUBLIC_BASE_URL", "https://grafik.tintindo.xyz")
CALLBACK_PATH = "/oauth/callback"
CALLBACK_URL = PUBLIC_BASE_URL + CALLBACK_PATH
OAUTH_PORT = int(os.getenv("OAUTH_PORT", "8080"))
DATABASE_URL = os.getenv("DATABASE_URL", "sqlite:///tokens.db")

pending_auth = {}

intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)
tree = bot.tree


# ─────────────────────────────────────────
#  DATABASE
# ─────────────────────────────────────────

engine = create_engine(
    DATABASE_URL,
    future=True,
    pool_pre_ping=True,
    pool_recycle=1800,
)

metadata = MetaData()

user_tokens_table = Table(
    "user_tokens", metadata,
    Column("discord_user_id", String(64), primary_key=True),
    Column("token_json",      Text,       nullable=False),
    Column("google_email",    String(256)),
    Column("updated_at",      DateTime,   default=datetime.now),
)

user_settings_table = Table(
    "user_settings", metadata,
    Column("discord_user_id",  String(64),  primary_key=True),
    Column("reminder_minutes", Integer,     default=30),
    Column("event_color",      String(4),   default="7"),
    Column("calendar_id",      String(256), default="primary"),
    Column("location",         String(512), default="PP Wrocław Świdnicka"),
)


def db_init():
    metadata.create_all(engine)
    logger.info("Database tables initialized", extra={"database_url": DATABASE_URL})


def _upsert(table: Table, values: dict, key_col: str):
    dialect = engine.dialect.name
    key_val = values[key_col]

    with engine.begin() as conn:
        if dialect == "sqlite":
            stmt = table.insert().prefix_with("OR REPLACE").values(**values)
            conn.execute(stmt)
        elif dialect == "postgresql":
            from sqlalchemy.dialects.postgresql import insert as pg_insert
            stmt = pg_insert(table).values(**values)
            update_cols = {k: v for k, v in values.items() if k != key_col}
            stmt = stmt.on_conflict_do_update(index_elements=[key_col], set_=update_cols)
            conn.execute(stmt)
        elif dialect in ("mysql", "mariadb"):
            from sqlalchemy.dialects.mysql import insert as my_insert
            stmt = my_insert(table).values(**values)
            update_cols = {k: v for k, v in values.items() if k != key_col}
            stmt = stmt.on_duplicate_key_update(**update_cols)
            conn.execute(stmt)
        else:
            rows = conn.execute(
                update(table)
                .where(table.c[key_col] == key_val)
                .values(**{k: v for k, v in values.items() if k != key_col})
            ).rowcount
            if rows == 0:
                conn.execute(insert(table).values(**values))


def db_save_token(discord_user_id: int, creds: Credentials, google_email: str = None):
    _upsert(user_tokens_table, {
        "discord_user_id": str(discord_user_id),
        "token_json":      creds.to_json(),
        "google_email":    google_email,
        "updated_at":      datetime.now(timezone.utc),
    }, key_col="discord_user_id")
    logger.debug(
        "Token saved",
        extra={"discord_user_id": str(discord_user_id), "google_email": google_email}
    )


def db_load_token(discord_user_id: int) -> Credentials | None:
    with engine.connect() as conn:
        row = conn.execute(
            select(user_tokens_table.c.token_json)
            .where(user_tokens_table.c.discord_user_id == str(discord_user_id))
        ).fetchone()
    if row:
        return Credentials.from_authorized_user_info(json.loads(row.token_json), SCOPES)
    return None


def db_delete_token(discord_user_id: int):
    with engine.begin() as conn:
        conn.execute(
            delete(user_tokens_table)
            .where(user_tokens_table.c.discord_user_id == str(discord_user_id))
        )
    logger.debug("Token deleted", extra={"discord_user_id": str(discord_user_id)})


def db_list_users() -> list[tuple]:
    with engine.connect() as conn:
        rows = conn.execute(
            select(
                user_tokens_table.c.discord_user_id,
                user_tokens_table.c.google_email,
                user_tokens_table.c.updated_at,
            ).order_by(user_tokens_table.c.updated_at.desc())
        ).fetchall()
    return [(r.discord_user_id, r.google_email, r.updated_at) for r in rows]


def db_get_settings(discord_user_id: int) -> dict:
    with engine.connect() as conn:
        row = conn.execute(
            select(
                user_settings_table.c.reminder_minutes,
                user_settings_table.c.event_color,
                user_settings_table.c.calendar_id,
                user_settings_table.c.location,
            ).where(user_settings_table.c.discord_user_id == str(discord_user_id))
        ).fetchone()
    if row:
        return {
            "reminder_minutes": row.reminder_minutes,
            "event_color":      row.event_color,
            "calendar_id":      row.calendar_id,
            "location":         row.location,
        }
    return {
        "reminder_minutes": 30,
        "event_color":      "7",
        "calendar_id":      "primary",
        "location":         "PP Wrocław Świdnicka",
    }


def db_save_settings(discord_user_id: int, **kwargs):
    settings = db_get_settings(discord_user_id)
    settings.update(kwargs)
    _upsert(user_settings_table, {
        "discord_user_id":  str(discord_user_id),
        "reminder_minutes": settings["reminder_minutes"],
        "event_color":      settings["event_color"],
        "calendar_id":      settings["calendar_id"],
        "location":         settings["location"],
    }, key_col="discord_user_id")


COLOR_NAMES = {
    '1': '🔵 Lawenda',   '2': '💚 Szałwia',    '3': '🫐 Winogrono',
    '4': '🩷 Flamingo',  '5': '🍌 Banan',       '6': '🍊 Mandarynka',
    '7': '🩵 Pawi',      '8': '🫐 Borówka',     '9': '🫐 Jagoda',
    '10': '🌿 Bazylia',  '11': '🍅 Pomidor',
}


# ─────────────────────────────────────────
#  GOOGLE CALENDAR
# ─────────────────────────────────────────

def get_calendar_service(discord_user_id: int):
    creds = db_load_token(discord_user_id)
    if not creds:
        logger.debug("No token found for user", extra={"discord_user_id": str(discord_user_id)})
        return None
    if not creds.valid:
        if creds.expired and creds.refresh_token:
            try:
                creds.refresh(Request())
                db_save_token(discord_user_id, creds)
                logger.info("Token refreshed", extra={"discord_user_id": str(discord_user_id)})
            except Exception:
                logger.exception(
                    "Token refresh failed",
                    extra={"discord_user_id": str(discord_user_id)}
                )
                return None
        else:
            logger.warning(
                "Token invalid and not refreshable",
                extra={"discord_user_id": str(discord_user_id)}
            )
            return None
    return build('calendar', 'v3', credentials=creds)


# ─────────────────────────────────────────
#  BOT EVENTS & COMMANDS
# ─────────────────────────────────────────

@bot.event
async def on_ready():
    db_init()
    try:
        synced = await tree.sync()
        logger.info(
            "Bot ready",
            extra={
                "bot_user": str(bot.user),
                "oauth_callback_url": CALLBACK_URL,
                "database_url": DATABASE_URL,
                "slash_commands_synced": len(synced),
            }
        )
    except Exception:
        logger.exception("Failed to sync slash commands")


@bot.command()
async def login(ctx):
    if not os.path.exists(CREDENTIALS_PATH):
        await ctx.send("❌ Brak pliku `credentials.json`!")
        return
    flow = Flow.from_client_secrets_file(CREDENTIALS_PATH, scopes=SCOPES, redirect_uri=CALLBACK_URL)
    auth_url, state = flow.authorization_url(access_type='offline', prompt='consent', include_granted_scopes='true')
    pending_auth[state] = {'discord_user_id': ctx.author.id, 'channel_id': ctx.channel.id, 'flow': flow}
    embed = discord.Embed(title="🔐 Autoryzacja Google Calendar", color=discord.Color.blue())
    embed.description = (
        f"**1.** [KLIKNIJ TUTAJ, ABY SIĘ ZALOGOWAĆ]({auth_url})\n\n"
        "**2.** Zaloguj się swoim kontem Google\n\n"
        "**3.** Zaakceptuj uprawnienia\n\n"
        "**4.** Bot automatycznie otrzyma dostęp — nie musisz nic więcej robić! ✅"
    )
    embed.set_footer(text=f"Link dla: {ctx.author.display_name} • wygasa po 10 minutach")
    try:
        await ctx.author.send(embed=embed)
        await ctx.send(f"📨 {ctx.author.mention} Wysłałem Ci link autoryzacyjny w wiadomości prywatnej!")
        logger.info(
            "OAuth flow initiated",
            extra={"discord_user_id": str(ctx.author.id), "source": "prefix_command"}
        )
    except discord.Forbidden:
        await ctx.send(embed=embed)
    await asyncio.sleep(600)
    if state in pending_auth:
        del pending_auth[state]
        logger.info(
            "OAuth state expired",
            extra={"discord_user_id": str(ctx.author.id), "source": "prefix_command"}
        )


@bot.command()
async def status(ctx):
    service = get_calendar_service(ctx.author.id)
    if service:
        embed = discord.Embed(title="✅ Połączono z Google Calendar", color=discord.Color.green())
        embed.add_field(name="Użytkownik Discord", value=ctx.author.mention)
        await ctx.send(embed=embed)
    else:
        await ctx.send(f"❌ {ctx.author.mention} Nie jesteś zalogowany. Wpisz `/login`.")


@bot.command()
async def logout(ctx):
    if db_load_token(ctx.author.id):
        db_delete_token(ctx.author.id)
        await ctx.send(f"✅ {ctx.author.mention} Wylogowano. Twój token został usunięty.")
        logger.info(
            "User logged out",
            extra={"discord_user_id": str(ctx.author.id), "source": "prefix_command"}
        )
    else:
        await ctx.send(f"ℹ️ {ctx.author.mention} Nie byłeś zalogowany.")


@bot.command()
@commands.has_permissions(administrator=True)
async def users(ctx):
    rows = db_list_users()
    if not rows:
        await ctx.send("ℹ️ Brak zalogowanych użytkowników.")
        return
    embed = discord.Embed(title="👥 Zalogowani użytkownicy", color=discord.Color.gold())
    for discord_user_id, google_email, updated_at in rows:
        user = bot.get_user(int(discord_user_id))
        name = user.display_name if user else f"ID: {discord_user_id}"
        embed.add_field(name=name, value=f"📧 {google_email or 'nieznany'}\n🕐 {updated_at}", inline=False)
    await ctx.send(embed=embed)


@bot.command()
@commands.has_permissions(administrator=True)
async def removeuser(ctx, user: discord.Member):
    if db_load_token(user.id):
        db_delete_token(user.id)
        logger.warning(
            "User token deleted by admin",
            extra={
                "admin_discord_id": str(ctx.author.id),
                "target_discord_id": str(user.id),
                "action": "admin_delete_user",
            }
        )
        await ctx.send(f"✅ Usunięto token użytkownika {user.mention}.")
    else:
        await ctx.send(f"ℹ️ {user.mention} nie miał zapisanego tokenu.")


@bot.command()
async def settings(ctx):
    if isinstance(ctx.channel, discord.DMChannel):
        await send_settings_dm(ctx.author)
        return
    try:
        await send_settings_dm(ctx.author)
        await ctx.send(f"📨 {ctx.author.mention} Wysłałem Ci panel ustawień w wiadomości prywatnej!")
    except discord.Forbidden:
        await ctx.send(f"❌ {ctx.author.mention} Nie mogę wysłać Ci DM. Odblokuj wiadomości prywatne.")


async def send_settings_dm(user: discord.User):
    s = db_get_settings(user.id)
    color_name = COLOR_NAMES.get(s['event_color'], s['event_color'])
    reminder_text = f"{s['reminder_minutes']} minut wcześniej" if s['reminder_minutes'] > 0 else "Wyłączone"
    embed = discord.Embed(
        title="⚙️ Twoje ustawienia GrafikBot",
        description="Użyj komend poniżej aby zmienić ustawienia. Działają tutaj w DM!",
        color=discord.Color.blurple()
    )
    embed.add_field(name="⏰ Przypomnienie",  value=reminder_text,    inline=True)
    embed.add_field(name="🎨 Kolor wydarzeń", value=color_name,       inline=True)
    embed.add_field(name="📍 Lokalizacja",    value=s['location'],    inline=False)
    embed.add_field(name="📅 Kalendarz",      value=s['calendar_id'], inline=False)
    colors_preview = "  ".join([f"`{k}` {v}" for k, v in COLOR_NAMES.items()])
    embed.add_field(
        name="📖 Dostępne komendy",
        value=(
            "`/login` — Logowanie przez Google\n"
            "`/logout` — Wylogowanie się\n"
            "`/settings` — Panel ustawień\n"
            "`/status` — Status połączenia\n"
            "`/setcalendar` — Wybór kalendarza\n"
            "`/setreminder <minuty>` — np. `/setreminder 15` lub `/setreminder 0` (wyłącz) \n"
            "`/setcolor <id>` — np. `/setcolor 4`\n"
            "`/setlocation <miejsce>` — np. `/setlocation PP Wrocław Świdnicka`\n\n"
            f"**Kolory:** {colors_preview}"
        ),
        inline=False
    )
    embed.set_footer(text="Wszystkie komendy działają zarówno tutaj w DM jak i na serwerze")
    await user.send(embed=embed)


@bot.command()
async def setreminder(ctx, minutes: int):
    if minutes < 0 or minutes > 1440:
        await ctx.author.send("❌ Podaj liczbę minut od 0 do 1440 (24h).")
        return
    db_save_settings(ctx.author.id, reminder_minutes=minutes)
    msg = "🔕 Przypomnienia **wyłączone**." if minutes == 0 else f"✅ Przypomnienie ustawione na **{minutes} minut** przed wydarzeniem."
    await ctx.author.send(msg)
    await send_settings_dm(ctx.author)
    logger.info(
        "User updated reminder setting",
        extra={"discord_user_id": str(ctx.author.id), "reminder_minutes": minutes, "source": "prefix_command"}
    )
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.message.add_reaction("✅")


@bot.command()
async def setcolor(ctx, color_id: str):
    if color_id not in COLOR_NAMES:
        colors_list = "\n".join([f"`{k}` — {v}" for k, v in COLOR_NAMES.items()])
        await ctx.author.send(f"❌ Nieprawidłowy kolor. Dostępne:\n{colors_list}")
        return
    db_save_settings(ctx.author.id, event_color=color_id)
    logger.info(
        "User updated color setting",
        extra={"discord_user_id": str(ctx.author.id), "event_color": color_id, "source": "prefix_command"}
    )
    await ctx.author.send(f"✅ Kolor wydarzeń ustawiony na {COLOR_NAMES[color_id]}.")
    await send_settings_dm(ctx.author)
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.message.add_reaction("✅")


@bot.command()
async def setlocation(ctx, *, location: str):
    db_save_settings(ctx.author.id, location=location)
    logger.info(
        "User updated location setting",
        extra={"discord_user_id": str(ctx.author.id), "location": location, "source": "prefix_command"}
    )
    await ctx.author.send(f"✅ Lokalizacja ustawiona na: **{location}**")
    await send_settings_dm(ctx.author)
    if not isinstance(ctx.channel, discord.DMChannel):
        await ctx.message.add_reaction("✅")


# ─────────────────────────────────────────
#  SLASH COMMANDS
# ─────────────────────────────────────────

@tree.command(name="login", description="🔐 Zaloguj się przez Google i połącz swój kalendarz z botem")
async def slash_login(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    if not os.path.exists(CREDENTIALS_PATH):
        await interaction.followup.send("❌ Brak pliku `credentials.json`!", ephemeral=True)
        return
    flow = Flow.from_client_secrets_file(CREDENTIALS_PATH, scopes=SCOPES, redirect_uri=CALLBACK_URL)
    auth_url, state = flow.authorization_url(access_type='offline', prompt='consent', include_granted_scopes='true')
    pending_auth[state] = {'discord_user_id': interaction.user.id, 'channel_id': interaction.channel_id, 'flow': flow}
    embed = discord.Embed(title="🔐 Autoryzacja Google Calendar", color=discord.Color.blue())
    embed.description = (
        f"**1.** [KLIKNIJ TUTAJ, ABY SIĘ ZALOGOWAĆ]({auth_url})\n\n"
        "**2.** Zaloguj się swoim kontem Google\n\n"
        "**3.** Zaakceptuj uprawnienia\n\n"
        "**4.** Bot automatycznie otrzyma dostęp ✅"
    )
    embed.set_footer(text=f"Link dla: {interaction.user.display_name} • wygasa po 10 minutach")
    try:
        await interaction.user.send(embed=embed)
        await interaction.followup.send("📨 Wysłałem Ci link autoryzacyjny w wiadomości prywatnej!", ephemeral=True)
        logger.info(
            "OAuth flow initiated",
            extra={"discord_user_id": str(interaction.user.id), "source": "slash_command"}
        )
    except discord.Forbidden:
        await interaction.followup.send(embed=embed, ephemeral=True)
    asyncio.create_task(_expire_state(state, 600))


async def _expire_state(state: str, delay: int):
    await asyncio.sleep(delay)
    if state in pending_auth:
        user_id = pending_auth[state].get("discord_user_id")
        pending_auth.pop(state, None)
        logger.info(
            "OAuth state expired",
            extra={"discord_user_id": str(user_id), "source": "slash_command"}
        )


@tree.command(name="status", description="📋 Sprawdź status połączenia z Google Calendar")
async def slash_status(interaction: discord.Interaction):
    service = get_calendar_service(interaction.user.id)
    if service:
        embed = discord.Embed(title="✅ Połączono z Google Calendar", color=discord.Color.green())
        embed.add_field(name="Użytkownik", value=interaction.user.mention)
        await interaction.response.send_message(embed=embed, ephemeral=True)
    else:
        await interaction.response.send_message("❌ Nie jesteś zalogowany. Użyj `/login`.", ephemeral=True)


@tree.command(name="logout", description="🚪 Wyloguj się i usuń połączenie z Google Calendar")
async def slash_logout(interaction: discord.Interaction):
    if db_load_token(interaction.user.id):
        db_delete_token(interaction.user.id)
        logger.info(
            "User logged out",
            extra={"discord_user_id": str(interaction.user.id), "source": "slash_command"}
        )
        await interaction.response.send_message("✅ Wylogowano. Twój token został usunięty.", ephemeral=True)
    else:
        await interaction.response.send_message("ℹ️ Nie byłeś zalogowany.", ephemeral=True)


@tree.command(name="settings", description="⚙️ Otwórz panel ustawień w wiadomości prywatnej (DM)")
async def slash_settings(interaction: discord.Interaction):
    try:
        await send_settings_dm(interaction.user)
        await interaction.response.send_message("📨 Wysłałem Ci panel ustawień w wiadomości prywatnej!", ephemeral=True)
    except discord.Forbidden:
        await interaction.response.send_message("❌ Nie mogę wysłać Ci DM. Odblokuj wiadomości prywatne.", ephemeral=True)


@tree.command(name="setreminder", description="⏰ Ustaw ile minut przed zmianą chcesz dostać przypomnienie")
@discord.app_commands.describe(minuty="Liczba minut przed wydarzeniem (0 = wyłącz)")
async def slash_setreminder(interaction: discord.Interaction, minuty: int):
    if minuty < 0 or minuty > 1440:
        await interaction.response.send_message("❌ Podaj liczbę minut od 0 do 1440.", ephemeral=True)
        return
    db_save_settings(interaction.user.id, reminder_minutes=minuty)
    logger.info(
        "User updated reminder setting",
        extra={"discord_user_id": str(interaction.user.id), "reminder_minutes": minuty, "source": "slash_command"}
    )
    msg = "🔕 Przypomnienia **wyłączone**." if minuty == 0 else f"✅ Przypomnienie ustawione na **{minuty} minut** przed wydarzeniem."
    await interaction.response.send_message(msg, ephemeral=True)
    try:
        await send_settings_dm(interaction.user)
    except discord.Forbidden:
        pass


@tree.command(name="setcolor", description="🎨 Wybierz kolor dla swoich wydarzeń w Google Calendar")
@discord.app_commands.describe(kolor="ID koloru (1-11)")
@discord.app_commands.choices(kolor=[
    discord.app_commands.Choice(name="🔵 Lawenda",    value="1"),
    discord.app_commands.Choice(name="💚 Szałwia",    value="2"),
    discord.app_commands.Choice(name="🫐 Winogrono",  value="3"),
    discord.app_commands.Choice(name="🩷 Flamingo",   value="4"),
    discord.app_commands.Choice(name="🍌 Banan",      value="5"),
    discord.app_commands.Choice(name="🍊 Mandarynka", value="6"),
    discord.app_commands.Choice(name="🩵 Pawi",       value="7"),
    discord.app_commands.Choice(name="🫐 Borówka",    value="8"),
    discord.app_commands.Choice(name="🫐 Jagoda",     value="9"),
    discord.app_commands.Choice(name="🌿 Bazylia",    value="10"),
    discord.app_commands.Choice(name="🍅 Pomidor",    value="11"),
])
async def slash_setcolor(interaction: discord.Interaction, kolor: str):
    db_save_settings(interaction.user.id, event_color=kolor)
    logger.info(
        "User updated color setting",
        extra={"discord_user_id": str(interaction.user.id), "event_color": kolor, "source": "slash_command"}
    )
    await interaction.response.send_message(f"✅ Kolor ustawiony na {COLOR_NAMES[kolor]}.", ephemeral=True)
    try:
        await send_settings_dm(interaction.user)
    except discord.Forbidden:
        pass


@tree.command(name="setlocation", description="📍 Ustaw adres miejsca pracy (pojawi się w wydarzeniu)")
@discord.app_commands.describe(lokalizacja="Adres lub nazwa miejsca pracy")
async def slash_setlocation(interaction: discord.Interaction, lokalizacja: str):
    db_save_settings(interaction.user.id, location=lokalizacja)
    logger.info(
        "User updated location setting",
        extra={"discord_user_id": str(interaction.user.id), "location": lokalizacja, "source": "slash_command"}
    )
    await interaction.response.send_message(f"✅ Lokalizacja ustawiona na: **{lokalizacja}**", ephemeral=True)
    try:
        await send_settings_dm(interaction.user)
    except discord.Forbidden:
        pass


@tree.command(name="users", description="👥 [ADMIN] Pokaż listę wszystkich zalogowanych użytkowników")
@discord.app_commands.default_permissions(administrator=True)
async def slash_users(interaction: discord.Interaction):
    rows = db_list_users()
    if not rows:
        await interaction.response.send_message("ℹ️ Brak zalogowanych użytkowników.", ephemeral=True)
        return
    embed = discord.Embed(title="👥 Zalogowani użytkownicy", color=discord.Color.gold())
    for discord_user_id, google_email, updated_at in rows:
        user = bot.get_user(int(discord_user_id))
        name = user.display_name if user else f"ID: {discord_user_id}"
        embed.add_field(name=name, value=f"📧 {google_email or 'nieznany'}\n🕐 {updated_at}", inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@tree.command(name="removeuser", description="🗑️ [ADMIN] Wyloguj wybranego użytkownika z Google Calendar")
@discord.app_commands.describe(uzytkownik="Użytkownik do wylogowania")
@discord.app_commands.default_permissions(administrator=True)
async def slash_removeuser(interaction: discord.Interaction, uzytkownik: discord.Member):
    if db_load_token(uzytkownik.id):
        db_delete_token(uzytkownik.id)
        logger.warning(
            "User token deleted by admin",
            extra={
                "admin_discord_id": str(interaction.user.id),
                "target_discord_id": str(uzytkownik.id),
                "action": "admin_delete_user",
                "source": "slash_command",
            }
        )
        await interaction.response.send_message(f"✅ Usunięto token użytkownika {uzytkownik.mention}.", ephemeral=True)
    else:
        await interaction.response.send_message(f"ℹ️ {uzytkownik.mention} nie miał zapisanego tokenu.", ephemeral=True)


@tree.command(name="setcalendar", description="📅 Wybierz do którego kalendarza Google dodawać wydarzenia")
async def slash_setcalendar(interaction: discord.Interaction):
    await interaction.response.defer(ephemeral=True)
    service = get_calendar_service(interaction.user.id)
    if not service:
        await interaction.followup.send("❌ Nie jesteś zalogowany. Użyj `/login` najpierw.", ephemeral=True)
        return
    try:
        calendar_list = service.calendarList().list().execute()
        calendars = calendar_list.get('items', [])
    except Exception:
        logger.exception(
            "Failed to fetch calendar list",
            extra={"discord_user_id": str(interaction.user.id)}
        )
        await interaction.followup.send("❌ Błąd pobierania kalendarzy.", ephemeral=True)
        return
    if not calendars:
        await interaction.followup.send("❌ Nie znaleziono żadnych kalendarzy.", ephemeral=True)
        return
    s = db_get_settings(interaction.user.id)
    current_id = s['calendar_id']
    options = []
    for cal in calendars[:25]:
        cal_id = cal['id']
        cal_name = cal.get('summary', cal_id)
        is_current = cal_id == current_id
        options.append(discord.SelectOption(
            label=cal_name[:100],
            value=cal_id[:100],
            description="✅ Aktualnie wybrany" if is_current else cal.get('description', '')[:100] or None,
            default=is_current,
            emoji="📅" if is_current else None
        ))

    class CalendarSelect(discord.ui.Select):
        def __init__(self):
            super().__init__(placeholder="Wybierz kalendarz...", options=options, min_values=1, max_values=1)

        async def callback(self, select_interaction: discord.Interaction):
            chosen_id = self.values[0]
            chosen_name = next((c.get('summary', chosen_id) for c in calendars if c['id'] == chosen_id), chosen_id)
            db_save_settings(interaction.user.id, calendar_id=chosen_id)
            logger.info(
                "User updated calendar setting",
                extra={
                    "discord_user_id": str(interaction.user.id),
                    "calendar_id": chosen_id,
                    "source": "slash_command",
                }
            )
            await select_interaction.response.send_message(f"✅ Kalendarz ustawiony na: **{chosen_name}**", ephemeral=True)
            try:
                await send_settings_dm(interaction.user)
            except discord.Forbidden:
                pass

    class CalendarView(discord.ui.View):
        def __init__(self):
            super().__init__(timeout=60)
            self.add_item(CalendarSelect())

    embed = discord.Embed(
        title="📅 Wybierz kalendarz",
        description="Wybierz do którego kalendarza Google bot ma dodawać Twoje zmiany:",
        color=discord.Color.blue()
    )
    embed.set_footer(text=f"Aktualnie: {current_id}")
    await interaction.followup.send(embed=embed, view=CalendarView(), ephemeral=True)


@bot.command()
async def setcalendar(ctx):
    await ctx.send("Użyj komendy slash `/setcalendar` aby wybrać kalendarz z listy!")


@bot.event
async def on_message(message):
    if message.author == bot.user:
        return
    if message.attachments:
        for attachment in message.attachments:
            if attachment.filename.lower().endswith('.pdf'):
                logger.info(
                    "PDF received",
                    extra={
                        "discord_user_id": str(message.author.id),
                        "pdf_filename": attachment.filename,
                        "file_size_bytes": attachment.size,
                        "channel_id": str(message.channel.id),
                    }
                )
                service = get_calendar_service(message.author.id)
                if not service:
                    await message.channel.send(
                        f"⚠️ {message.author.mention} Nie jesteś zalogowany do Google Calendar. "
                        f"Wpisz `!login` aby się połączyć."
                    )
                    return
                status_msg = await message.channel.send(
                    f"⏳ {message.author.mention} Przetwarzam grafik i dodaję do Twojego kalendarza..."
                )
                try:
                    pdf_data = await attachment.read()
                    wynik = procesuj_pdf_i_kalendarz(pdf_data, service, message.author.id)
                    await status_msg.edit(content=f"{message.author.mention}\n{wynik}")
                except Exception:
                    logger.exception(
                        "PDF processing failed",
                        extra={
                            "discord_user_id": str(message.author.id),
                            "pdf_filename": attachment.filename,
                        }
                    )
                    await status_msg.edit(content="❌ Wystąpił błąd podczas przetwarzania pliku.")
    await bot.process_commands(message)


# ─────────────────────────────────────────
#  PDF PROCESSING
# ─────────────────────────────────────────

def parse_year_month_from_pdf(pdf) -> str:
    import re
    for page in pdf.pages:
        text = page.extract_text() or ""
        match = re.search(r'\d{2}\.(\d{2})\.(\d{4})', text)
        if match:
            return f"{match.group(2)}-{match.group(1)}"
    logger.warning("Could not parse year/month from PDF, falling back to current month")
    return datetime.now().strftime("%Y-%m")


def make_datetime(date_iso: str, time_str: str, start_str: str) -> str:
    from datetime import timedelta
    d = datetime.strptime(date_iso, "%Y-%m-%d")
    if time_str == "00:00" or time_str < start_str:
        d += timedelta(days=1)
    return f"{d.strftime('%Y-%m-%d')}T{time_str}:00"


def procesuj_pdf_i_kalendarz(file_bytes, service, discord_user_id: int):
    import re
    s = db_get_settings(discord_user_id)
    raport = "✅ **Zaktualizowano kalendarz!**\n"
    events_added = 0

    with pdfplumber.open(io.BytesIO(file_bytes)) as pdf:
        rok_miesiac = parse_year_month_from_pdf(pdf)
        logger.info(
            "PDF parsed: detected month",
            extra={"discord_user_id": str(discord_user_id), "rok_miesiac": rok_miesiac}
        )

        tabela = pdf.pages[0].extract_table()
        if not tabela or len(tabela) < 2:
            logger.warning(
                "PDF table not found or too short",
                extra={"discord_user_id": str(discord_user_id)}
            )
            return "❌ Nie znaleziono tabeli w PDF."

        dni_tygodnia = tabela[0]
        dane_wiersz = tabela[1]

        for i in range(len(dni_tygodnia)):
            komorka = dane_wiersz[i]
            if not komorka:
                continue
            linie = [l.strip() for l in komorka.split('\n') if l.strip()]
            if not linie:
                continue
            godziny_linia = next(
                (l for l in linie if re.match(r'^\d{2}:\d{2}-\d{2}:\d{2}$', l)), None
            )
            if not godziny_linia:
                continue
            start_h, end_h = godziny_linia.split('-')
            zadanie = next(
                (l for l in linie if l != godziny_linia
                 and not re.match(r'^\d{1,2}$', l)
                 and 'PP ' not in l),
                "Praca"
            )
            nr_dnia = next(
                (l.zfill(2) for l in reversed(linie) if re.match(r'^\d{1,2}$', l)), None
            )
            if not nr_dnia:
                continue

            data_iso = f"{rok_miesiac}-{nr_dnia}"
            miesiac = rok_miesiac.split('-')[1]
            start_dt = make_datetime(data_iso, start_h, start_h)
            end_dt   = make_datetime(data_iso, end_h,   start_h)

            event = {
                'summary':  f"Praca: {zadanie}",
                'location': s['location'],
                'colorId':  s['event_color'],
                'start': {'dateTime': start_dt, 'timeZone': 'Europe/Warsaw'},
                'end':   {'dateTime': end_dt,   'timeZone': 'Europe/Warsaw'},
            }
            if s['reminder_minutes'] > 0:
                event['reminders'] = {
                    'useDefault': False,
                    'overrides': [{'method': 'popup', 'minutes': s['reminder_minutes']}]
                }
            else:
                event['reminders'] = {'useDefault': False, 'overrides': []}

            try:
                service.events().insert(calendarId=s['calendar_id'], body=event).execute()
                events_added += 1
                logger.debug(
                    "Calendar event inserted",
                    extra={
                        "discord_user_id": str(discord_user_id),
                        "date": data_iso,
                        "start": start_h,
                        "end": end_h,
                        "task": zadanie,
                    }
                )
            except Exception:
                logger.exception(
                    "Failed to insert calendar event",
                    extra={
                        "discord_user_id": str(discord_user_id),
                        "date": data_iso,
                        "start": start_h,
                        "end": end_h,
                    }
                )

            raport += f"• {nr_dnia}.{miesiac} ({dni_tygodnia[i]}) — {start_h}-{end_h}\n"

    logger.info(
        "PDF processing complete",
        extra={
            "discord_user_id": str(discord_user_id),
            "rok_miesiac": rok_miesiac,
            "events_added": events_added,
        }
    )
    return raport


# ─────────────────────────────────────────
#  OAUTH CALLBACK SERVER
# ─────────────────────────────────────────

async def handle_callback(request: web.Request) -> web.Response:
    state = request.query.get('state')
    code  = request.query.get('code')
    error = request.query.get('error')

    logger.info(
        "OAuth callback received",
        extra={
            "state_known": state in pending_auth if state else False,
            "has_code": bool(code),
            "error": error,
            "remote": str(request.remote),
        }
    )

    if error:
        if state and state in pending_auth:
            discord_user_id = pending_auth[state]['discord_user_id']
            channel = bot.get_channel(pending_auth[state]['channel_id'])
            if channel:
                await channel.send(f"❌ Autoryzacja odrzucona: `{error}`")
            del pending_auth[state]
            logger.warning(
                "OAuth denied by user",
                extra={"discord_user_id": str(discord_user_id), "error": error}
            )
        return web.Response(
            text="<h2>❌ Autoryzacja odrzucona.</h2><p>Możesz zamknąć tę kartę.</p>",
            content_type='text/html'
        )

    if not state or state not in pending_auth:
        logger.warning(
            "OAuth callback with unknown or expired state",
            extra={"state": state, "remote": str(request.remote)}
        )
        return web.Response(
            text="<h2>❌ Nieznana sesja OAuth.</h2><p>Spróbuj ponownie wpisać !login na Discordzie.</p>",
            content_type='text/html', status=400
        )

    session = pending_auth.pop(state)
    flow: Flow = session['flow']
    channel_id: int = session['channel_id']
    discord_user_id: int = session['discord_user_id']

    try:
        flow.fetch_token(code=code)
        creds = flow.credentials

        google_email = None
        try:
            from googleapiclient.discovery import build as gbuild
            oauth2 = gbuild('oauth2', 'v2', credentials=creds)
            google_email = oauth2.userinfo().get().execute().get('email')
        except Exception:
            logger.warning(
                "Could not fetch Google email after OAuth",
                extra={"discord_user_id": str(discord_user_id)}
            )

        db_save_token(discord_user_id, creds, google_email)
        logger.info(
            "OAuth completed successfully",
            extra={"discord_user_id": str(discord_user_id), "google_email": google_email}
        )

        channel = bot.get_channel(channel_id)
        if channel:
            user = bot.get_user(discord_user_id)
            mention = user.mention if user else f"<@{discord_user_id}>"
            email_info = f" ({google_email})" if google_email else ""
            await channel.send(
                f"✅ {mention} **Autoryzacja zakończona sukcesem!**{email_info}\n"
                f"Bot ma teraz dostęp do Twojego Google Calendar."
            )

        return web.Response(text="""
            <html>
            <head><style>
                body { font-family: sans-serif; display:flex; align-items:center;
                       justify-content:center; height:100vh; background:#1a1a2e; color:white; margin:0; }
                .box { text-align:center; padding:48px 40px; background:#16213e;
                       border-radius:16px; box-shadow: 0 8px 32px rgba(0,0,0,0.4); }
                h2 { color:#4ade80; margin-bottom:8px; }
                p  { color:#94a3b8; }
            </style></head>
            <body>
                <div class="box">
                    <h2>✅ Autoryzacja zakończona!</h2>
                    <p>Bot Discord ma teraz dostęp do Twojego Google Calendar.</p>
                    <p>Możesz zamknąć tę kartę.</p>
                </div>
            </body>
            </html>
        """, content_type='text/html')

    except Exception:
        logger.exception(
            "OAuth token exchange failed",
            extra={"discord_user_id": str(discord_user_id)}
        )
        channel = bot.get_channel(channel_id)
        if channel:
            await channel.send("❌ Błąd podczas wymiany tokenu. Spróbuj ponownie wpisać `/login`.")
        return web.Response(
            text="<h2>❌ Błąd autoryzacji.</h2><p>Wróć na Discord i spróbuj ponownie.</p>",
            content_type='text/html', status=500
        )


async def start_web_server():
    app = web.Application()
    app.router.add_get(CALLBACK_PATH, handle_callback)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, '0.0.0.0', OAUTH_PORT)
    await site.start()
    logger.info("Web server started", extra={"port": OAUTH_PORT})


# ─────────────────────────────────────────
#  MAIN
# ─────────────────────────────────────────

async def main():
    async with bot:
        await start_web_server()
        await bot.start(DISCORD_TOKEN)


if __name__ == '__main__':
    asyncio.run(main())