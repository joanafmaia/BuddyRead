# BuddyRead

A Discord bot for managing your reading life, personal library, and group reads. **BuddyRead** helps reading communities track progress, share quotes, and read books together — all within Discord.

## Features

### Personal library
- Search books via the **Google Books API** (title or ISBN)
- Organize books into three states: *Want to Read*, *Currently Reading*, and *Completed*
- Update page progress with a visual percentage bar
- Rate and write reviews when you finish a book
- View your full library, TBR list, and remove books

### Profile & stats
- Profile card with reading counters
- Annual reading challenge (`/challenge`)
- Unlockable achievement (badge) system
- Detailed activity history with pagination
- Server leaderboard

### Group reads (Buddy Read)
- Create joint reading events
- Other members can join with a button
- Compare each participant's progress on the same book

### Quotes
- Save memorable book quotes
- Get a random quote from the community

### Reminders
- Automatic DM reminders when no reading activity is logged for 2+ days
- Enable or disable with `/reminders`

## Commands

| Command | Description |
|---------|-------------|
| `/help` | Bot usage guide |
| `/search` | Search for a book and add it to your library |
| `/progress` | Update pages read for a book in progress |
| `/profile` | View profile, stats, and achievements |
| `/library` | View full library (yours or another member's) |
| `/tbr` | View your "Want to Read" list |
| `/remove_book` | Remove a book from your library |
| `/challenge` | Set your yearly book goal |
| `/history` | View your reading activity timeline |
| `/leaderboard` | View the server's reader rankings |
| `/reminders` | Enable or disable daily reminders |
| `/quote add` | Save a quote from a book |
| `/quote random` | Get a random quote |
| `/buddyread create` | Create a group reading event |
| `/buddyread status` | View member progress in a group |

## Tech stack

- **Python 3**
- **[discord.py](https://discordpy.readthedocs.io/)** — Discord API with slash commands and interactive components
- **[Motor](https://motor.readthedocs.io/)** — async MongoDB driver
- **[aiohttp](https://docs.aiohttp.org/)** — async HTTP requests to the Google Books API
- **MongoDB Atlas** — database for profiles, quotes, and reading groups
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

3. In the [Discord Developer Portal](https://discord.com/developers/applications), enable the following **Privileged Gateway Intents** for your bot:
- **Message Content Intent**

4. Invite the bot to your server with permissions to send messages, use slash commands, and send embeds.

> **Security note:** Never commit your `.env` file. It is listed in `.gitignore`.

## Run

```bash
python bot.py
```

Once the bot is online, slash commands sync automatically. You can get started with `/help` in your server.

## Deploy on Render (free tier)

On Render's free plan, use a **Web Service** (Background Workers are paid). The bot starts a small health endpoint on the `PORT` that Render provides — use it with [UptimeRobot](https://uptimerobot.com) to prevent the service from sleeping.

### Setup

1. **New +** → **Web Service** (or **Blueprint** with `render.yaml`)
2. Connect the `BuddyRead` GitHub repository
3. Configure:
   - **Build Command:** `pip install -r requirements.txt`
   - **Start Command:** `python bot.py`
4. Add environment variables: `DISCORD_TOKEN`, `MONGO_URI`, `GOOGLE_BOOKS_KEY`
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
- Enable **Message Content Intent** in the Discord Developer Portal

> Locally, the health server only starts when `PORT` is set — your `.env` does not need it.

## Deploy on Railway

1. **New Project** → **Deploy from GitHub** → select `BuddyRead`
2. Set **Start Command** to `python bot.py` (or use the included `Procfile`)
3. Add `DISCORD_TOKEN`, `MONGO_URI`, and `GOOGLE_BOOKS_KEY` under **Variables**
4. Deploy as a **Worker** service (not a web service)

## Database structure

The bot uses the `book_bot_db` database with three collections:

| Collection | Contents |
|------------|----------|
| `users` | Profiles, bookshelf, history, yearly goal, and preferences |
| `quotes` | Quotes saved by the community |
| `buddy_reads` | Group reading sessions |

## Available achievements

- **First Step** — Finish your first book
- **Bookworm** — Finish 5 books
- **Leviathan Slayer** — Read a book with 400+ pages

## License

Personal project. Use and adapt freely.
