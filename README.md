# BuddyRead

A Discord bot for managing your reading life, personal library, and group reads. **BuddyRead** helps reading communities track progress and read books together — all within Discord.

## Features

### Personal library
- Search books via the **Google Books API** (title or ISBN) — search is private
- Save books to your **wishlist** with `/search`
- Browse your full catalog with `/library` (filters for reading, finished, wishlist, paused; edit/remove from the UI)
- Update reading status: *Reading*, *Finished*, *Wishlist*, or *Paused*
- Log page progress with heart progress bars
- Optional private star rating when you finish a book
- View reading activity from your profile diary

### Profile & stats
- Profile card with reading counters (yours or others')
- Annual reading challenge (button on `/profile`)
- Unlockable achievement (badge) system
- Server leaderboard (members of the current server only)

### Group reads (Book Club)
- `/bookclub create` — start a group read (invite in chat), or mark it as Book of the Month
- Join with a button (adds the book to your shelf as *Reading* automatically)
- Compare progress with `/bookclub status`

## Commands

| Command | Description |
|---------|-------------|
| `/help` | List all available commands |
| `/search` | Private search; add a book to your library |
| `/library` | Browse shelves; on yours, pick a book to edit or remove |
| `/progress` | Log pages read or update reading status |
| `/profile` | View profile, stats, diary & challenge |
| `/leaderboard` | Server reader rankings (this year or all time) |
| `/bookclub create` | Start a group read (optionally Book of the Month) |
| `/bookclub status` | View progress; join, repost, or delete |

## Tech stack

- **Python 3**
- **[discord.py](https://discordpy.readthedocs.io/)** — Discord API with slash commands and interactive components
- **[Motor](https://motor.readthedocs.io/)** — async MongoDB driver
- **[aiohttp](https://docs.aiohttp.org/)** — async HTTP requests to the Google Books API
- **MongoDB Atlas** — database for profiles and reading groups
- **Google Books API** — book search and metadata

## Prerequisites

1. [Python 3.10+](https://www.python.org/downloads/)
2. A [Discord Developer Portal](https://discord.com/developers/applications) application with a bot created
3. A [MongoDB Atlas](https://www.mongodb.com/atlas) database (or local instance)
4. A [Google Books API](https://developers.google.com/books) key

## Installation

```bash
# Clone or download the repository
cd book-bot

# Create a virtual environment (recommended)
python -m venv venv
venv\Scripts\activate        # Windows
# source venv/bin/activate   # macOS / Linux

# Install dependencies
pip install -r requirements.txt
```

## Configuration

1. Copy the example environment file and fill in your credentials:

```bash
cp .env.example .env        # macOS / Linux
copy .env.example .env      # Windows
```

2. Edit `.env` with your values:

| Variable | Description |
|----------|-------------|
| `DISCORD_TOKEN` | Bot token from the [Discord Developer Portal](https://discord.com/developers/applications) |
| `MONGO_URI` | MongoDB connection string |
| `GOOGLE_BOOKS_KEY` | Google Books API key |
| `GUILD_ID` | (Optional) Discord server ID for instant command sync |
| `ANNOUNCE_CHANNEL_ID` | (Optional) Channel for book-finished celebrations |
| `BOOKCLUB_CHANNEL_ID` | Channel for Book of the Month posts |

3. Invite the bot to your server with permissions to send messages, use slash commands, and send embeds. **Message Content Intent is not required** for slash-command usage.

> **Security note:** Never commit your `.env` file. It is listed in `.gitignore`.

## Run

```bash
python bot.py
```

Once the bot is online, slash commands sync automatically. Use `/help` to see all commands.

## Deploy on Render (free tier)

On Render's free plan, use a **Web Service** (Background Workers are paid). The bot starts a small health endpoint on the `PORT` that Render provides — use it with [UptimeRobot](https://uptimerobot.com) to prevent the service from sleeping.

### Setup

1. **New +** → **Web Service** (or **Blueprint** with `render.yaml`)
2. Connect the `BuddyRead` GitHub repository
3. Configure:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
4. Add environment variables: `DISCORD_TOKEN`, `MONGO_URI`, `GOOGLE_BOOKS_KEY`, and optionally `GUILD_ID`, `ANNOUNCE_CHANNEL_ID`, `BOOKCLUB_CHANNEL_ID`
5. Deploy and copy your service URL (e.g. `https://buddyread.onrender.com`)

### Keep it awake with UptimeRobot

1. Create a free account at [uptimerobot.com](https://uptimerobot.com)
2. **Add Monitor** → type **HTTP(s)**
3. URL: `https://your-app.onrender.com/health`
4. Interval: **5 minutes**
5. Save

UptimeRobot pings `/health` every 5 minutes so Render does not spin down the service.

### Before deploying

- Stop any local instance of the bot (only one process can use the same Discord token)
- In MongoDB Atlas → **Network Access**, allow `0.0.0.0/0`
- Invite the bot with slash-command permissions (Message Content Intent not required)

> Locally, the health server only starts when `PORT` is set — your `.env` does not need it.

## Database structure

The bot uses the `book_bot_db` database with two collections:

| Collection | Contents |
|------------|----------|
| `users` | Profiles, bookshelf, history, yearly goal, and preferences |
| `buddy_reads` | Group reading sessions |

## License

Personal project. Use and adapt freely.
