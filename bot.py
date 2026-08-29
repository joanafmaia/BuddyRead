import os
import asyncio
import discord
import certifi
from discord.ext import commands, tasks
import aiohttp
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# 1. SETUP DISCORD BOT
intents = discord.Intents.default()
intents.message_content = True


class BookBot(commands.Bot):
    async def setup_hook(self):
        self.add_view(BuddyJoinView())
        if not weekly_club_pulse.is_running():
            weekly_club_pulse.start()


bot = BookBot(command_prefix="!", intents=intents)

http_session = None
_thumbnail_cache = {}


async def get_http_session():
    global http_session
    if http_session is None or http_session.closed:
        http_session = aiohttp.ClientSession()
    return http_session


def parse_dt(value):
    if value is None:
        return None
    if isinstance(value, datetime):
        return value
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00"))
        except ValueError:
            return None
    return None


def compute_streak(last_read, current_streak, now=None):
    now = now or datetime.now()
    last = parse_dt(last_read)
    streak = int(current_streak or 0)
    if last is None:
        return 1
    delta = (now.date() - last.date()).days
    if delta == 0:
        return max(streak, 1)
    if delta == 1:
        return streak + 1
    return 1


def display_streak(profile):
    last = parse_dt(profile.get("last_read"))
    streak = int(profile.get("streak") or 0)
    if last is None or streak <= 0:
        return 0
    if (datetime.now().date() - last.date()).days > 1:
        return 0
    return streak


def stamp_completed(book, year=None):
    book["completed_year"] = year or current_year()
    book["completed_at"] = datetime.now().isoformat()


def book_completed_sort_key(book):
    completed_at = parse_dt(book.get("completed_at"))
    if completed_at:
        return completed_at.timestamp()
    year = book.get("completed_year") or 0
    try:
        year = int(year)
    except (TypeError, ValueError):
        year = 0
    if year <= 0:
        return 0
    return datetime(year, 12, 31).timestamp()


def activity_fields(profile, now=None):
    now = now or datetime.now()
    return {
        "last_read": now,
        "streak": compute_streak(profile.get("last_read"), profile.get("streak"), now),
    }
DISCORD_TOKEN = os.getenv("DISCORD_TOKEN")
MONGO_URI = os.getenv("MONGO_URI")
GOOGLE_BOOKS_KEY = os.getenv("GOOGLE_BOOKS_KEY")
GUILD_ID = os.getenv("GUILD_ID")
ANNOUNCE_CHANNEL_ID = os.getenv("ANNOUNCE_CHANNEL_ID", "1517151054246973611")
BOOKCLUB_CHANNEL_ID = os.getenv("BOOKCLUB_CHANNEL_ID")

if not all([DISCORD_TOKEN, MONGO_URI, GOOGLE_BOOKS_KEY]):
    raise RuntimeError(
        "Missing required environment variables. "
        "Copy .env.example to .env and fill in DISCORD_TOKEN, MONGO_URI, and GOOGLE_BOOKS_KEY."
    )

db_client = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())

db = db_client["book_bot_db"]
users_col = db["users"]
buddies_col = db["buddy_reads"]

COLORS = {
    "library": 0xFFB7C5,
    "reading": 0xE8B4F8,
    "completed": 0xFFD1DC,
    "to_read": 0xDDA0DD,
    "abandoned": 0xE6E6FA,
    "social": 0xFFCCE5,
    "achievements": 0xFFDAB9,
    "profile": 0xF8BBD9,
    "wishlist": 0xE6E6FA,
    "search": 0xFFB7C5,
}

COZY_AUTHOR = "BuddyRead · your cozy reading corner ✨"
COZY_DIVIDER = "˚ · ☆ · ˚ · ☆ · ˚"


def apply_cozy_style(embed):
    embed.set_author(name=COZY_AUTHOR)
    return embed


def cozy_stars(rating, empty="not rated yet"):
    if not rating:
        return empty
    return "💛" * int(rating)


def progress_bar(current, total, length=8):
    if total <= 0:
        return "🤍" * length
    percent = min(max(current / total, 0), 1)
    filled = round(percent * length)
    return "💗" * filled + "🤍" * (length - filled)


def format_reading_book(book):
    total = book.get("total_pages", 0) or 0
    current = book.get("current_page", 0) or 0
    bar = progress_bar(current, total)
    pct = round((current / total) * 100) if total > 0 else 0
    return f"💭 **{book['title']}**\n`{bar}` **{pct}%** · page {current}/{total if total > 0 else '?'}"


def format_completed_book(book):
    rating = cozy_stars(book.get("rating"), "No rating")
    year_bit = f", {book['completed_year']}" if book.get("completed_year") else ""
    return f"✨ **{book['title']}** ({rating}{year_bit})"


def average_rating(completed_books):
    rated = [b["rating"] for b in completed_books if b.get("rating")]
    if not rated:
        return None
    return round(sum(rated) / len(rated), 1)


def favorite_genre(completed_books):
    counts = {}
    for book in completed_books:
        for genre in get_book_genres(book):
            counts[genre] = counts.get(genre, 0) + 1
    if not counts:
        return None
    top = max(counts, key=counts.get)
    return GENRE_LABELS.get(top, top.title())


def find_group_book(bookshelf, group):
    if group.get("book_id"):
        book = find_shelf_book(bookshelf, group["book_id"])
        if book:
            return book
    title = group.get("book_title", "").lower()
    return next(
        (
            book for book in bookshelf
            if title in book.get("title", "").lower() or book.get("title", "").lower() in title
        ),
        None,
    )


async def health_check(_request):
    return web.Response(text="BuddyRead is online")


async def start_health_server():
    port = int(os.getenv("PORT", 10000))
    app = web.Application()
    app.router.add_get("/", health_check)
    app.router.add_get("/health", health_check)
    runner = web.AppRunner(app)
    await runner.setup()
    site = web.TCPSite(runner, "0.0.0.0", port)
    await site.start()
    print(f"Health server listening on port {port}")


async def fetch_book_thumbnail(book_id):
    if not book_id:
        return ""
    cached = _thumbnail_cache.get(book_id)
    if cached is not None:
        return cached
    api_url = f"https://www.googleapis.com/books/v1/volumes/{book_id}?key={GOOGLE_BOOKS_KEY}"
    try:
        session = await get_http_session()
        async with session.get(api_url) as resp:
            if resp.status == 200:
                data = await resp.json()
                image_links = data.get("volumeInfo", {}).get("imageLinks", {})
                thumbnail = image_links.get("thumbnail") or image_links.get("smallThumbnail") or ""
                thumbnail = thumbnail.replace("http://", "https://")
                _thumbnail_cache[book_id] = thumbnail
                return thumbnail
    except Exception:
        pass
    _thumbnail_cache[book_id] = ""
    return ""


def build_book_embed(volume_info):
    book_title = volume_info.get("title", "Unknown Title")
    authors = ", ".join(volume_info.get("authors", ["Unknown Author"]))
    description = volume_info.get("description", "No description available.")
    if len(description) > 400:
        description = description[:400] + "..."
    pages = volume_info.get("pageCount", 0)
    categories = volume_info.get("categories", [])
    thumbnail = volume_info.get("imageLinks", {}).get("thumbnail", "").replace("http://", "https://")

    embed = discord.Embed(
        title=f"🌸 {book_title}",
        description=(
            f"**Author(s):** {authors}\n"
            f"**Pages:** {pages if pages > 0 else '???'}\n\n"
            f"*{description}*"
        ),
        color=COLORS["search"],
    )
    apply_cozy_style(embed)
    if categories:
        embed.add_field(name="📂 Genre", value=categories[0], inline=True)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed


def build_search_results_embed(items, query):
    embed = discord.Embed(
        title="🎀 Book Search",
        description=(
            f"Found **{len(items)}** matches for **{query}**.\n"
            "Only you can see this — pick a book in the menu below."
        ),
        color=COLORS["search"],
    )
    apply_cozy_style(embed)
    return embed


SHELF_ACTION_COPY = {
    "to_read": ("💌 Wishlist", COLORS["wishlist"], "Added to your wishlist. Open it anytime with `/library`."),
    "reading": ("💭 Reading now", COLORS["reading"], "You're reading this — log pages with `/progress`."),
    "completed": ("✨ Finished", COLORS["completed"], "Marked as finished. Rate it below if you like."),
}


def build_shelf_action_embed(title, status, display_name=None):
    heading, color, body = SHELF_ACTION_COPY[status]
    who = f"**{display_name}** · " if display_name else ""
    embed = discord.Embed(
        title=heading,
        description=f"{who}**{title}**\n{body}",
        color=color,
    )
    return apply_cozy_style(embed)


def current_year():
    return datetime.now().year


def get_book_completed_year(book):
    return book.get("completed_year") or current_year()


def books_completed_in_year(bookshelf, year=None):
    year = year or current_year()
    return [
        b for b in bookshelf
        if b.get("status") == "completed" and get_book_completed_year(b) == year
    ]


def is_current_year_finish(book):
    return get_book_completed_year(book) == current_year()


GENRE_ACHIEVEMENTS = [
    ("fantasy", ["fantasy"], "🐲 **Dragon Reader**", "Finished your first Fantasy book!"),
    ("sci_fi", ["science fiction", "sci-fi"], "🚀 **Space Cadet**", "Finished your first Sci-Fi book!"),
    ("romance", ["romance"], "💕 **Hopeless Romantic**", "Finished your first Romance book!"),
    ("horror", ["horror"], "👻 **Brave Soul**", "Finished your first Horror book!"),
    ("mystery", ["mystery", "thriller", "suspense"], "🔍 **Detective**", "Finished your first Mystery/Thriller book!"),
    ("biography", ["biography", "autobiograph"], "📖 **Life Explorer**", "Finished your first Biography!"),
    ("history", ["history"], "⏳ **Time Traveler**", "Finished your first History book!"),
]

DIVERSITY_ACHIEVEMENTS = [
    (3, "🎭 **Genre Hopper**", "Finished books from 3 different genres!"),
    (5, "🌈 **Renaissance Reader**", "Finished books from 5 different genres!"),
]

SUBGENRE_ACHIEVEMENTS = [
    ("dark_romance", ["dark romance", "gothic romance", "mafia romance"], [
        (1, "🖤 **Dark Heart**", "Finished your first Dark Romance!"),
        (5, "🖤 **Twisted Soul**", "Finished 5 Dark Romance books!"),
    ]),
]

