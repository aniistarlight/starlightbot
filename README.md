# StarlightCraft Discord Bot

A small prefix-command Discord bot for the StarlightCraft Minecraft server. It
provides server information, player lookup, avatar utilities, and selected
Minecraft RCON commands.

## Features

| Command | Description |
| --- | --- |
| `!ip` | Shows the Minecraft server address and version |
| `!playerlist` | Gets the online player list through RCON |
| `!avatar [@member]` | Displays a Discord avatar |
| `!ping` | Shows the bot's gateway latency |
| `!pet <custom emoji or @member>` | Creates a petpet GIF |
| `!skin <player> <skinname>` | Changes a player's skin through RCON (admin only) |
| `!weather <clear/rain/thunder> [ticks]` | Changes the server weather (admin only) |

## Requirements

- Python 3.10 or newer
- A Discord application/bot and its token
- Minecraft server RCON enabled and reachable from the machine running this bot

## Setup

1. Clone this repository:

   ```bash
   git clone <your-repository-url>
   cd starlightcraft-discord-bot
   ```

2. Create and activate a virtual environment:

   ```bash
   python -m venv .venv
   ```

   Linux/macOS:

   ```bash
   source .venv/bin/activate
   ```

   Windows PowerShell:

   ```powershell
   .venv\Scripts\Activate.ps1
   ```

3. Install dependencies:

   ```bash
   python -m pip install -r requirements.txt
   ```

4. Create your local environment file:

   ```bash
   cp .env.example .env
   ```

   On Windows, copy `.env.example` to `.env` manually if `cp` is unavailable.

5. Edit `.env` and fill in your Discord token and RCON connection details.

6. In the Discord Developer Portal, enable **Message Content Intent** for the bot.
   Invite it to your server with the permissions needed to read and send messages
   and embeds.

7. Start the bot:

   ```bash
   python bot.py
   ```

## RCON setup

Enable RCON in the Minecraft server configuration and set a strong password.
Make sure the host/port are reachable from the computer running the bot. Do not
expose RCON publicly unless your host provides a secure, restricted connection.

## Admin access

The `!skin` and `!weather` commands are restricted to the Discord user ID
configured as `WEATHER_ADMIN_ID` near the top of `bot.py`. Change that value to
the intended administrator's Discord user ID before running the bot.

## Security notes

- **Never commit `.env`.** It contains your Discord token and RCON password.
- If a token or RCON password is accidentally committed, rotate it immediately.
- Keep the RCON port restricted to trusted connections.
- The bot uses `asyncio.to_thread()` for RCON calls so blocking network work
  does not freeze Discord's event loop.
