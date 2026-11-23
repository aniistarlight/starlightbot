import asyncio
import json
import math
import os
import re
import time
from collections import deque
from io import BytesIO
from typing import Optional, Union

import discord
import matplotlib

matplotlib.use("Agg")  # headless backend, must be set before importing pyplot
import matplotlib.dates as mdates  # noqa: E402
import matplotlib.pyplot as plt  # noqa: E402
from datetime import datetime  # noqa: E402

from discord.ext import commands, tasks
from dotenv import load_dotenv
from mctools import RCONClient
from petpetgif import petpet as petpetgif

load_dotenv()

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
WEATHER_ADMIN_ID = 856143540446036008

RCON_HOST = os.getenv("RCON_HOST")
RCON_PORT_RAW = os.getenv("RCON_PORT")
RCON_PASSWORD = os.getenv("RCON_PASSWORD")

SERVER_IP = "starlightcraft.go.altare.global"
SERVER_VERSION = "26.3"
MAX_PLAYERS = 20

# TPS polling / history
TPS_POLL_SECONDS = 10
TPS_HISTORY_HOURS = 24
TPS_HISTORY_FILE = "tps_history.json"
TPS_SAVE_EVERY = 30  # save to disk every N samples (30 * 10s = 5 minutes)


def validate_config() -> int:
    """Validate required environment variables before starting the bot."""
    missing = [
        name
        for name, value in {
            "DISCORD_TOKEN": DISCORD_TOKEN,
            "RCON_HOST": RCON_HOST,
            "RCON_PORT": RCON_PORT_RAW,
            "RCON_PASSWORD": RCON_PASSWORD,
        }.items()
        if not value
    ]

    if missing:
        raise RuntimeError(
            "Missing environment variable(s): "
            + ", ".join(missing)
            + ". Copy .env.example to .env and fill it in."
        )

    try:
        port = int(RCON_PORT_RAW)
    except ValueError as exc:
        raise RuntimeError("RCON_PORT must be a number.") from exc

    if not 1 <= port <= 65535:
        raise RuntimeError("RCON_PORT must be between 1 and 65535.")

    return port


RCON_PORT = validate_config()


# ---------------------------------------------------------------------------
# Discord bot setup
# ---------------------------------------------------------------------------

intents = discord.Intents.default()
intents.message_content = True

bot = commands.Bot(
    command_prefix="!",
    intents=intents,
    help_command=None,
)


# ---------------------------------------------------------------------------
# Minecraft RCON helpers
# ---------------------------------------------------------------------------

def execute_rcon(command: str) -> str:
    """Execute one command on the Minecraft server through RCON."""
    rcon = RCONClient(RCON_HOST, port=RCON_PORT)

    try:
        if not rcon.login(RCON_PASSWORD):
            raise ConnectionError("RCON authentication failed.")

        response = rcon.command(command)
        response = re.sub(r"\x1b\[[0-9;]*m", "", response or "")
        response = re.sub(r"§.", "", response)  # legacy colour codes, just in case
        return response.strip()
    finally:
        rcon.stop()


def get_playerlist() -> str:
    return execute_rcon("list")


# ---------------------------------------------------------------------------
# TPS parsing + history
# ---------------------------------------------------------------------------

_NUM = r"\*?([\d.]+)"  # TabTPS may prefix a "*" when TPS is above 20

TPS_RE = re.compile(
    rf"TPS:\s*{_NUM}\s*\(5s\),\s*{_NUM}\s*\(1m\),\s*{_NUM}\s*\(5m\),\s*{_NUM}\s*\(15m\)"
)
MSPT_RE = re.compile(r"(\d+)s\s*-\s*([\d.]+),\s*([\d.]+),\s*([\d.]+)")
CPU_RE = re.compile(r"CPU:\s*([\d.]+)%,\s*([\d.]+)%")
RAM_RE = re.compile(r"RAM:\s*(\d+)M/(\d+)M\s*\(max\.?\s*(\d+)M\)")