SPECIALIST_ACHIEVEMENTS = [
    ("fantasy", [
        (3, "🐲 **Fantasy Fanatic**", "Finished 3 Fantasy books!"),
        (5, "🐲 **Fantasy Overlord**", "Finished 5 Fantasy books!"),
    ]),
    ("romance", [
        (3, "💕 **Romance Addict**", "Finished 3 Romance books!"),
        (5, "💕 **Romance Devotee**", "Finished 5 Romance books!"),
    ]),
    ("mystery", [
        (3, "🔍 **Mystery Buff**", "Finished 3 Mystery/Thriller books!"),
        (5, "🔍 **Case Closed**", "Finished 5 Mystery/Thriller books!"),
    ]),
]

GENRE_LABELS = {
    "fantasy": "Fantasy",
    "sci_fi": "Sci-Fi",
    "romance": "Romance",
    "horror": "Horror",
    "mystery": "Mystery / Thriller",
    "biography": "Biography",
    "history": "History",
}


def normalize_genres(categories):
    if not categories:
        return set()
    combined = " ".join(categories).lower()
    found = set()
    for genre_key, keywords, _, _ in GENRE_ACHIEVEMENTS:
        if any(keyword in combined for keyword in keywords):
            found.add(genre_key)
    return found


def normalize_subgenres(categories):
    if not categories:
        return set()
    combined = " ".join(categories).lower()
    found = set()
    for subgenre_key, keywords, _ in SUBGENRE_ACHIEVEMENTS:
        if any(keyword in combined for keyword in keywords):
            found.add(subgenre_key)
        elif subgenre_key == "dark_romance" and "dark" in combined and "romance" in combined:
            found.add(subgenre_key)
    return found


def get_book_genres(book):
    if book.get("genres"):
        return set(book["genres"])
    return normalize_genres(book.get("categories", []))


def get_book_subgenres(book):
    if book.get("subgenres"):
        return set(book["subgenres"])
    return normalize_subgenres(book.get("categories", []))


def get_completed_genres(completed_books):
    genres = set()
    for book in completed_books:
        genres.update(get_book_genres(book))
    return genres


def count_books_with_genre(completed_books, genre_key):
    return sum(1 for book in completed_books if genre_key in get_book_genres(book))


def count_books_with_subgenre(completed_books, subgenre_key):
    return sum(1 for book in completed_books if subgenre_key in get_book_subgenres(book))


def append_tier_unlocks(achievements, previous_books, completed_books, counter_fn, key, tiers):
    previous_count = counter_fn(previous_books, key)
    total_count = counter_fn(completed_books, key)
    for threshold, name, desc in tiers:
        if total_count >= threshold and previous_count < threshold:
            achievements.append(f"{name} — {desc}")


async def mark_buddy_read_finish(user_id, book):
    if book.get("buddy_read_finish"):
        return
    cursor = buddies_col.find({"members": str(user_id)})
    groups = await cursor.to_list(length=100)
    book_title = book["title"].lower()
    for group in groups:
        group_title = group["book_title"].lower()
        if group_title in book_title or book_title in group_title:
            book["buddy_read_finish"] = True
            return


def collect_extra_badges(completed, book=None, previous_books=None, for_finish=False):
    badges = []
    previous_books = previous_books if previous_books is not None else []

    if for_finish and book is not None:
        pages = book.get("total_pages", 0)
        if 0 < pages < 150:
            badges.append("📖 **Short & Sweet** — Finished a book under 150 pages!")
    elif not for_finish and any(0 < b.get("total_pages", 0) < 150 for b in completed):
        badges.append("📖 **Short & Sweet:** Finished a book under 150 pages!")

    brick_count = sum(1 for b in completed if b.get("total_pages", 0) >= 400)
    prev_brick = sum(1 for b in previous_books if b.get("total_pages", 0) >= 400)
    if for_finish:
        if brick_count >= 3 and prev_brick < 3:
            badges.append("🗿 **Brick Layer** — Finished 3 books with 400+ pages!")
    elif brick_count >= 3:
        badges.append("🗿 **Brick Layer:** Finished 3 books with 400+ pages!")

    if for_finish and book is not None:
        if book.get("buddy_read_finish") and not any(b.get("buddy_read_finish") for b in previous_books):
            badges.append("👥 **Buddy Reader** — Finished a Buddy Read book!")
    elif not for_finish and any(b.get("buddy_read_finish") for b in completed):
        badges.append("👥 **Buddy Reader:** Finished a Buddy Read book!")

    for subgenre_key, _, tiers in SUBGENRE_ACHIEVEMENTS:
        append_tier_unlocks(badges, previous_books, completed, count_books_with_subgenre, subgenre_key, tiers)

    for genre_key, tiers in SPECIALIST_ACHIEVEMENTS:
        append_tier_unlocks(badges, previous_books, completed, count_books_with_genre, genre_key, tiers)

    return badges


def build_achievements_help():
    return (
        "**General:** First Step · Bookworm · Leviathan Slayer\n"
        "**Genres:** Dragon Reader · Space Cadet · Hopeless Romantic · Brave Soul · "
        "Detective · Life Explorer · Time Traveler\n"
        "**Diversity:** Genre Hopper (3 genres) · Renaissance Reader (5 genres)\n"
        "**Subgenre:** Dark Heart · Twisted Soul (dark romance)\n"
        "**Specialist:** Fantasy / Romance / Mystery Fanatic (3) & Overlord (5)\n"
        "**Fun:** Short & Sweet · Brick Layer · Buddy Reader"
    )


def get_profile_badges(completed):
    badges = []
    if len(completed) >= 1:
        badges.append("✨ **First Step:** Finished your first book!")
    if len(completed) >= 5:
        badges.append("📚 **Bookworm:** Finished 5 books total.")
    if any(b.get("total_pages", 0) >= 400 for b in completed):
        badges.append("🐉 **Leviathan Slayer:** Read a book with 400+ pages.")

    completed_genres = get_completed_genres(completed)
    for genre_key, _, name, desc in GENRE_ACHIEVEMENTS:
        if genre_key in completed_genres:
            badges.append(f"{name}: {desc}")

    for threshold, name, desc in DIVERSITY_ACHIEVEMENTS:
        if len(completed_genres) >= threshold:
            badges.append(f"{name}: {desc}")

    badges.extend(collect_extra_badges(completed))
    return badges


def get_finish_achievements(completed_books, book):
    completed_count = len(completed_books)
    achievements = []
    if completed_count == 1:
        achievements.append("✨ **First Step** — First book finished!")
    if completed_count == 5:
        achievements.append("📚 **Bookworm** — 5 books finished!")
    if book.get("total_pages", 0) >= 400:
        achievements.append("🐉 **Leviathan Slayer** — Finished a 400+ page book!")

    book_genres = get_book_genres(book)
    previous_books = [b for b in completed_books if b.get("book_id") != book.get("book_id")]
    previous_genres = get_completed_genres(previous_books)
    all_genres = get_completed_genres(completed_books)

    for genre_key, _, name, desc in GENRE_ACHIEVEMENTS:
        if genre_key in book_genres and genre_key not in previous_genres:
            achievements.append(f"{name} — {desc}")

    for threshold, name, desc in DIVERSITY_ACHIEVEMENTS:
        if len(all_genres) >= threshold and len(previous_genres) < threshold:
            achievements.append(f"{name} — {desc}")

    achievements.extend(collect_extra_badges(completed_books, book, previous_books, for_finish=True))
    return achievements


async def get_announce_channel(bot_client):
    if not ANNOUNCE_CHANNEL_ID:
        print("ANNOUNCE_CHANNEL_ID not configured — skipping book finished announcement")
        return None
    return await get_configured_channel(bot_client, ANNOUNCE_CHANNEL_ID)


async def get_configured_channel(bot_client, channel_id_value):
    if not channel_id_value:
        return None

    channel_id = int(channel_id_value)

    try:
        return await bot_client.fetch_channel(channel_id)
    except Exception as e:
        print(f"fetch_channel failed for {channel_id}: {e}")

    if GUILD_ID:
        try:
            guild = bot_client.get_guild(int(GUILD_ID))
            if guild is None:
                guild = await bot_client.fetch_guild(int(GUILD_ID))
            return await guild.fetch_channel(channel_id)
        except Exception as e:
            print(f"guild fetch_channel failed for {channel_id}: {e}")

    return None


def build_bookclub_invite_embed(host_name, book_title, reminder=False, member_count=1, group_id=None):
    title = "🔔 Book Club Reminder" if reminder else "👯‍♀️ Book of the Month"
    description = (
        f"**{book_title}**\n"
        f"Join this month's cozy group read 💕\n"
        f"👯 **{member_count}** reader{'s' if member_count != 1 else ''} joined so far"
    )
    embed = discord.Embed(title=title, description=description, color=COLORS["social"])
    apply_cozy_style(embed)
    if group_id:
        embed.set_footer(text=f"id:{group_id}")
    return embed


async def post_bookclub_invite(bot_client, group, reminder=False):
    channel = await get_configured_channel(bot_client, BOOKCLUB_CHANNEL_ID)
    if channel is None:
        return False

    embed = build_bookclub_invite_embed(
        group["host_name"], group["book_title"], reminder=reminder,
        member_count=len(group.get("members", [])),
        group_id=group["_id"],
    )
    thumbnail = group.get("thumbnail_url") or await fetch_book_thumbnail(group.get("book_id"))
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    await channel.send(embed=embed, view=BuddyJoinView())
    return True


async def hydrate_legacy_bookclub_group(group):
    if group.get("book_id") and group.get("thumbnail_url"):
        return group

    host_id = group.get("host_id")
    if not host_id:
        return group

    host_profile = await users_col.find_one({"_id": host_id})
    if not host_profile or not host_profile.get("bookshelf"):
        return group

    matched_book = next(
        (
            book for book in host_profile["bookshelf"]
            if book.get("title", "").lower() == group.get("book_title", "").lower()
        ),
        None,
    )
    if not matched_book:
        matched_book = next(
            (
                book for book in host_profile["bookshelf"]
                if group.get("book_title", "").lower() in book.get("title", "").lower()
                or book.get("title", "").lower() in group.get("book_title", "").lower()
            ),
            None,
        )
    if not matched_book:
        return group

    updates = {}
    if matched_book.get("book_id") and not group.get("book_id"):
        updates["book_id"] = matched_book.get("book_id")
    if updates.get("book_id") or group.get("book_id"):
        thumbnail = await fetch_book_thumbnail(updates.get("book_id") or group.get("book_id"))
        if thumbnail and not group.get("thumbnail_url"):
            updates["thumbnail_url"] = thumbnail

    if updates:
        await buddies_col.update_one({"_id": group["_id"]}, {"$set": updates})
        group.update(updates)

    return group


