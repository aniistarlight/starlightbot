import asyncio
import os
import re
from io import BytesIO
from typing import Optional, Union

import discord
from discord.ext import commands
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
        return response.strip()
    finally:
        rcon.stop()


def get_playerlist() -> str:
    return execute_rcon("list")


# ---------------------------------------------------------------------------
# Events
# ---------------------------------------------------------------------------

@bot.event
async def on_ready():
    print(f"Logged in as {bot.user} (ID: {bot.user.id})")
    print("StarlightCraft Discord bot is ready.")


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


if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