def parse_tps(raw: str) -> dict:
    """Parse the output of TabTPS's /tps command into a dict."""
    tps_match = TPS_RE.search(raw)
    if not tps_match:
        raise ValueError(f"Could not parse TPS output: {raw[:200]!r}")

    data: dict = {
        "tps": {
            "5s": float(tps_match.group(1)),
            "1m": float(tps_match.group(2)),
            "5m": float(tps_match.group(3)),
            "15m": float(tps_match.group(4)),
        },
        "mspt": {},
        "cpu": None,
        "ram": None,
    }

    for m in MSPT_RE.finditer(raw):
        data["mspt"][f"{m.group(1)}s"] = {
            "avg": float(m.group(2)),
            "min": float(m.group(3)),
            "max": float(m.group(4)),
        }

    cpu = CPU_RE.search(raw)
    if cpu:
        data["cpu"] = {"sys": float(cpu.group(1)), "proc": float(cpu.group(2))}

    ram = RAM_RE.search(raw)
    if ram:
        data["ram"] = {
            "used": int(ram.group(1)),
            "total": int(ram.group(2)),
            "max": int(ram.group(3)),
        }

    return data


def fetch_tps() -> dict:
    return parse_tps(execute_rcon("tps"))


# Each sample: (unix_timestamp, tps_5s, tps_1m, mspt_avg_10s, mspt_max_10s)
tps_history: deque = deque(maxlen=TPS_HISTORY_HOURS * 3600 // TPS_POLL_SECONDS)


def load_tps_history() -> None:
    try:
        with open(TPS_HISTORY_FILE, "r", encoding="utf-8") as f:
            samples = json.load(f)
        cutoff = time.time() - TPS_HISTORY_HOURS * 3600
        for s in samples:
            if len(s) == 5 and s[0] >= cutoff:
                tps_history.append(tuple(s))
        print(f"Loaded {len(tps_history)} TPS samples from disk.")
    except FileNotFoundError:
        pass
    except Exception as exc:  # corrupted file shouldn't stop the bot
        print(f"Could not load TPS history: {exc}")


def save_tps_history() -> None:
    try:
        tmp = TPS_HISTORY_FILE + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(list(tps_history), f)
        os.replace(tmp, TPS_HISTORY_FILE)
    except Exception as exc:
        print(f"Could not save TPS history: {exc}")


_samples_since_save = 0


@tasks.loop(seconds=TPS_POLL_SECONDS)
async def poll_tps():
    global _samples_since_save
    try:
        data = await asyncio.to_thread(fetch_tps)
    except Exception as exc:
        # Server offline / RCON hiccup: just skip this sample (shows as a gap).
        print(f"TPS poll failed: {exc}")
        return

    mspt = data["mspt"].get("10s") or data["mspt"].get("5s") or {}
    tps_history.append(
        (
            time.time(),
            data["tps"]["5s"],
            data["tps"]["1m"],
            mspt.get("avg", float("nan")),
            mspt.get("max", float("nan")),
        )
    )

    _samples_since_save += 1
    if _samples_since_save >= TPS_SAVE_EVERY:
        _samples_since_save = 0
        await asyncio.to_thread(save_tps_history)


@poll_tps.before_loop
async def before_poll_tps():
    await bot.wait_until_ready()


# ---------------------------------------------------------------------------
# Embed / graph helpers
# ---------------------------------------------------------------------------

def tps_color(tps: float) -> discord.Color:
    if tps >= 19:
        return discord.Color.green()
    if tps >= 15:
        return discord.Color.orange()
    return discord.Color.red()


def tps_icon(tps: float) -> str:
    return "🟢" if tps >= 19 else "🟠" if tps >= 15 else "🔴"


def text_bar(fraction: float, width: int = 20) -> str:
    fraction = max(0.0, min(1.0, fraction))
    filled = round(fraction * width)
    return "█" * filled + "░" * (width - filled)


def parse_duration(text: str) -> int:
    """Parse '30m', '2h', '1d' (or a bare number of minutes) into seconds."""
    match = re.fullmatch(r"(\d+)\s*([mhd]?)", text.strip().lower())
    if not match:
        raise ValueError("Use a duration like `30m`, `2h` or `1d`.")

    value = int(match.group(1))
    unit = match.group(2) or "m"
    return value * {"m": 60, "h": 3600, "d": 86400}[unit]


def render_tps_graph(samples: list, span_seconds: int) -> BytesIO:
    """Render TPS + MSPT over time into a PNG. Runs in a worker thread."""
    nan = float("nan")
    times, tps5, tps1m, mspt_avg, mspt_max = [], [], [], [], []

    prev_ts = None
    for ts, t5, t1, ma, mx in samples:
        # Insert a NaN break where polling had a gap so the line doesn't lie.
        if prev_ts is not None and ts - prev_ts > TPS_POLL_SECONDS * 3:
            times.append(datetime.fromtimestamp((prev_ts + ts) / 2))
            tps5.append(nan)
            tps1m.append(nan)
            mspt_avg.append(nan)
            mspt_max.append(nan)
        times.append(datetime.fromtimestamp(ts))
        tps5.append(t5)
        tps1m.append(t1)
        mspt_avg.append(ma)
        mspt_max.append(mx)
        prev_ts = ts

    bg, fg, grid = "#2b2d31", "#dbdee1", "#40444b"

    fig, (ax1, ax2) = plt.subplots(
        2, 1, figsize=(10, 6), sharex=True,
        gridspec_kw={"height_ratios": [3, 2]}, facecolor=bg,
    )

    for ax in (ax1, ax2):
        ax.set_facecolor(bg)
        ax.tick_params(colors=fg)
        ax.grid(True, color=grid, linewidth=0.6)
        for spine in ax.spines.values():
            spine.set_color(grid)

    ax1.plot(times, tps5, color="#5865f2", linewidth=1.2, label="TPS (5s)", alpha=0.9)
    ax1.plot(times, tps1m, color="#57f287", linewidth=1.8, label="TPS (1m)")
    ax1.axhline(20, color="#99aab5", linestyle="--", linewidth=0.8)
    ax1.set_ylim(0, 21)
    ax1.set_ylabel("TPS", color=fg)
    ax1.set_title("StarlightCraft - Server Performance", color=fg)
    ax1.legend(facecolor=bg, edgecolor=grid, labelcolor=fg, loc="lower left")

    ax2.plot(times, mspt_avg, color="#fee75c", linewidth=1.4, label="MSPT avg (10s)")
    ax2.plot(times, mspt_max, color="#ed4245", linewidth=0.9, alpha=0.7, label="MSPT max (10s)")
    ax2.axhline(50, color="#99aab5", linestyle="--", linewidth=0.8)  # 50ms = lag threshold
    ax2.set_ylabel("MSPT (ms)", color=fg)
    ax2.set_ylim(bottom=0)
    ax2.legend(facecolor=bg, edgecolor=grid, labelcolor=fg, loc="upper left")

    locator = mdates.AutoDateLocator()
    ax2.xaxis.set_major_locator(locator)
    ax2.xaxis.set_major_formatter(
        mdates.DateFormatter("%H:%M" if span_seconds <= 86400 else "%b %d %H:%M")
    )

    fig.tight_layout()

    buffer = BytesIO()
    fig.savefig(buffer, format="png", dpi=120, facecolor=bg)
    plt.close(fig)
    buffer.seek(0)
    return buffer


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("StarlightCraft Discord bot is ready.")

    # on_ready can fire more than once (reconnects), so only start things once.
    if not poll_tps.is_running():
        await asyncio.to_thread(load_tps_history)
        poll_tps.start()


# ---------------------------------------------------------------------------
# General commands
# ---------------------------------------------------------------------------

@bot.command()
async def ip(ctx: commands.Context):
    embed = discord.Embed(
        title="StarlightCraft",
        color=discord.Color.blurple(),
    )
    embed.add_field(name="Server IP", value=f"`{SERVER_IP}`", inline=False)
    embed.add_field(name="Version", value=f"`{SERVER_VERSION}`", inline=False)
    await ctx.send(embed=embed)


@bot.command()
async def playerlist(ctx: commands.Context):
    try:
        response = await asyncio.to_thread(get_playerlist)

        # Typical response:
        # There are 2 of a max of 20 players online: Player1, Player2
        player_section = response.split(":", 1)[1].strip() if ":" in response else ""
        players = [
            player.strip()
            for player in player_section.split(",")
            if player.strip()
        ]

        embed = discord.Embed(
            description=f"**Players Online: {len(players)} / {MAX_PLAYERS}**",
            color=discord.Color.blurple(),
        )

        if players:
            player_lines = []
            for player in players:
                emoji = discord.utils.find(
                    lambda item: item.name.lower() == player.lower(),
                    ctx.guild.emojis,
                ) if ctx.guild else None

                player_lines.append(f"{emoji} `{player}`" if emoji else f"👤 `{player}`")

            embed.add_field(name="Online Players", value="\n".join(player_lines), inline=False)
        else:
            embed.add_field(
                name="Online Players",
                value="Nobody is online right now.",
                inline=False,
            )

        embed.set_footer(text="StarlightCraft")
        await ctx.send(embed=embed)

    except Exception as exc:
        await ctx.send(f"⚠️ Could not retrieve the player list: `{exc}`")


@bot.command()
async def tps(ctx: commands.Context):
    """Show current server TPS, MSPT, CPU and RAM."""
    try:
        data = await asyncio.to_thread(fetch_tps)
    except Exception as exc:
        await ctx.send(f"⚠️ Could not retrieve TPS: `{exc}`")
        return

    t = data["tps"]
    embed = discord.Embed(
        title="Server Performance",
        color=tps_color(t["1m"]),
        timestamp=discord.utils.utcnow(),
    )

    embed.add_field(
        name=f"{tps_icon(t['5s'])} TPS",
        value=(
            f"`5s`  **{t['5s']:.2f}**\n"
            f"`1m`  **{t['1m']:.2f}**\n"
            f"`5m`  **{t['5m']:.2f}**\n"
            f"`15m` **{t['15m']:.2f}**"
        ),
        inline=True,
    )

    if data["mspt"]:
        lines = ["`window  avg   min   max`"]
        for window, m in data["mspt"].items():
            lines.append(
                f"`{window:<6} {m['avg']:>5.1f} {m['min']:>5.1f} {m['max']:>5.1f}`"
            )
        embed.add_field(name="MSPT (ms)", value="\n".join(lines), inline=True)

    if data["cpu"]:
        embed.add_field(
            name="CPU",
            value=f"System: **{data['cpu']['sys']:.1f}%**\nProcess: **{data['cpu']['proc']:.1f}%**",
            inline=False,
        )

    if data["ram"]:
        r = data["ram"]
        embed.add_field(
            name="RAM",
            value=(
                f"`{text_bar(r['used'] / r['max'])}` "
                f"{r['used']}M / {r['max']}M max "
                f"({r['used'] / r['max'] * 100:.0f}%)"
            ),
            inline=False,
        )

    embed.set_footer(text="StarlightCraft")
    await ctx.send(embed=embed)


@bot.command()
async def tpsgraph(ctx: commands.Context, duration: str = "1h"):
    """Graph TPS/MSPT history. Usage: !tpsgraph [30m|2h|1d]"""
    try:
        span = parse_duration(duration)
    except ValueError as exc:
        await ctx.reply(str(exc))
        return

    max_span = TPS_HISTORY_HOURS * 3600
    span = max(60, min(span, max_span))

    cutoff = time.time() - span
    samples = [s for s in tps_history if s[0] >= cutoff]

    if len(samples) < 2:
        await ctx.reply(
            "Not enough TPS data collected yet. Give it a minute and try again."
        )
        return

    async with ctx.typing():
        try:
            buffer = await asyncio.to_thread(render_tps_graph, samples, span)
        except Exception as exc:
            await ctx.send(f"⚠️ Could not render the graph: `{exc}`")
            return

    covered = samples[-1][0] - samples[0][0]
    tps_values = [s[1] for s in samples if not math.isnan(s[1])]
    avg = sum(tps_values) / len(tps_values)
    low = min(tps_values)

    embed = discord.Embed(
        title="TPS History",
        description=(
            f"Last **{covered / 60:.0f} min** of data - "
            f"avg **{avg:.2f}** TPS, lowest **{low:.2f}**"
        ),
        color=tps_color(avg),
    )
    embed.set_image(url="attachment://tps_graph.png")
    embed.set_footer(text=f"StarlightCraft - sampled every {TPS_POLL_SECONDS}s")
    await ctx.send(embed=embed, file=discord.File(buffer, filename="tps_graph.png"))


@bot.command()
async def avatar(ctx: commands.Context, member: Optional[discord.Member] = None):
    member = member or ctx.author

    embed = discord.Embed(
        title=f"{member.display_name}'s Avatar",
        color=discord.Color.blurple(),
    )
    embed.set_image(url=member.display_avatar.url)
    embed.set_footer(text=f"Requested by {ctx.author.display_name}")
    await ctx.send(embed=embed)


@bot.command()
async def ping(ctx: commands.Context):
    latency = round(bot.latency * 1000)
    await ctx.send(f"Pong! `{latency}ms`")


# ---------------------------------------------------------------------------
# Admin commands
# ---------------------------------------------------------------------------

def is_weather_admin(ctx: commands.Context) -> bool:
    return ctx.author.id == WEATHER_ADMIN_ID


@bot.command()
async def skin(ctx: commands.Context, player: str, *, skinname: str):
    if not is_weather_admin(ctx):
        await ctx.reply("You don't have permission to use this command.")
        return

    # Restrict names to safe characters to prevent command/path injection.
    if not re.fullmatch(r"[a-zA-Z0-9_]{1,16}", player):
        await ctx.reply("Invalid Minecraft username.")
        return

    if not re.fullmatch(r"[a-zA-Z0-9_-]+", skinname):
        await ctx.reply("Invalid skin name.")
        return

    mc_command = (
        f"execute as {player} run "
        f"skin set upload classic skins/{skinname}.png"
    )

    try:
        response = await asyncio.to_thread(execute_rcon, mc_command)

        embed = discord.Embed(
            title="Skin Updated",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Player", value=f"`{player}`", inline=True)
        embed.add_field(name="Skin", value=f"`{skinname}`", inline=True)

        if response:
            embed.add_field(
                name="Server Response",
                value=f"```{response[:1000]}```",
                inline=False,
            )

        await ctx.send(embed=embed)

    except Exception as exc:
        await ctx.send(f"⚠️ Failed to change skin: `{exc}`")


@bot.command()
async def weather(
    ctx: commands.Context,
    weather_type: str,
    duration: Optional[int] = None,
):
    if not is_weather_admin(ctx):
        await ctx.reply("You don't have permission to use this command.")
        return

    weather_type = weather_type.lower()
    if weather_type not in {"clear", "rain", "thunder"}:
        await ctx.reply(
            "Invalid weather type!\nUse: `clear`, `rain`, or `thunder`."
        )
        return

    if duration is not None and duration <= 0:
        await ctx.reply("Duration must be greater than 0 ticks.")
        return

    mc_command = f"weather {weather_type}"
    if duration is not None:
        mc_command += f" {duration}"

    try:
        response = await asyncio.to_thread(execute_rcon, mc_command)

        embed = discord.Embed(
            title="Weather Updated",
            color=discord.Color.blurple(),
        )
        embed.add_field(name="Weather", value=f"`{weather_type}`", inline=True)
        embed.add_field(
            name="Duration",
            value=f"`{duration} ticks`" if duration is not None else "`Default`",
            inline=True,
        )
        embed.add_field(
            name="Executed Command",
            value=f"`/{mc_command}`",
            inline=False,
        )

        if response:
            embed.add_field(
                name="Server Response",
                value=f"```{response[:1000]}```",
                inline=False,
            )

        await ctx.send(embed=embed)

    except Exception as exc:
        await ctx.send(f"⚠️ Failed to change weather: `{exc}`")

@bot.command()
async def update(ctx: commands.Context, title: str, *, body: str):
    if not is_weather_admin(ctx):
        await ctx.reply("You don't have permission to use this command.")
        return

    if len(title) > 256:
        await ctx.reply("Title must be 256 characters or fewer.")
        return

    if len(body) > 4096:
        await ctx.reply("Body must be 4096 characters or fewer.")
        return

    embed = discord.Embed(
        title=title,
        description=body,
        color=discord.Color.blurple(),
        timestamp=discord.utils.utcnow(),
    )
    embed.set_footer(text="StarlightCraft")

    await ctx.send(embed=embed)

    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send(
            "⚠️ Posted, but I couldn't delete your message. "
            "I need the **Manage Messages** permission.",
            delete_after=10,
        )

# ---------------------------------------------------------------------------
# Petpet command
# --------------------------------------------------------------------------------------

@bot.command()
async def pet(
    ctx: commands.Context,
    image: Optional[Union[discord.PartialEmoji, discord.Member]] = None,
):
    if isinstance(image, discord.PartialEmoji):
        image_bytes = await image.read()
        filename = f"{image.name}-petpet.gif"
    elif isinstance(image, discord.Member):
        image_bytes = await image.display_avatar.read()
        filename = f"{image.name}-petpet.gif"
    else:
        await ctx.reply(
            "Please use a custom emoji or tag a member to petpet their avatar."
        )
        return

    source = BytesIO(image_bytes)
    destination = BytesIO()

    try:
        await asyncio.to_thread(petpetgif.make, source, destination)
        destination.seek(0)
        await ctx.send(file=discord.File(destination, filename=filename))
    except Exception as exc:
        await ctx.send(f"⚠️ Could not create the petpet GIF: `{exc}`")

async def post_status_embed(
    ctx: commands.Context,
    title: str,
    body: str,
    *,
    label: str,
    color: discord.Color,
):
    """Post a color-coded status embed, then delete the invoking message."""
    if not is_weather_admin(ctx):
        await ctx.reply("You don't have permission to use this command.")
        return

    if len(title) > 256:
        await ctx.reply("Title must be 256 characters or fewer.")
        return

    if len(body) > 4096:
        await ctx.reply("Body must be 4096 characters or fewer.")
        return

    embed = discord.Embed(
        title=title,
        description=body,
        color=color,
        timestamp=discord.utils.utcnow(),
    )
    embed.set_author(name=label)
    embed.set_footer(text="StarlightCraft")

    await ctx.send(embed=embed)

    try:
        await ctx.message.delete()
    except discord.Forbidden:
        await ctx.send(
            "⚠️ Posted, but I couldn't delete your message. "
            "I need the **Manage Messages** permission.",
            delete_after=10,
        )


@bot.command()
async def issue(ctx: commands.Context, title: str, *, body: str):
    await post_status_embed(
        ctx, title, body,
        label="🔴 Issue Reported",
        color=discord.Color.red(),
    )


@bot.command()
async def investigating(ctx: commands.Context, title: str, *, body: str):
    await post_status_embed(
        ctx, title, body,
        label="🟠 Investigating",
        color=discord.Color.orange(),
    )


@bot.command()
async def resolved(ctx: commands.Context, title: str, *, body: str):
    await post_status_embed(
        ctx, title, body,
        label="🟢 Resolved",
        color=discord.Color.green(),
    )

if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