async def user_in_guild(guild, user_id):
    if guild is None:
        return False
    member = guild.get_member(int(user_id))
    if member is not None:
        return True
    try:
        await guild.fetch_member(int(user_id))
        return True
    except (discord.NotFound, discord.HTTPException, ValueError):
        return False


async def announce_book_finished(bot_client, member, book, completed_books, user_id):
    completed_count = len(completed_books)
    achievements = get_finish_achievements(completed_books, book)
    embed = discord.Embed(
        title="✨ Book Finished!",
        description=f"**{member.display_name}** just finished **{book['title']}** — so proud! 💕",
        color=COLORS["achievements"],
    )
    apply_cozy_style(embed)
    book_genres = get_book_genres(book)
    if book_genres:
        genre_text = ", ".join(GENRE_LABELS.get(g, g.title()) for g in book_genres)
        embed.add_field(name="📂 Genre", value=genre_text, inline=True)
    if achievements:
        embed.add_field(name="🎀 Achievements Unlocked", value="\n".join(achievements), inline=False)
    embed.set_footer(text=f"📚 Total books finished: {completed_count} · keep going! ✨")
    thumbnail = await fetch_book_thumbnail(book.get("book_id"))
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)

    channel = await get_announce_channel(bot_client)
    if channel is None:
        return

    try:
        await channel.send(embed=embed)
        print(f"Announced book finished: {book['title']} for user {user_id} in channel {channel.id}")
    except Exception as e:
        print(f"announce_book_finished send error: {e}")


# 3. PAGINATION VIEW
class PaginatorView(discord.ui.View):
    def __init__(self, title, data_list, color=None, items_per_page=5):
        super().__init__(timeout=180)
        self.title = title
        self.data_list = data_list
        self.color = color or COLORS["library"]
        self.items_per_page = items_per_page
        self.current_page = 0
        self.total_pages = max(1, (len(data_list) - 1) // items_per_page + 1)

    def get_embed(self):
        start = self.current_page * self.items_per_page
        end = start + self.items_per_page
        page_items = self.data_list[start:end]

        description = "\n".join(page_items) if page_items else "*nothing here yet — add some books! 💕*"
        embed = discord.Embed(title=self.title, description=description, color=self.color)
        apply_cozy_style(embed)
        embed.set_footer(
            text=f"page {self.current_page + 1} of {self.total_pages} · {len(self.data_list)} total · happy reading 💕"
        )
        return embed

    @discord.ui.button(label="Back", emoji="🩷", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next", emoji="🩷", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.defer()


LIBRARY_STATUS_ICONS = {
    "reading": "💭",
    "completed": "✨",
    "to_read": "💌",
    "abandoned": "🍂",
}

LIBRARY_SECTION_LABELS = {
    "all": "🎀 Full Collection",
    "reading": "💭 Currently Reading",
    "completed": "✨ Finished & Loved",
    "to_read": "💌 Wishlist",
    "abandoned": "🍂 Maybe Later",
}


def library_shelf_stats(bookshelf):
    counts = {"reading": 0, "completed": 0, "to_read": 0, "abandoned": 0}
    for book in bookshelf:
        status = book.get("status", "to_read")
        if status in counts:
            counts[status] += 1
    return counts


def format_library_card(book):
    title = book.get("title", "Unknown Title")
    status = book.get("status", "to_read")
    icon = LIBRARY_STATUS_ICONS.get(status, "📕")

    if status == "reading":
        total = book.get("total_pages", 0) or 0
        current = book.get("current_page", 0) or 0
        pct = round((current / total) * 100) if total > 0 else 0
        hearts = progress_bar(current, total)
        detail = f"{hearts}\n{pct}% · page {current}/{total if total > 0 else '?'}"
    elif status == "completed":
        rating = cozy_stars(book.get("rating"))
        year = book.get("completed_year")
        detail = f"loved it · {rating}" + (f"\nfinished in {year}" if year else "")
        review = (book.get("review") or "").strip()
        if review:
            snippet = review[:80] + ("..." if len(review) > 80 else "")
            detail += f"\n*{snippet}*"
    elif status == "abandoned":
        detail = "paused for now"
    else:
        pages = book.get("total_pages", 0) or 0
        detail = f"on the wishlist · {pages} pages" if pages > 0 else "waiting on the wishlist"

    return f"{icon} {title[:70]}", detail


class LibraryView(discord.ui.View):
    SECTIONS = {
        "all": (LIBRARY_SECTION_LABELS["all"], COLORS["library"], lambda b: True),
        "reading": (LIBRARY_SECTION_LABELS["reading"], COLORS["reading"], lambda b: b["status"] == "reading"),
        "completed": (LIBRARY_SECTION_LABELS["completed"], COLORS["completed"], lambda b: b["status"] == "completed"),
        "to_read": (LIBRARY_SECTION_LABELS["to_read"], COLORS["to_read"], lambda b: b["status"] == "to_read"),
        "abandoned": (LIBRARY_SECTION_LABELS["abandoned"], COLORS["abandoned"], lambda b: b["status"] == "abandoned"),
    }

    def __init__(self, display_name, bookshelf, section="all", owner_id=None, viewer_id=None):
        super().__init__(timeout=180)
        self.display_name = display_name
        self.bookshelf = bookshelf
        self.section = section
        self.current_page = 0
        self.items_per_page = 24
        self.owner_id = str(owner_id) if owner_id else None
        self.viewer_id = str(viewer_id) if viewer_id else None
        self.selected_key = None
        self._refresh_items()
        self._rebuild_section_select()
        self._rebuild_manage_controls()

    @property
    def is_owner(self):
        return bool(self.owner_id and self.owner_id == self.viewer_id)

    def page_books(self):
        start = self.current_page * self.items_per_page
        return self.filtered_books[start:start + self.items_per_page]

    def _rebuild_section_select(self):
        for child in self.children.copy():
            if isinstance(child, LibrarySectionSelect):
                self.remove_item(child)
        self.add_item(LibrarySectionSelect(self))

    def _rebuild_manage_controls(self):
        for child in self.children.copy():
            if isinstance(child, (LibraryManageBookSelect, LibraryManageActionSelect)):
                self.remove_item(child)
        if not self.is_owner or not self.page_books():
            self.selected_key = None
            return
        self.add_item(LibraryManageBookSelect(self))
        self.add_item(LibraryManageActionSelect(self))

    def _refresh_items(self):
        _, _, filter_fn = self.SECTIONS[self.section]
        self.filtered_books = sorted(
            [book for book in self.bookshelf if filter_fn(book)],
            key=lambda book: book.get("title", "").lower(),
        )
        self.total_pages = max(1, (len(self.filtered_books) - 1) // self.items_per_page + 1)
        if self.current_page >= self.total_pages:
            self.current_page = max(0, self.total_pages - 1)

    def get_embed(self):
        title, color, _ = self.SECTIONS[self.section]
        counts = library_shelf_stats(self.bookshelf)
        total_books = len(self.bookshelf)
        start = self.current_page * self.items_per_page
        page_books = self.page_books()

        if self.section == "all":
            shelf_label = "your whole cozy collection"
        else:
            shelf_label = title.lower()

        embed = discord.Embed(
            title=f"🎀 {self.display_name}'s Cozy Library",
            description=(
                f"✨ *welcome to {shelf_label}* ✨\n"
                f"**{len(self.filtered_books)}** sweet reads on this shelf\n\n"
                f"🌸 **{total_books}** books total\n"
                f"💭 {counts['reading']} reading  ·  "
                f"✨ {counts['completed']} finished  ·  "
                f"💌 {counts['to_read']} wishlist  ·  "
                f"🍂 {counts['abandoned']} paused\n"
                "˚ · ☆ · ˚ · ☆ · ˚ · ☆ · ˚"
            ),
            color=color,
        )
        embed.set_author(name=COZY_AUTHOR)

        if not page_books:
            embed.add_field(name="🌷 Empty shelf", value="*this shelf is waiting for its first book* 💕", inline=False)
        else:
            for index, book in enumerate(page_books, start=start + 1):
                name, value = format_library_card(book)
                embed.add_field(name=f"˚ {index:02d} · {name}", value=value, inline=True)

        embed.set_footer(
            text=(
                f"page {self.current_page + 1} of {self.total_pages} · happy reading 💕"
                + (" · pick a book below to edit or remove" if self.is_owner else "")
            )
        )
        featured_book_id = None
        if self.section == "reading":
            featured_book_id = next((book.get("book_id") for book in page_books if book.get("book_id")), None)
        return embed, featured_book_id

    async def apply_library_cover(self, embed, book_id):
        if self.section == "reading" and book_id:
            cover = await fetch_book_thumbnail(book_id)
            embed.set_image(url=cover if cover else None)
        else:
            embed.set_image(url=None)

    async def reload_from_db(self):
        profile = await users_col.find_one({"_id": self.owner_id})
        self.bookshelf = profile.get("bookshelf", []) if profile else []
        self._refresh_items()
        self._rebuild_section_select()
        self._rebuild_manage_controls()

    async def _edit_with_cover(self, interaction: discord.Interaction, embed, book_id):
        self._rebuild_manage_controls()
        await self.apply_library_cover(embed, book_id)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="Back", emoji="🩷", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            embed, book_id = self.get_embed()
            await self._edit_with_cover(interaction, embed, book_id)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="Next", emoji="🩷", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            embed, book_id = self.get_embed()
            await self._edit_with_cover(interaction, embed, book_id)
        else:
            await interaction.response.defer()


class LibrarySectionSelect(discord.ui.Select):
    def __init__(self, library_view):
        self.library_view = library_view
        current = library_view.section
        current_label, _, _ = LibraryView.SECTIONS[current]
        options = [
            discord.SelectOption(label=label, value=key, default=(key == current))
            for key, (label, _, _) in LibraryView.SECTIONS.items()
        ]
        super().__init__(
            placeholder=current_label,
            options=options,
            min_values=1,
            max_values=1,
            row=0,
        )

    async def callback(self, interaction: discord.Interaction):
        self.library_view.section = self.values[0]
        self.library_view.current_page = 0
        self.library_view.selected_key = None
        self.library_view._refresh_items()
        self.library_view._rebuild_section_select()
        self.library_view._rebuild_manage_controls()
        embed, book_id = self.library_view.get_embed()
        await self.library_view.apply_library_cover(embed, book_id)
        await interaction.response.edit_message(embed=embed, view=self.library_view)


class LibraryManageBookSelect(discord.ui.Select):
    def __init__(self, library_view):
        self.library_view = library_view
        options = []
        for book in library_view.page_books()[:25]:
            key = (book.get("book_id") or book.get("title") or "book")[:100]
            title = (book.get("title") or "Unknown Title")[:100]
            options.append(
                discord.SelectOption(
                    label=title,
                    value=key,
                    default=(key == library_view.selected_key),
                )
            )
        super().__init__(
            placeholder="Manage a book on this page",
            options=options,
            min_values=1,
            max_values=1,
            row=2,
        )

    async def callback(self, interaction: discord.Interaction):
        if not self.library_view.is_owner or str(interaction.user.id) != self.library_view.owner_id:
            await interaction.response.send_message("🌷 You can only manage your own library.", ephemeral=True)
            return
        self.library_view.selected_key = self.values[0]
        self.library_view._rebuild_manage_controls()
        embed, book_id = self.library_view.get_embed()
        await self.library_view.apply_library_cover(embed, book_id)
        await interaction.response.edit_message(embed=embed, view=self.library_view)


class LibraryManageActionSelect(discord.ui.Select):
    def __init__(self, library_view):
        self.library_view = library_view
        options = [
            discord.SelectOption(label="Reading now", value="reading"),
            discord.SelectOption(label="Finished", value="completed"),
            discord.SelectOption(label="Wishlist", value="to_read"),
            discord.SelectOption(label="Paused", value="abandoned"),
            discord.SelectOption(label="Edit rating / year / pages", value="edit"),
            discord.SelectOption(label="Remove from library", value="remove"),
        ]
        super().__init__(
            placeholder="Then choose an action",
            options=options,
            min_values=1,
            max_values=1,
            row=3,
        )

    async def callback(self, interaction: discord.Interaction):
        view = self.library_view
        if not view.is_owner or str(interaction.user.id) != view.owner_id:
            await interaction.response.send_message("🌷 You can only manage your own library.", ephemeral=True)
            return
        if not view.selected_key:
            await interaction.response.send_message("🌷 Pick a book in the menu above first.", ephemeral=True)
            return

        action = self.values[0]
        if action == "edit":
            book = find_shelf_book(view.bookshelf, view.selected_key)
            title = book["title"] if book else "this book"
            await interaction.response.send_modal(
                LibraryEditModal(view, view.selected_key, title, interaction.message)
            )
            return

        if action == "remove":
            removed = await remove_shelf_book(view.owner_id, view.selected_key)
            view.selected_key = None
            await view.reload_from_db()
            embed, book_id = view.get_embed()
            await view.apply_library_cover(embed, book_id)
            await interaction.response.edit_message(embed=embed, view=view)
            if removed:
                await interaction.followup.send(f"🌷 Removed **{removed}**.", ephemeral=True)
            return

        book, updates = await apply_book_edits(view.owner_id, view.selected_key, status=action)
        await view.reload_from_db()
        embed, book_id = view.get_embed()
        await view.apply_library_cover(embed, book_id)
        await interaction.response.edit_message(embed=embed, view=view)
        if book:
            await interaction.followup.send(
                f"✨ Updated **{book['title']}**: {', '.join(updates)}",
                ephemeral=True,
            )


class LibraryEditModal(discord.ui.Modal, title="Edit book details"):
    rating = discord.ui.TextInput(
        label="Rating (1–5, optional)",
        required=False,
        max_length=1,
        placeholder="4",
    )
    year = discord.ui.TextInput(
        label="Year finished (optional)",
        required=False,
        max_length=4,
        placeholder="2026",
    )
    pages = discord.ui.TextInput(
        label="Total pages (optional)",
        required=False,
        max_length=5,
        placeholder="320",
    )

    def __init__(self, library_view, identifier, title, source_message):
        super().__init__()
        self.library_view = library_view
        self.identifier = identifier
        self.book_title = title
        self.source_message = source_message

    async def on_submit(self, interaction: discord.Interaction):
        rating = year = total_pages = None
        try:
            if str(self.rating.value).strip():
                rating = int(str(self.rating.value).strip())
                if rating < 1 or rating > 5:
                    raise ValueError("rating")
            if str(self.year.value).strip():
                year = int(str(self.year.value).strip())
                if year < 2000 or year > current_year():
                    raise ValueError("year")
            if str(self.pages.value).strip():
                total_pages = int(str(self.pages.value).strip())
                if total_pages < 1:
                    raise ValueError("pages")
        except ValueError:
            await interaction.response.send_message(
                f"🌷 Check the values: rating 1–5, year 2000–{current_year()}, pages at least 1.",
                ephemeral=True,
            )
            return

        if rating is None and year is None and total_pages is None:
            await interaction.response.send_message("🌷 Fill in at least one field.", ephemeral=True)
            return

        book, updates = await apply_book_edits(
            self.library_view.owner_id, self.identifier,
            rating=rating, year=year, total_pages=total_pages,
        )
        await self.library_view.reload_from_db()
        embed, book_id = self.library_view.get_embed()
        await self.library_view.apply_library_cover(embed, book_id)
        if self.source_message:
            try:
                await self.source_message.edit(embed=embed, view=self.library_view)
            except (discord.HTTPException, discord.NotFound):
                pass
        label = book["title"] if book else self.book_title
        await interaction.response.send_message(
            f"✨ Updated **{label}**: {', '.join(updates) if updates else 'saved'}",
            ephemeral=True,
        )


# 4. RATINGS & REVIEWS
class RateModal(discord.ui.Modal, title="Rate Your Read 💕"):
    review_text = discord.ui.TextInput(
        label="Share your thoughts (optional)",
        style=discord.TextStyle.paragraph,
        placeholder="What did you love? What would you tell a friend...",
        required=False,
        max_length=500
    )

    def __init__(self, book_id, title, rating, source_message=None):
        super().__init__()
        self.book_id = book_id
        self.title = title
        self.rating = rating
        self.source_message = source_message

    async def on_submit(self, interaction: discord.Interaction):
        try:
            user_id = str(interaction.user.id)
            user_profile = await users_col.find_one({"_id": user_id})
            
            if user_profile:
                bookshelf = user_profile["bookshelf"]
                for book in bookshelf:
                    if book["book_id"] == self.book_id:
                        book["status"] = "completed"
                        book["rating"] = int(self.rating)
                        book["review"] = self.review_text.value
                        break
                
                history = user_profile.get("history", [])
                history.append(f"🏆 Finished and rated **{self.title}** with {self.rating} stars on {datetime.now().strftime('%d/%m/%Y')}")
                
                await users_col.update_one({"_id": user_id}, {"$set": {"bookshelf": bookshelf, "history": history}})
            
            stars = cozy_stars(self.rating, "")
            await interaction.response.send_message(
                f"✨ Saved! You rated **{self.title}** {stars}",
                ephemeral=True,
            )
            if self.source_message:
                try:
                    await self.source_message.edit(view=None)
                except (discord.HTTPException, discord.NotFound):
                    pass
        except Exception:
            await interaction.response.send_message("🌷 Something went wrong saving your review.", ephemeral=True)

class RatingDropdown(discord.ui.Select):
    def __init__(self, book_id, title, owner_id=None):
        self.book_id = book_id
        self.title = title
        self.owner_id = str(owner_id) if owner_id else None
        options = [
            discord.SelectOption(label="💛💛💛💛💛 absolute masterpiece", value="5"),
            discord.SelectOption(label="💛💛💛💛 really loved it", value="4"),
            discord.SelectOption(label="💛💛💛 it was okay", value="3"),
            discord.SelectOption(label="💛💛 not for me", value="2"),
            discord.SelectOption(label="💛 didn't enjoy it", value="1"),
        ]
        super().__init__(placeholder="How much did you love it? 💕", options=options)

    async def callback(self, interaction: discord.Interaction):
        if self.owner_id and str(interaction.user.id) != self.owner_id:
            await interaction.response.send_message("🌷 This rating is for someone else.", ephemeral=True)
            return
        await interaction.response.send_modal(
            RateModal(self.book_id, self.title, self.values[0], source_message=interaction.message)
        )

class RatingView(discord.ui.View):
    def __init__(self, book_id, title, owner_id=None):
        super().__init__(timeout=120)
        self.add_item(RatingDropdown(book_id, title, owner_id=owner_id))


# 5. BOOKSHELF CORE BUTTONS
class BookshelfButtons(discord.ui.View):
    def __init__(self, book_id, title, total_pages, categories=None, search_items=None, search_query=""):
        super().__init__(timeout=180)
        self.book_id = book_id
        self.title = title
        self.total_pages = int(total_pages) if total_pages else 0
        self.categories = categories or []
        self.genres = list(normalize_genres(self.categories))
        self.subgenres = list(normalize_subgenres(self.categories))
        self.read_year = current_year()
        self.search_items = search_items or []
        self.search_query = search_query
        for child in self.children:
            if isinstance(child, discord.ui.Button) and child.label == "Back" and not self.search_items:
                child.label = "Cancel"

    async def update_bookshelf(self, user_id, username, status):
        user_profile = await users_col.find_one({"_id": str(user_id)})
        if not user_profile:
            user_profile = {"_id": str(user_id), "username": username, "yearly_goal": 0, "bookshelf": [], "history": [], "last_read": datetime.now()}
            await users_col.insert_one(user_profile)

        bookshelf = user_profile.get("bookshelf", [])
        history = user_profile.get("history", [])
        book_exists = False
        
        for book in bookshelf:
            if book["book_id"] == self.book_id:
                book["status"] = status
                if self.categories and not book.get("categories"):
                    book["categories"] = self.categories
                    book["genres"] = self.genres
                    book["subgenres"] = self.subgenres
                if status == "completed":
                    stamp_completed(book, self.read_year)
                book_exists = True
                break
        
        if not book_exists:
            new_book = {
                "book_id": self.book_id, "title": self.title, "status": status,
                "current_page": 0, "total_pages": self.total_pages, "rating": None, "review": None,
                "categories": self.categories, "genres": self.genres, "subgenres": self.subgenres,
            }
            if status == "completed":
                stamp_completed(new_book, self.read_year)
            bookshelf.append(new_book)
            
        emoji_map = {"to_read": "💌 Wishlist:", "reading": "💭 Started:", "completed": "✨ Finished:"}
        history.append(f"{emoji_map.get(status, '▫️')} Moved **{self.title}** to {status.replace('_', ' ')} on {datetime.now().strftime('%d/%m/%Y')}")

        await users_col.update_one(
            {"_id": str(user_id)},
            {"$set": {"bookshelf": bookshelf, "history": history, **activity_fields(user_profile)}},
        )

    async def _apply_status(self, interaction: discord.Interaction, status):
        await interaction.response.defer()
        await self.update_bookshelf(interaction.user.id, interaction.user.name, status)

        rating_view = None
        if status == "completed":
            user_profile = await users_col.find_one({"_id": str(interaction.user.id)})
            book = next((b for b in user_profile["bookshelf"] if b["book_id"] == self.book_id), None)
            if book:
                if is_current_year_finish(book):
                    await mark_buddy_read_finish(interaction.user.id, book)
                await users_col.update_one(
                    {"_id": str(interaction.user.id)},
                    {"$set": {"bookshelf": user_profile["bookshelf"]}},
                )
                if is_current_year_finish(book):
                    completed_books = [b for b in user_profile["bookshelf"] if b["status"] == "completed"]
                    await announce_book_finished(bot, interaction.user, book, completed_books, interaction.user.id)
            rating_view = RatingView(
                self.book_id, self.title, owner_id=str(interaction.user.id)
            )

        embed = build_shelf_action_embed(self.title, status, interaction.user.display_name)
        posted = False
        if interaction.channel is not None:
            try:
                await interaction.channel.send(embed=embed, view=rating_view)
                posted = True
            except discord.HTTPException as e:
                print(f"shelf confirmation send error: {e}")
        if posted:
            try:
                await interaction.delete_original_response()
            except (discord.HTTPException, discord.NotFound):
                pass
            return

        await interaction.edit_original_response(embed=embed, view=rating_view)

    @discord.ui.button(label="Wishlist", style=discord.ButtonStyle.blurple, emoji="💌")
    async def to_read(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_status(interaction, "to_read")

    @discord.ui.button(label="Reading Now", style=discord.ButtonStyle.success, emoji="💭")
    async def reading(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_status(interaction, "reading")

    @discord.ui.button(label="Finished", style=discord.ButtonStyle.secondary, emoji="✨")
    async def completed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._apply_status(interaction, "completed")

    @discord.ui.button(label="Back", style=discord.ButtonStyle.secondary, row=1)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.search_items:
            embed = build_search_results_embed(self.search_items, self.search_query)
            await interaction.response.edit_message(
                embed=embed,
                view=SearchResultsView(self.search_items, self.search_query),
            )
            return
        await interaction.response.defer()
        await interaction.delete_original_response()


class SearchResultSelect(discord.ui.Select):
    def __init__(self, items, query=""):
        self.items = items
        self.query = query
        self.items_by_id = {item["id"]: item for item in items}
        options = []
        for item in items:
            volume_info = item["volumeInfo"]
            title = volume_info.get("title", "Unknown Title")
            authors = ", ".join(volume_info.get("authors", ["Unknown Author"]))
            options.append(
                discord.SelectOption(
                    label=title[:100],
                    description=authors[:100] or None,
                    value=item["id"],
                )
            )
        super().__init__(placeholder="Pick a book 🌸", options=options, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction):
        try:
            item = self.items_by_id[self.values[0]]
            volume_info = item["volumeInfo"]
            book_id = item["id"]
            book_title = volume_info.get("title", "Unknown Title")
            pages = volume_info.get("pageCount", 0)
            categories = volume_info.get("categories", [])
            embed = build_book_embed(volume_info)
            await interaction.response.edit_message(
                embed=embed,
                view=BookshelfButtons(
                    book_id, book_title, pages, categories,
                    search_items=self.items, search_query=self.query,
                ),
            )
        except Exception as e:
            print(f"SearchResultSelect error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "🌷 Could not load this book — try `/search` again.",
                    ephemeral=True,
                )


class SearchResultsView(discord.ui.View):
    def __init__(self, items, query=""):
        super().__init__(timeout=180)
        self.items = items
        self.query = query
        self.add_item(SearchResultSelect(items, query))

    @discord.ui.button(label="Cancel", style=discord.ButtonStyle.secondary, row=1)
    async def cancel(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer()
        await interaction.delete_original_response()

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# 6. BUDDY READ JOIN BUTTON
def group_id_from_message(message):
    if not message or not message.embeds:
        return None
    footer = message.embeds[0].footer
    if footer and footer.text and footer.text.startswith("id:"):
        return footer.text[3:].strip()
    return None


class BuddyJoinView(discord.ui.View):
    def __init__(self):
        super().__init__(timeout=None)

    @discord.ui.button(
        label="Join Book Club",
        style=discord.ButtonStyle.success,
        emoji="💕",
        custom_id="buddyread:join",
    )
    async def join_group(self, interaction: discord.Interaction, button: discord.ui.Button):
        match_id = group_id_from_message(interaction.message)
        if not match_id:
            await interaction.response.send_message("🌷 Couldn't find this book club.", ephemeral=True)
            return
        message, ephemeral = await join_buddy_group(match_id, interaction.user)
        await interaction.response.send_message(message, ephemeral=ephemeral)


# 7. AUTOCOMPLETE FUNCTIONS
async def book_autocomplete(interaction: discord.Interaction, current: str):
    try:
        user_id = str(interaction.user.id)
        user_profile = await users_col.find_one({"_id": user_id})
        if not user_profile:
            user_profile = await users_col.find_one({"_id": interaction.user.id})
        if not user_profile or not user_profile.get("bookshelf"):
            return []

        status_labels = {
            "reading": "Reading now",
            "completed": "Finished",
            "to_read": "Wishlist",
            "abandoned": "Paused",
        }
        current_lower = current.lower()
        choices = []
        for book in user_profile["bookshelf"]:
            title = book.get("title") or "Unknown Title"
            if current_lower and current_lower not in title.lower():
                continue
            book_id = book.get("book_id") or title
            status = status_labels.get(book.get("status"), "Unknown")
            name = f"{title[:70]} ({status})"[:100]
            value = book_id[:100]
            choices.append(app_commands.Choice(name=name, value=value))
            if len(choices) >= 25:
                break
        return choices
    except Exception as e:
        print(f"book_autocomplete error: {e}")
        return []


async def progress_book_autocomplete(interaction: discord.Interaction, current: str):
    try:
        choices = await book_autocomplete(interaction, current)
        filtered = []
        for choice in choices:
            name_lower = choice.name.lower()
            if "(finished)" in name_lower:
                continue
            filtered.append(choice)
        return filtered
    except Exception as e:
        print(f"progress_book_autocomplete error: {e}")
        return []


def find_shelf_book(bookshelf, identifier):
    if not identifier:
        return None
    for book in bookshelf:
        if book.get("book_id") == identifier:
            return book
        if book.get("title", "").lower() == identifier.lower():
            return book
    return None


SHELF_STATUS_LABELS = {
    "reading": "reading now",
    "completed": "finished",
    "to_read": "wishlist",
    "abandoned": "paused",
}


async def apply_book_edits(user_id, identifier, rating=None, year=None, total_pages=None, status=None):
    user_profile = await users_col.find_one({"_id": str(user_id)})
    if not user_profile or not user_profile.get("bookshelf"):
        return None, "empty"
    book = find_shelf_book(user_profile["bookshelf"], identifier)
    if not book:
        return None, "missing"

    updates = []
    if rating is not None:
        book["rating"] = rating
        updates.append(f"rating → {cozy_stars(rating, '')}")
    if year is not None:
        book["completed_year"] = year
        updates.append(f"year → {year}")
    if total_pages is not None:
        book["total_pages"] = total_pages
        updates.append(f"pages → {total_pages}")
    if status is not None:
        book["status"] = status
        updates.append(f"status → {SHELF_STATUS_LABELS.get(status, status)}")
        if status == "completed" and not book.get("completed_year"):
            stamp_completed(book, year)
        elif status == "completed":
            if year is not None:
                book["completed_year"] = year
            if not book.get("completed_at"):
                book["completed_at"] = datetime.now().isoformat()
        if status == "completed" and book.get("total_pages", 0) > 0:
            book["current_page"] = book["total_pages"]

    await users_col.update_one({"_id": str(user_id)}, {"$set": {"bookshelf": user_profile["bookshelf"]}})
    return book, updates


async def remove_shelf_book(user_id, identifier):
    user_profile = await users_col.find_one({"_id": str(user_id)})
    if not user_profile or not user_profile.get("bookshelf"):
        return None
    bookshelf = user_profile["bookshelf"]
    book = find_shelf_book(bookshelf, identifier)
    if not book:
        return None
    book_id = book.get("book_id")
    removed_title = book["title"]
    if book_id:
        updated = [b for b in bookshelf if b.get("book_id") != book_id]
    else:
        updated = [b for b in bookshelf if b["title"].lower() != removed_title.lower()]
    await users_col.update_one({"_id": str(user_id)}, {"$set": {"bookshelf": updated}})
    return removed_title


def build_history_paginator(display_name, entries):
    timeline = [f"˚ {entry}" for entry in reversed(entries)]
    return PaginatorView(
        title=f"📜 {display_name}'s Reading Diary",
        data_list=timeline,
        color=COLORS["profile"],
        items_per_page=10,
    )


async def buddy_autocomplete(interaction: discord.Interaction, current: str):
    try:
        cursor = buddies_col.find()
        groups = await cursor.to_list(length=25)
        current_lower = current.lower()
        choices = []
        for group in groups:
            if group.get("closed"):
                continue
            title = group.get("book_title", "Unknown")
            if current_lower and current_lower not in title.lower():
                continue
            host = group.get("host_name", "Unknown")
            name = f"{title[:70]} (Host: {host})"[:100]
            value = str(group["_id"])[:100]
            choices.append(app_commands.Choice(name=name, value=value))
            if len(choices) >= 25:
                break
        return choices
    except Exception as e:
        print(f"buddy_autocomplete error: {e}")
        return []


async def ensure_group_book_on_shelf(user, group):
    user_id = str(user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    now = datetime.now()
    if not user_profile:
        user_profile = {
            "_id": user_id,
            "username": user.name,
            "yearly_goal": 0,
            "bookshelf": [],
            "history": [],
            "last_read": now,
            "streak": 0,
        }
        await users_col.insert_one(user_profile)

    bookshelf = user_profile.get("bookshelf", [])
    history = user_profile.get("history", [])
    existing = find_group_book(bookshelf, group)
    if existing:
        if existing.get("status") == "to_read":
            existing["status"] = "reading"
            history.append(f"💭 Started **{existing['title']}** with the book club")
            await users_col.update_one(
                {"_id": user_id},
                {"$set": {"bookshelf": bookshelf, "history": history, **activity_fields(user_profile, now)}},
            )
        return existing.get("title")

    title = group.get("book_title", "Unknown Title")
    book_id = group.get("book_id") or title
    total_pages = 0
    host = await users_col.find_one({"_id": group.get("host_id")}) if group.get("host_id") else None
    if host:
        host_book = find_group_book(host.get("bookshelf", []), group)
        if host_book:
            total_pages = host_book.get("total_pages", 0) or 0
            book_id = host_book.get("book_id") or book_id
            title = host_book.get("title") or title

    bookshelf.append({
        "book_id": book_id,
        "title": title,
        "status": "reading",
        "current_page": 0,
        "total_pages": total_pages,
        "rating": None,
        "review": None,
        "categories": [],
        "genres": [],
        "subgenres": [],
    })
    history.append(f"👯‍♀️ Joined the book club for **{title}**")
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"bookshelf": bookshelf, "history": history, **activity_fields(user_profile, now)}},
    )
    return title


async def join_buddy_group(group_id, user):
    user_id = str(user.id)
    group = await buddies_col.find_one({"_id": group_id})

    if not group:
        return "🌷 Reading group not found.", True

    if user_id in group["members"]:
        return "💕 You're already in this book club!", True

    await buddies_col.update_one({"_id": group_id}, {"$push": {"members": user_id}})
    await ensure_group_book_on_shelf(user, group)
    return (
        f"👯‍♀️ **{user.display_name}** joined **{group['book_title']}** — "
        "it's on your shelf as Reading now!",
        False,
    )


async def find_active_group_for_book(user_id, book):
    cursor = buddies_col.find({"members": str(user_id), "closed": {"$ne": True}})
    groups = await cursor.to_list(length=50)
    for group in groups:
        if find_group_book([book], group):
            return group
        group_title = (group.get("book_title") or "").lower()
        book_title = (book.get("title") or "").lower()
        if group_title and book_title and (group_title in book_title or book_title in group_title):
            return group
        if group.get("book_id") and group.get("book_id") == book.get("book_id"):
            return group
    return None


async def announce_club_progress(interaction, book, percentage, page, total):
    group = await find_active_group_for_book(interaction.user.id, book)
    if not group:
        return
    channel = await get_configured_channel(bot, BOOKCLUB_CHANNEL_ID)
    if channel is None:
        return
    if interaction.channel_id and int(interaction.channel_id) == int(channel.id):
        return
    bar = progress_bar(page, total)
    embed = discord.Embed(
        title="💭 Book club progress",
        description=(
            f"**{interaction.user.display_name}** on **{book['title']}**\n"
            f"`{bar}` **{percentage}%** · page {page}/{total if total else '?'}"
        ),
        color=COLORS["social"],
    )
    apply_cozy_style(embed)
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"announce_club_progress error: {e}")


_commands_synced = False


@bot.event
async def on_ready():
    global _commands_synced
    print(f"🚀 Bot {bot.user} está online e pronto!")

    if _commands_synced:
        return
    _commands_synced = True

    try:
        synced = await bot.tree.sync()
        print(f"✅ Synced {len(synced)} commands globally (DMs + servers).")

        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.clear_commands(guild=guild)
            await bot.tree.sync(guild=guild)
            print(f"✅ Removed server copies so /commands are not duplicated in {GUILD_ID}.")
    except Exception as e:
        print(f"❌ Erro na sincronização: {e}")



# 8. ALL COMMANDS

@bot.tree.command(name="help", description="List all BuddyRead commands")
@app_commands.describe(mode="Quick start or full command guide")
@app_commands.choices(mode=[
    app_commands.Choice(name="Quick Start", value="quick"),
    app_commands.Choice(name="Full Guide", value="full"),
])
async def help_command(interaction: discord.Interaction, mode: app_commands.Choice[str] = None):
    mode_value = mode.value if mode else "quick"
    if mode_value == "quick":
        embed = discord.Embed(
            title="🎀 BuddyRead — Quick Start",
            description=(
                "Your cozy corner for tracking reads, book club & cute stats ✨\n"
                "**Note:** `/buddyread` was renamed to `/bookclub`."
            ),
            color=COLORS["reading"],
        )
        apply_cozy_style(embed)
        embed.add_field(
            name="Most Loved Commands",
            value=(
                "`/search` — Find a book (private) 🌸\n"
                "`/progress` — Log pages 💭\n"
                "`/library` — Browse and manage your shelf 🎀\n"
                "`/profile` — Stats, diary & yearly challenge ✨\n"
                "`/bookclub create` — Start a group read 👯‍♀️\n"
                "`/leaderboard` — Server ranking 🏆"
            ),
            inline=False,
        )
        embed.set_footer(text="Use /help mode:Full Guide for every command 💕")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="🎀 BuddyRead — Command Guide",
        description=(
            "Use commands in **server channels** or **DMs** ✨\n"
            "Finishing a book this year posts a celebration in the reading channel 💕\n"
            "**Note:** `/buddyread` was renamed to `/bookclub`."
        ),
        color=COLORS["reading"],
    )
    apply_cozy_style(embed)
    embed.add_field(
        name="📚 Library",
        value=(
            "`/search` — Private search; pick a book, then Wishlist / Reading / Finished\n"
            "`/library [member]` — Browse shelves; on yours, pick a book to edit or remove"
        ),
        inline=False,
    )
    embed.add_field(
        name="💭 Reading",
        value=(
            "`/progress` — Log **page**, **add_pages**, or set **status**\n"
            "`/profile [member]` — Stats & badges · **Diary** and **Challenge** buttons"
        ),
        inline=False,
    )
    embed.add_field(
        name="👯‍♀️ Social",
        value=(
            "`/bookclub create` — Start a group; tick **of_the_month** to post in the book club channel\n"
            "`/bookclub status` — Progress, plus Join / Repost / Delete buttons\n"
            "`/leaderboard` — Server ranking (this year or all time)"
        ),
        inline=False,
    )
    embed.add_field(name="🎀 Achievements", value=build_achievements_help(), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="search", description="Search privately and add a book to your shelf")
@app_commands.describe(title="Book title or ISBN", author="Optional author name to narrow results")
async def search(interaction: discord.Interaction, title: str, author: str = None):
    await interaction.response.defer(ephemeral=True)
    api_url = "https://www.googleapis.com/books/v1/volumes"
    query = f"intitle:{title}+inauthor:{author}" if author else title
    params = {"q": query, "key": GOOGLE_BOOKS_KEY, "maxResults": 10}
    try:
        session = await get_http_session()
        async with session.get(api_url, params=params) as response:
            if response.status != 200:
                await interaction.followup.send("🌷 Couldn't reach the book API right now — try again soon!")
                return
            data = await response.json()
            if "items" not in data:
                await interaction.followup.send("🌷 No books found with that title — try another search!")
                return

            items = data["items"][:10]
            if len(items) == 1:
                volume_info = items[0]["volumeInfo"]
                book_id = items[0]["id"]
                book_title = volume_info.get("title", "Unknown Title")
                pages = volume_info.get("pageCount", 0)
                categories = volume_info.get("categories", [])
                embed = build_book_embed(volume_info)
                await interaction.followup.send(
                    embed=embed,
                    view=BookshelfButtons(book_id, book_title, pages, categories),
                )
            else:
                embed = build_search_results_embed(items, title)
                await interaction.followup.send(
                    embed=embed, view=SearchResultsView(items, title)
                )
    except Exception:
        await interaction.followup.send("🌷 Something went wrong while searching — please try again.")


@bot.tree.command(name="progress", description="Log pages read or update reading status for a book")
@app_commands.autocomplete(book_title=progress_book_autocomplete)
@app_commands.describe(
    page="Current page number (absolute)",
    add_pages="Pages read since last update (relative)",
    status="Update status without logging pages",
    year="Year finished (only when marking as Finished)",
)
@app_commands.choices(status=[
    app_commands.Choice(name="In Progress", value="reading"),
    app_commands.Choice(name="Finished", value="completed"),
    app_commands.Choice(name="Abandoned", value="abandoned"),
])
async def progress(
    interaction: discord.Interaction,
    book_title: str,
    page: int = None,
    add_pages: int = None,
    status: app_commands.Choice[str] = None,
    year: int = None,
):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})

    if not user_profile or not user_profile.get("bookshelf"):
        await interaction.response.send_message(
            "🌷 Your shelf is empty — use `/search` to add your first book!",
            ephemeral=True,
        )
        return

    current_book = find_shelf_book(user_profile["bookshelf"], book_title)
    if not current_book:
        await interaction.response.send_message("🌷 That book isn't on your shelf.", ephemeral=True)
        return

    now = datetime.now()
    history = user_profile.get("history", [])

    if page is not None and add_pages is not None:
        await interaction.response.send_message("🌷 Use either **page** or **add_pages**, not both.", ephemeral=True)
        return

    if add_pages is not None:
        if add_pages <= 0:
            await interaction.response.send_message("🌷 **add_pages** must be greater than 0.", ephemeral=True)
            return
        page = current_book.get("current_page", 0) + add_pages

    if page is not None:
        if current_book["status"] == "completed":
            await interaction.response.send_message("🌷 This book is already marked as finished ✨", ephemeral=True)
            return

        if current_book["status"] != "reading":
            previous_status = current_book["status"]
            current_book["status"] = "reading"
            if previous_status == "to_read":
                history.append(f"💭 Started reading **{current_book['title']}**")
            elif previous_status == "abandoned":
                history.append(f"💭 Resumed reading **{current_book['title']}**")

        total_pages = current_book["total_pages"]
        if page < 0 or (total_pages > 0 and page > total_pages):
            await interaction.response.send_message("🌷 That page number doesn't look right!", ephemeral=True)
            return

        history.append(f"📈 Logged page **{page}** on *{current_book['title']}*")
        current_book["current_page"] = page
        percentage = round((page / total_pages) * 100) if total_pages > 0 else 100

        if total_pages > 0 and page == total_pages:
            await interaction.response.defer(ephemeral=True)
            current_book["status"] = "completed"
            stamp_completed(current_book)
            if is_current_year_finish(current_book):
                await mark_buddy_read_finish(user_id, current_book)
            await users_col.update_one(
                {"_id": user_id},
                {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, **activity_fields(user_profile, now)}},
            )
            await announce_club_progress(interaction, current_book, 100, page, total_pages)
            if is_current_year_finish(current_book):
                completed_books = [b for b in user_profile["bookshelf"] if b["status"] == "completed"]
                await announce_book_finished(bot, interaction.user, current_book, completed_books, user_id)
            view = RatingView(current_book["book_id"], current_book["title"], owner_id=user_id)
            await interaction.followup.send(
                f"✨ You finished *{current_book['title']}*! Rate it below 💕",
                view=view,
                ephemeral=True,
            )
        else:
            await users_col.update_one(
                {"_id": user_id},
                {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, **activity_fields(user_profile, now)}},
            )
            progress_bar_display = progress_bar(page, total_pages)
            await interaction.response.send_message(
                f"💭 **{interaction.user.display_name}** is making progress on *{current_book['title']}*!\n"
                f"> `{progress_bar_display}` **{percentage}%** · page {page}/{total_pages if total_pages > 0 else '?'}"
            )
            await announce_club_progress(interaction, current_book, percentage, page, total_pages)
        return

    if status is None and page is None and add_pages is None:
        await interaction.response.send_message("🌷 Provide a **page**, **add_pages**, or a **status**.", ephemeral=True)
        return

    status_value = status.value
    if status_value == "completed":
        if year is not None and (year < 2000 or year > current_year()):
            await interaction.response.send_message(f"🌷 Year must be between **2000** and **{current_year()}**.", ephemeral=True)
            return
        current_book["completed_year"] = year or current_year()
        stamp_completed(current_book, current_book["completed_year"])

    status_messages = {
        "reading": ("💭 Reading now", f"💭 **{current_book['title']}** is now *in progress*"),
        "completed": ("✨ Finished", f"✨ **{current_book['title']}** is marked *finished*"),
        "abandoned": ("🍂 Paused", f"🍂 **{current_book['title']}** is paused for now"),
    }

    current_book["status"] = status_value
    if status_value == "completed" and current_book["total_pages"] > 0:
        current_book["current_page"] = current_book["total_pages"]

    label, history_entry = status_messages[status_value]
    history.append(f"{history_entry} on {now.strftime('%d/%m/%Y')}")

    if status_value == "completed":
        await interaction.response.defer(ephemeral=True)
        if is_current_year_finish(current_book):
            await mark_buddy_read_finish(user_id, current_book)

        await users_col.update_one(
            {"_id": user_id},
            {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, **activity_fields(user_profile, now)}},
        )
        if status_value == "completed":
            total = current_book.get("total_pages", 0) or 0
            page_now = current_book.get("current_page", 0) or 0
            pct = round((page_now / total) * 100) if total > 0 else 100
            await announce_club_progress(interaction, current_book, pct, page_now, total)

        if is_current_year_finish(current_book):
            completed_books = [b for b in user_profile["bookshelf"] if b["status"] == "completed"]
            await announce_book_finished(bot, interaction.user, current_book, completed_books, user_id)
        view = RatingView(current_book["book_id"], current_book["title"], owner_id=user_id)
        await interaction.followup.send(
            f"✨ You finished *{current_book['title']}*! Rate it below 💕",
            view=view,
            ephemeral=True,
        )
    else:
        await users_col.update_one(
            {"_id": user_id},
            {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, **activity_fields(user_profile, now)}},
        )
        await interaction.response.send_message(f"{label} — **{current_book['title']}** updated!", ephemeral=True)


@bot.tree.command(name="profile", description="View reading profile, stats, and achievements")
@app_commands.describe(member="Member to view")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    target_user = member or interaction.user
    user_id = str(target_user.id)
    user_profile = await users_col.find_one({"_id": user_id})

    if not user_profile:
        await interaction.response.send_message(
            f"🌷 {target_user.display_name} doesn't have a profile yet — start with `/search`!",
            ephemeral=True,
        )
        return

    bookshelf = user_profile.get("bookshelf", [])
    completed = [b for b in bookshelf if b["status"] == "completed"]
    reading = [b for b in bookshelf if b["status"] == "reading"]
    to_read = [b for b in bookshelf if b["status"] == "to_read"]
    year_completed = books_completed_in_year(bookshelf, current_year())

    embed = discord.Embed(
        title=f"🎀 {target_user.display_name}'s Reading Profile",
        description=f"✨ *your cozy reading story* ✨\n{COZY_DIVIDER}",
        color=COLORS["profile"],
    )
    apply_cozy_style(embed)
    embed.set_thumbnail(url=target_user.display_avatar.url)

    goal = user_profile.get("yearly_goal", 0)
    if goal > 0:
        challenge_year = current_year()
        percent = min(round((len(year_completed) / goal) * 100), 100)
        bar = progress_bar(len(year_completed), goal)
        embed.add_field(
            name=f"🏆 {challenge_year} Reading Challenge",
            value=f"`{bar}` **{len(year_completed)} / {goal}** books ({percent}%)",
            inline=False,
        )

    embed.add_field(name="💭 Reading", value=f"**{len(reading)}**", inline=True)
    embed.add_field(name="✨ Finished", value=f"**{len(completed)}**", inline=True)
    embed.add_field(name="💌 Wishlist", value=f"**{len(to_read)}**", inline=True)

    streak = display_streak(user_profile)
    if streak:
        embed.add_field(name="🔥 Streak", value=f"**{streak}** day{'s' if streak != 1 else ''}", inline=True)

    avg = average_rating(completed)
    if avg:
        embed.add_field(name="💛 Avg Rating", value=f"**{avg}** / 5", inline=True)
    top_genre = favorite_genre(completed)
    if top_genre:
        embed.add_field(name="📂 Fav Genre", value=top_genre, inline=True)
    if year_completed:
        embed.add_field(name="🌸 This Year", value=f"**{len(year_completed)}** finished", inline=True)

    if reading:
        reading_lines = []
        for book in reading[:5]:
            total = book.get("total_pages", 0) or 0
            current_page = book.get("current_page", 0) or 0
            pct = round((current_page / total) * 100) if total > 0 else 0
            reading_lines.append(
                f"**{book['title']}**\n`{progress_bar(current_page, total)}` **{pct}%**"
            )
        if len(reading) > 5:
            reading_lines.append(f"*+ {len(reading) - 5} more in `/library`*")
        embed.add_field(
            name="💭 Currently Reading",
            value="\n\n".join(reading_lines),
            inline=False,
        )
        first_cover = next((book.get("book_id") for book in reading if book.get("book_id")), None)
        if first_cover:
            thumbnail = await fetch_book_thumbnail(first_cover)
            if thumbnail:
                embed.set_image(url=thumbnail)

    if completed:
        last = max(completed, key=book_completed_sort_key)
        stars = cozy_stars(last.get("rating"))
        embed.add_field(name="🌷 Latest Finish", value=f"**{last['title']}** · {stars}", inline=False)

    reviewed = [b for b in completed if (b.get("review") or "").strip()]
    if reviewed:
        reviewed.sort(key=book_completed_sort_key, reverse=True)
        lines = []
        for book in reviewed[:3]:
            text = book["review"].strip()
            if len(text) > 140:
                text = text[:137] + "..."
            stars = cozy_stars(book.get("rating"), "")
            lines.append(f"**{book['title']}** {stars}\n*{text}*")
        embed.add_field(name="✍️ Recent reviews", value="\n\n".join(lines), inline=False)

    badges = get_profile_badges(completed)
    embed.add_field(
        name="🎀 Achievements",
        value="\n".join(badges) if badges else "*no badges yet — your first finish unlocks one!* 💕",
        inline=False,
    )
    embed.set_footer(text="keep reading, you're doing amazing ✨")
    await interaction.response.send_message(
        embed=embed,
        view=ProfileView(target_user.display_name, user_id, str(interaction.user.id)),
    )


class ChallengeModal(discord.ui.Modal, title="Yearly reading challenge"):
    books_goal = discord.ui.TextInput(
        label="How many books this year? (1–100)",
        placeholder="12",
        max_length=3,
        required=True,
    )

    async def on_submit(self, interaction: discord.Interaction):
        try:
            goal = int(str(self.books_goal.value).strip())
        except ValueError:
            await interaction.response.send_message("🌷 Please enter a number between **1** and **100**.", ephemeral=True)
            return
        if goal < 1 or goal > 100:
            await interaction.response.send_message("🌷 Please set a goal between **1** and **100** books.", ephemeral=True)
            return
        year = current_year()
        await users_col.update_one(
            {"_id": str(interaction.user.id)},
            {"$set": {"yearly_goal": goal, "username": interaction.user.name}},
            upsert=True,
        )
        await interaction.response.send_message(
            f"🎀 Your **{year}** reading challenge is set to **{goal}** books — you've got this! ✨",
            ephemeral=True,
        )


class ProfileView(discord.ui.View):
    def __init__(self, display_name, target_id, viewer_id):
        super().__init__(timeout=180)
        self.display_name = display_name
        self.target_id = str(target_id)
        self.viewer_id = str(viewer_id)
        if self.viewer_id != self.target_id:
            for child in self.children.copy():
                if isinstance(child, discord.ui.Button) and child.label == "Challenge":
                    self.remove_item(child)

    @discord.ui.button(label="Diary", emoji="📜", style=discord.ButtonStyle.secondary)
    async def show_diary(self, interaction: discord.Interaction, button: discord.ui.Button):
        profile = await users_col.find_one({"_id": self.target_id})
        entries = profile.get("history", []) if profile else []
        if not entries:
            await interaction.response.send_message(
                "🌷 No reading activity yet — start with `/search`!",
                ephemeral=True,
            )
            return
        paginator = build_history_paginator(self.display_name, entries)
        await interaction.response.send_message(embed=paginator.get_embed(), view=paginator, ephemeral=True)

    @discord.ui.button(label="Challenge", emoji="🏆", style=discord.ButtonStyle.secondary)
    async def set_challenge(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.target_id:
            await interaction.response.send_message("🌷 You can only set your own challenge.", ephemeral=True)
            return
        await interaction.response.send_modal(ChallengeModal())


# BOOK CLUB (/bookclub)
async def build_bookclub_status_embed(group):
    rows = []
    all_finished = True
    for member_id in group["members"]:
        member_profile = await users_col.find_one({"_id": member_id})
        name = member_profile.get("username", f"User {member_id}") if member_profile else f"User {member_id}"
        page_info = "Not started yet"
        if member_profile and member_profile.get("bookshelf"):
            match_book = find_group_book(member_profile["bookshelf"], group)
            if match_book:
                if match_book["status"] == "completed":
                    page_info = "✨ Finished"
                elif match_book["status"] == "abandoned":
                    page_info = "🍂 Paused"
                    all_finished = False
                elif match_book["status"] == "reading":
                    all_finished = False
                    total = match_book.get("total_pages", 0) or 0
                    current = match_book.get("current_page", 0) or 0
                    pct = round((current / total) * 100) if total > 0 else 0
                    bar = progress_bar(current, total)
                    page_info = f"`{bar}` **{pct}%** · p.{current}/{total if total > 0 else '?'}"
                else:
                    all_finished = False
                    page_info = "💌 On wishlist"
            else:
                all_finished = False
        rows.append(f"🌸 **{name}** · {page_info}")

    if all_finished and len(group["members"]) > 0:
        await buddies_col.update_one({"_id": group["_id"]}, {"$set": {"closed": True}})

    embed = discord.Embed(
        title="👯‍♀️ Book Club Progress",
        description=f"**{group['book_title']}**\n{COZY_DIVIDER}\n" + "\n".join(rows),
        color=COLORS["social"],
    )
    apply_cozy_style(embed)
    embed.set_footer(text=f"{len(group['members'])} readers in this club 💕")
    thumbnail = group.get("thumbnail_url") or await fetch_book_thumbnail(group.get("book_id"))
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed


class BookClubStatusView(discord.ui.View):
    def __init__(self, group_id, host_id):
        super().__init__(timeout=180)
        self.group_id = group_id
        self.host_id = str(host_id)

    @discord.ui.button(label="Join", emoji="💕", style=discord.ButtonStyle.success)
    async def join_group(self, interaction: discord.Interaction, button: discord.ui.Button):
        message, ephemeral = await join_buddy_group(self.group_id, interaction.user)
        await interaction.response.send_message(message, ephemeral=ephemeral)

    @discord.ui.button(label="Repost invite", style=discord.ButtonStyle.secondary)
    async def repost(self, interaction: discord.Interaction, button: discord.ui.Button):
        group = await buddies_col.find_one({"_id": self.group_id})
        if not group:
            await interaction.response.send_message("🌷 Reading group not found.", ephemeral=True)
            return
        group = await hydrate_legacy_bookclub_group(group)
        posted = await post_bookclub_invite(bot, group, reminder=True)
        if not posted:
            await interaction.response.send_message(
                "🌷 Couldn't post in the book club channel — check `BOOKCLUB_CHANNEL_ID`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"✨ Posted **{group['book_title']}** in the book club channel!",
            ephemeral=True,
        )

    @discord.ui.button(label="Delete", style=discord.ButtonStyle.danger)
    async def delete_group(self, interaction: discord.Interaction, button: discord.ui.Button):
        if str(interaction.user.id) != self.host_id:
            await interaction.response.send_message("🌷 Only the host can delete this book club.", ephemeral=True)
            return
        group = await buddies_col.find_one({"_id": self.group_id})
        if not group:
            await interaction.response.send_message("🌷 Reading group not found.", ephemeral=True)
            return
        await buddies_col.delete_one({"_id": self.group_id})
        await interaction.response.send_message(
            f"🌷 Deleted the book club for **{group.get('book_title', 'Unknown Title')}**.",
            ephemeral=True,
        )


buddy_group = app_commands.Group(name="bookclub", description="Create and track shared reading groups")

@buddy_group.command(name="create", description="Start a group read — optionally post as Book of the Month")
@app_commands.autocomplete(book_title=book_autocomplete)
@app_commands.describe(
    book_title="Book to read together",
    of_the_month="Post it in the book club channel as Book of the Month",
)
async def buddy_create(interaction: discord.Interaction, book_title: str, of_the_month: bool = False):
    user_profile = await users_col.find_one({"_id": str(interaction.user.id)})
    shelf_book = find_shelf_book(user_profile.get("bookshelf", []), book_title) if user_profile else None
    resolved_title = shelf_book["title"] if shelf_book else book_title
    existing = await buddies_col.find_one({"book_title": {"$regex": f"^{resolved_title}$", "$options": "i"}, "closed": {"$ne": True}})
    if existing:
        if of_the_month:
            group = await hydrate_legacy_bookclub_group(existing)
            posted = await post_bookclub_invite(bot, group, reminder=True)
            if not posted:
                await interaction.response.send_message(
                    "🌷 A group already exists and I couldn't post in the book club channel.",
                    ephemeral=True,
                )
                return
            await interaction.response.send_message(
                f"✨ Reposted **{resolved_title}** — a group for this book already exists.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"🌷 A reading group for **{resolved_title}** already exists — use `/bookclub status` or the Join button.",
            ephemeral=True,
        )
        return

    resolved_book_id = shelf_book.get("book_id") if shelf_book else None
    resolved_thumbnail = await fetch_book_thumbnail(resolved_book_id)
    match_id = f"buddy_{interaction.user.id}_{int(datetime.now().timestamp())}"
    group_doc = {
        "_id": match_id,
        "book_title": resolved_title,
        "book_id": resolved_book_id,
        "thumbnail_url": resolved_thumbnail,
        "host_id": str(interaction.user.id),
        "host_name": interaction.user.display_name,
        "members": [str(interaction.user.id)],
    }
    await buddies_col.insert_one(group_doc)

    if of_the_month:
        posted = await post_bookclub_invite(bot, group_doc)
        if not posted:
            await interaction.response.send_message(
                "🌷 Group created, but I couldn't post in the book club channel — check `BOOKCLUB_CHANNEL_ID`.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"👯‍♀️ **Book of the Month** is **{resolved_title}** — posted in the book club channel! 💕",
            ephemeral=True,
        )
        return

    embed = build_bookclub_invite_embed(
        interaction.user.display_name, resolved_title, group_id=match_id,
    )
    if resolved_thumbnail:
        embed.set_thumbnail(url=resolved_thumbnail)
    await interaction.response.send_message(embed=embed, view=BuddyJoinView())

@buddy_group.command(name="status", description="Check progress — join, repost, or delete from here")
@app_commands.autocomplete(group_id=buddy_autocomplete)
async def buddy_status(interaction: discord.Interaction, group_id: str):
    group = await buddies_col.find_one({"_id": group_id})
    if not group:
        await interaction.response.send_message("🌷 Reading group not found.", ephemeral=True)
        return
    group = await hydrate_legacy_bookclub_group(group)

    await interaction.response.defer()
    embed = await build_bookclub_status_embed(group)
    view = BookClubStatusView(group["_id"], group.get("host_id"))
    await interaction.followup.send(embed=embed, view=view)

bot.tree.add_command(buddy_group)



@bot.tree.command(name="library", description="Browse your cozy personal book nook")
async def library(interaction: discord.Interaction, member: discord.Member = None):
    target_user = member or interaction.user
    user_id = str(target_user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    if not user_profile or not user_profile.get("bookshelf"):
        await interaction.response.send_message(
            f"🌷 {target_user.display_name}'s library is still empty — add your first book with `/search` 💕",
            ephemeral=True,
        )
        return
    bookshelf = user_profile["bookshelf"]
    view = LibraryView(
        target_user.display_name,
        bookshelf,
        owner_id=str(target_user.id),
        viewer_id=str(interaction.user.id),
    )
    embed, book_id = view.get_embed()
    await view.apply_library_cover(embed, book_id)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="leaderboard", description="See the top readers in this server")
@app_commands.describe(period="Count books finished this year or all time")
@app_commands.choices(period=[
    app_commands.Choice(name="This Year", value="year"),
    app_commands.Choice(name="All Time", value="all"),
])
async def leaderboard(interaction: discord.Interaction, period: app_commands.Choice[str] = None):
    await interaction.response.defer()
    if interaction.guild is None:
        await interaction.followup.send("🌷 The leaderboard only works inside a server!", ephemeral=True)
        return

    period_value = period.value if period else "year"
    cursor = users_col.find(
        {},
        {"_id": 1, "username": 1, "bookshelf.status": 1, "bookshelf.completed_year": 1},
    )
    all_users = await cursor.to_list(length=5000)
    leaderboard_data = []
    for user_data in all_users:
        user_id = user_data["_id"]
        if not await user_in_guild(interaction.guild, user_id):
            continue
        bookshelf = user_data.get("bookshelf", [])
        if period_value == "year":
            completed_count = len(books_completed_in_year(bookshelf, current_year()))
        else:
            completed_count = len([b for b in bookshelf if b["status"] == "completed"])
        if completed_count > 0:
            member = interaction.guild.get_member(int(user_id))
            display_name = member.display_name if member else user_data.get("username", "Unknown Reader")
            leaderboard_data.append({"username": display_name, "completed": completed_count})
    leaderboard_data = sorted(leaderboard_data, key=lambda x: x["completed"], reverse=True)
    if not leaderboard_data:
        await interaction.followup.send("🌷 The leaderboard is empty — be the first to finish a book! ✨")
        return
    medal_emojis = ["🥇", "🥈", "🥉"]
    period_label = "This Year" if period_value == "year" else "All Time"
    leaderboard_rows = []
    for rank, entry in enumerate(leaderboard_data):
        prefix = medal_emojis[rank] if rank < 3 else f"˚ {rank + 1}."
        leaderboard_rows.append(f"{prefix} **{entry['username']}** · **{entry['completed']}** books ✨")
    paginator = PaginatorView(
        title=f"🏆 {interaction.guild.name} — {period_label}",
        data_list=leaderboard_rows,
        color=COLORS["achievements"],
        items_per_page=10,
    )
    await interaction.followup.send(embed=paginator.get_embed(), view=paginator)


_last_weekly_key = None


@tasks.loop(minutes=30)
async def weekly_club_pulse():
    global _last_weekly_key
    now = datetime.now()
    if now.weekday() != 6 or now.hour < 18:
        return
    key = now.strftime("%Y-%W")
    if _last_weekly_key == key:
        return

    week_ago = now - timedelta(days=7)
    users = await users_col.find({}, {"username": 1, "bookshelf": 1}).to_list(length=5000)
    week_finishes = []
    for user_data in users:
        for book in user_data.get("bookshelf", []):
            if book.get("status") != "completed":
                continue
            finished = parse_dt(book.get("completed_at"))
            if finished and finished >= week_ago:
                week_finishes.append((user_data.get("username", "Reader"), book.get("title", "a book")))

    announce = await get_announce_channel(bot)
    if announce is not None:
        lines = [f"✨ **{name}** finished *{title}*" for name, title in week_finishes[:12]]
        desc = (
            "\n".join(lines)
            if lines
            else "No finishes logged this week — a cozy week to start something new."
        )
        embed = discord.Embed(
            title="📜 This week's reading recap",
            description=desc,
            color=COLORS["achievements"],
        )
        apply_cozy_style(embed)
        try:
            await announce.send(embed=embed)
        except Exception as e:
            print(f"weekly recap error: {e}")

    groups = await buddies_col.find({"closed": {"$ne": True}}).to_list(length=20)
    if groups:
        group = max(groups, key=lambda g: len(g.get("members", [])))
        group = await hydrate_legacy_bookclub_group(group)
        try:
            await post_bookclub_invite(bot, group, reminder=True)
        except Exception as e:
            print(f"weekly club reminder error: {e}")

    _last_weekly_key = key


@weekly_club_pulse.before_loop
async def before_weekly_club_pulse():
    await bot.wait_until_ready()


# 12. RUN THE BOT
async def main():
    if os.getenv("PORT"):
        await start_health_server()
    try:
        async with bot:
            await bot.start(DISCORD_TOKEN)
    finally:
        if http_session is not None and not http_session.closed:
            await http_session.close()


if __name__ == "__main__":
    asyncio.run(main())
