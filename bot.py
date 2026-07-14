import os
import asyncio
import discord
import certifi
from discord.ext import commands
import aiohttp
from aiohttp import web
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()

# 1. SETUP DISCORD BOT
intents = discord.Intents.default()
intents.message_content = True 
bot = commands.Bot(command_prefix="!", intents=intents)

# 2. SETUP CONNECTIONS
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
    "library": 0x4a90e2,
    "reading": 0x50e3c2,
    "social": 0x2ecc71,
    "achievements": 0xf5a623,
    "profile": 0x2b2d31,
}


def progress_bar(current, total, length=10):
    if total <= 0:
        return "⬛" * length
    percent = min(max(current / total, 0), 1)
    filled = round(percent * length)
    return "⬛" * filled + "⬜" * (length - filled)


def format_reading_book(book):
    total = book.get("total_pages", 0) or 0
    current = book.get("current_page", 0) or 0
    bar = progress_bar(current, total)
    pct = round((current / total) * 100) if total > 0 else 0
    return f"▪️ **{book['title']}**\n`{bar}` **{pct}%** · Page {current}/{total if total > 0 else '?'}"


def format_completed_book(book):
    rating = "⭐" * book["rating"] if book.get("rating") else "No Rating"
    year_bit = f", {book['completed_year']}" if book.get("completed_year") else ""
    return f"▪️ **{book['title']}** ({rating}{year_bit})"


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


async def post_public_review(bot_client, member, book, rating, review):
    channel = await get_announce_channel(bot_client)
    if channel is None:
        return
    stars = "⭐" * int(rating)
    embed = discord.Embed(
        title="⭐ New Review",
        description=f"**{member.display_name}** finished **{book['title']}** {stars}",
        color=COLORS["achievements"],
    )
    if review:
        embed.add_field(name="Review", value=f"> *{review[:500]}*", inline=False)
    thumbnail = await fetch_book_thumbnail(book.get("book_id"))
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    try:
        await channel.send(embed=embed)
    except Exception as e:
        print(f"post_public_review error: {e}")


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
    api_url = f"https://www.googleapis.com/books/v1/volumes/{book_id}?key={GOOGLE_BOOKS_KEY}"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    image_links = data.get("volumeInfo", {}).get("imageLinks", {})
                    thumbnail = image_links.get("thumbnail") or image_links.get("smallThumbnail") or ""
                    return thumbnail.replace("http://", "https://")
    except Exception:
        pass
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
        title=f"📖 {book_title}",
        description=f"**Author(s):** {authors}\n**Pages:** {pages if pages > 0 else '???'}\n\n*{description}*",
        color=0x4a90e2,
    )
    if categories:
        embed.add_field(name="Genre", value=categories[0], inline=True)
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    return embed


def build_search_results_embed(items, query):
    embed = discord.Embed(
        title="📚 Search Results",
        description=f"Found **{len(items)}** books matching **{query}**. Select one below:",
        color=0x4a90e2,
    )
    for i, item in enumerate(items[:5], 1):
        volume_info = item["volumeInfo"]
        title = volume_info.get("title", "Unknown Title")
        authors = ", ".join(volume_info.get("authors", ["Unknown Author"]))
        pages = volume_info.get("pageCount", 0)
        embed.add_field(
            name=f"{i}. {title[:80]}",
            value=f"{authors} · {pages if pages > 0 else '?'} pages",
            inline=False,
        )
    if len(items) > 5:
        embed.set_footer(text=f"Preview of 5 — all {len(items)} available in the dropdown.")
    return embed


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


def build_bookclub_invite_embed(host_name, book_title, reminder=False, member_count=1):
    title = "🔔 Book Club Reminder" if reminder else "📚 Book of the Month"
    description = (
        f"**{book_title}**\n"
        f"Click the button below to join this month's group read.\n"
        f"👥 **{member_count}** member{'s' if member_count != 1 else ''} joined so far."
    )
    return discord.Embed(title=title, description=description, color=COLORS["social"])


async def post_bookclub_invite(bot_client, group, reminder=False):
    channel = await get_configured_channel(bot_client, BOOKCLUB_CHANNEL_ID)
    if channel is None:
        return False

    embed = build_bookclub_invite_embed(
        group["host_name"], group["book_title"], reminder=reminder,
        member_count=len(group.get("members", [])),
    )
    thumbnail = group.get("thumbnail_url") or await fetch_book_thumbnail(group.get("book_id"))
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    await channel.send(embed=embed, view=BuddyJoinView(group["_id"]))
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


async def announce_book_finished(bot_client, member, book, completed_books, user_id):
    completed_count = len(completed_books)
    achievements = get_finish_achievements(completed_books, book)
    embed = discord.Embed(
        title="🏆 Book Finished!",
        description=f"**{member.display_name}** just finished reading **{book['title']}**!",
        color=0xe6a15c,
    )
    book_genres = get_book_genres(book)
    if book_genres:
        genre_text = ", ".join(GENRE_LABELS.get(g, g.title()) for g in book_genres)
        embed.add_field(name="📂 Genre", value=genre_text, inline=True)
    if achievements:
        embed.add_field(name="🎖️ Achievements Unlocked", value="\n".join(achievements), inline=False)
    embed.set_footer(text=f"📚 Total books finished: {completed_count}")
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
    def __init__(self, title, data_list, color=0x2b2d31, items_per_page=5):
        super().__init__(timeout=180)
        self.title = title
        self.data_list = data_list
        self.color = color
        self.items_per_page = items_per_page
        self.current_page = 0
        self.total_pages = max(1, (len(data_list) - 1) // items_per_page + 1)

    def get_embed(self):
        start = self.current_page * self.items_per_page
        end = start + self.items_per_page
        page_items = self.data_list[start:end]
        
        description = "\n".join(page_items) if page_items else "*No items found.*"
        embed = discord.Embed(title=self.title, description=description, color=self.color)
        embed.set_footer(text=f"Page {self.current_page + 1} of {self.total_pages} • Total: {len(self.data_list)}")
        return embed

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            await interaction.response.edit_message(embed=self.get_embed(), view=self)
        else:
            await interaction.response.defer()


class LibraryView(discord.ui.View):
    SECTIONS = {
        "reading": ("⚡ In Progress", COLORS["reading"], lambda b: b["status"] == "reading"),
        "completed": ("✨ Finished", COLORS["achievements"], lambda b: b["status"] == "completed"),
        "to_read": ("📌 Want to Read", COLORS["library"], lambda b: b["status"] == "to_read"),
        "abandoned": ("🚫 Abandoned", 0x95a5a6, lambda b: b["status"] == "abandoned"),
    }

    def __init__(self, display_name, bookshelf, section="reading"):
        super().__init__(timeout=180)
        self.display_name = display_name
        self.bookshelf = bookshelf
        self.section = section
        self.current_page = 0
        self.items_per_page = 5
        self._refresh_items()
        self.add_item(LibrarySectionSelect(self))

    def _refresh_items(self):
        _, _, filter_fn = self.SECTIONS[self.section]
        books = [b for b in self.bookshelf if filter_fn(b)]
        if self.section == "reading":
            self.items = [format_reading_book(b) for b in books]
            self.book_ids = [b.get("book_id") for b in books]
        elif self.section == "completed":
            self.items = [format_completed_book(b) for b in books]
            self.book_ids = [b.get("book_id") for b in books]
        else:
            self.items = [f"▪️ **{b['title']}**" for b in books]
            self.book_ids = [b.get("book_id") for b in books]
        self.total_pages = max(1, (len(self.items) - 1) // self.items_per_page + 1)
        if self.current_page >= self.total_pages:
            self.current_page = max(0, self.total_pages - 1)

    def get_embed(self):
        title, color, _ = self.SECTIONS[self.section]
        start = self.current_page * self.items_per_page
        page_items = self.items[start:start + self.items_per_page]
        description = "\n\n".join(page_items) if page_items else "*No books in this section.*"
        embed = discord.Embed(
            title=f"📖 {self.display_name}'s Library — {title}",
            description=description,
            color=color,
        )
        embed.set_footer(text=f"Page {self.current_page + 1} of {self.total_pages} · {len(self.items)} books")
        page_book_id = next((bid for bid in self.book_ids[start:start + self.items_per_page] if bid), None)
        return embed, page_book_id

    async def update_message(self, interaction: discord.Interaction):
        embed, book_id = self.get_embed()
        if book_id:
            thumbnail = await fetch_book_thumbnail(book_id)
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
        await interaction.response.edit_message(embed=embed, view=self)

    @discord.ui.button(label="◀", style=discord.ButtonStyle.secondary, row=1)
    async def previous_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page > 0:
            self.current_page -= 1
            embed, book_id = self.get_embed()
            if book_id:
                thumbnail = await fetch_book_thumbnail(book_id)
                if thumbnail:
                    embed.set_thumbnail(url=thumbnail)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()

    @discord.ui.button(label="▶", style=discord.ButtonStyle.secondary, row=1)
    async def next_page(self, interaction: discord.Interaction, button: discord.ui.Button):
        if self.current_page < self.total_pages - 1:
            self.current_page += 1
            embed, book_id = self.get_embed()
            if book_id:
                thumbnail = await fetch_book_thumbnail(book_id)
                if thumbnail:
                    embed.set_thumbnail(url=thumbnail)
            await interaction.response.edit_message(embed=embed, view=self)
        else:
            await interaction.response.defer()


class LibrarySectionSelect(discord.ui.Select):
    def __init__(self, library_view):
        self.library_view = library_view
        options = [
            discord.SelectOption(label=label, value=key)
            for key, (label, _, _) in LibraryView.SECTIONS.items()
        ]
        super().__init__(placeholder="Browse section...", options=options, min_values=1, max_values=1, row=0)

    async def callback(self, interaction: discord.Interaction):
        self.library_view.section = self.values[0]
        self.library_view.current_page = 0
        self.library_view._refresh_items()
        embed, book_id = self.library_view.get_embed()
        if book_id:
            thumbnail = await fetch_book_thumbnail(book_id)
            if thumbnail:
                embed.set_thumbnail(url=thumbnail)
        await interaction.response.edit_message(embed=embed, view=self.library_view)


# 4. RATINGS & REVIEWS
class RateModal(discord.ui.Modal, title="Rate & Review Your Book"):
    review_text = discord.ui.TextInput(
        label="Write a short review (Optional)",
        style=discord.TextStyle.paragraph,
        placeholder="Share your thoughts about this story...",
        required=False,
        max_length=500
    )

    def __init__(self, book_id, title, rating):
        super().__init__()
        self.book_id = book_id
        self.title = title
        self.rating = rating

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
                
                # Log de Histórico
                history = user_profile.get("history", [])
                history.append(f"🏆 Finished and rated **{self.title}** with {self.rating} stars on {datetime.now().strftime('%d/%m/%Y')}")
                
                await users_col.update_one({"_id": user_id}, {"$set": {"bookshelf": bookshelf, "history": history}})
            
            stars = "⭐" * int(self.rating)
            await interaction.response.send_message(f"🎉 Saved! You rated **{self.title}** {stars}!", ephemeral=True)

            rating_embed = discord.Embed(
                title="⭐ Rating Posted",
                description=f"**{interaction.user.display_name}** rated **{self.title}** {stars}",
                color=COLORS["achievements"],
            )
            if self.review_text.value:
                rating_embed.add_field(name="Review", value=f"> *{self.review_text.value}*", inline=False)
            thumbnail = await fetch_book_thumbnail(self.book_id)
            if thumbnail:
                rating_embed.set_thumbnail(url=thumbnail)
            rated_book = next((b for b in bookshelf if b.get("book_id") == self.book_id), None)
            await post_public_review(bot, interaction.user, rated_book or {"title": self.title, "book_id": self.book_id}, self.rating, self.review_text.value)
            if interaction.guild:
                await interaction.channel.send(embed=rating_embed)
        except Exception:
            await interaction.response.send_message("❌ An error occurred saving your review.", ephemeral=True)

class RatingDropdown(discord.ui.Select):
    def __init__(self, book_id, title):
        self.book_id = book_id
        self.title = title
        options = [
            discord.SelectOption(label="⭐⭐⭐⭐⭐ Masterpiece", value="5"),
            discord.SelectOption(label="⭐⭐⭐⭐ Great", value="4"),
            discord.SelectOption(label="⭐⭐⭐ Good / Okay", value="3"),
            discord.SelectOption(label="⭐⭐ Bad", value="2"),
            discord.SelectOption(label="⭐ Terrible", value="1"),
        ]
        super().__init__(placeholder="How many stars would you give it?", options=options)

    async def callback(self, interaction: discord.Interaction):
        await interaction.response.send_modal(RateModal(self.book_id, self.title, self.values[0]))

class RatingView(discord.ui.View):
    def __init__(self, book_id, title):
        super().__init__(timeout=None)
        self.add_item(RatingDropdown(book_id, title))


# 5. BOOKSHELF CORE BUTTONS
class ReadYearSelect(discord.ui.Select):
    def __init__(self, bookshelf_view):
        self.bookshelf_view = bookshelf_view
        year = current_year()
        options = [
            discord.SelectOption(label=str(y), value=str(y), default=(y == year))
            for y in range(year, year - 15, -1)
        ]
        super().__init__(
            placeholder="Year read (for yearly challenge)",
            options=options,
            min_values=1,
            max_values=1,
            row=1,
        )

    async def callback(self, interaction: discord.Interaction):
        self.bookshelf_view.read_year = int(self.values[0])
        await interaction.response.defer()


class BookshelfButtons(discord.ui.View):
    def __init__(self, book_id, title, total_pages, categories=None):
        super().__init__(timeout=None)
        self.book_id = book_id
        self.title = title
        self.total_pages = int(total_pages) if total_pages else 0
        self.categories = categories or []
        self.genres = list(normalize_genres(self.categories))
        self.subgenres = list(normalize_subgenres(self.categories))
        self.read_year = current_year()
        self.add_item(ReadYearSelect(self))

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
                    book["completed_year"] = self.read_year
                book_exists = True
                break
        
        if not book_exists:
            new_book = {
                "book_id": self.book_id, "title": self.title, "status": status,
                "current_page": 0, "total_pages": self.total_pages, "rating": None, "review": None,
                "categories": self.categories, "genres": self.genres, "subgenres": self.subgenres,
            }
            if status == "completed":
                new_book["completed_year"] = self.read_year
            bookshelf.append(new_book)
            
        emoji_map = {"to_read": "🔖 Plan:", "reading": "📖 Started:", "completed": "✅ Finished:"}
        history.append(f"{emoji_map.get(status, '▫️')} Moved **{self.title}** to {status.replace('_', ' ')} on {datetime.now().strftime('%d/%m/%Y')}")

        await users_col.update_one(
            {"_id": str(user_id)}, 
            {"$set": {"bookshelf": bookshelf, "history": history, "last_read": datetime.now()}}
        )

    @discord.ui.button(label="Want to Read", style=discord.ButtonStyle.blurple, emoji="🔖")
    async def to_read(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bookshelf(interaction.user.id, interaction.user.name, "to_read")
        await interaction.response.send_message(f"🔖 Added **{self.title}** to your Want to Read list!", ephemeral=True)

    @discord.ui.button(label="Currently Reading", style=discord.ButtonStyle.success, emoji="📖")
    async def reading(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bookshelf(interaction.user.id, interaction.user.name, "reading")
        await interaction.response.send_message(f"📖 Started reading **{self.title}**! Use `/progress` to log pages.", ephemeral=True)

    @discord.ui.button(label="Mark as Read", style=discord.ButtonStyle.secondary, emoji="✅")
    async def completed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.defer(ephemeral=True)
        await self.update_bookshelf(interaction.user.id, interaction.user.name, "completed")
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
        view = RatingView(self.book_id, self.title)
        year_note = f" ({self.read_year})" if self.read_year != current_year() else ""
        await interaction.followup.send(f"🎉 Added **{self.title}** as read{year_note}! Please select a rating below:", view=view, ephemeral=True)


class SearchResultSelect(discord.ui.Select):
    def __init__(self, items):
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
        super().__init__(placeholder="Select a book...", options=options, min_values=1, max_values=1)

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
                view=BookshelfButtons(book_id, book_title, pages, categories),
            )
        except Exception as e:
            print(f"SearchResultSelect error: {e}")
            if not interaction.response.is_done():
                await interaction.response.send_message(
                    "Could not load this book. Please try `/search` again.",
                    ephemeral=True,
                )


class SearchResultsView(discord.ui.View):
    def __init__(self, items):
        super().__init__(timeout=120)
        self.add_item(SearchResultSelect(items))

    async def on_timeout(self):
        for child in self.children:
            child.disabled = True


# 6. BUDDY READ JOIN BUTTON
class BuddyJoinView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    @discord.ui.button(label="Join Reading Group", style=discord.ButtonStyle.success, emoji="👥")
    async def join_group(self, interaction: discord.Interaction, button: discord.ui.Button):
        message, ephemeral = await join_buddy_group(self.match_id, interaction.user)
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
            "reading": "In Progress",
            "completed": "Finished",
            "to_read": "Want to Read",
            "abandoned": "Abandoned",
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


async def join_buddy_group(group_id, user):
    user_id = str(user.id)
    group = await buddies_col.find_one({"_id": group_id})

    if not group:
        return "❌ Reading group not found.", True

    if user_id in group["members"]:
        return "🤝 You are already a member of this reading group!", True

    await buddies_col.update_one({"_id": group_id}, {"$push": {"members": user_id}})
    return f"👥 **{user.display_name}** joined **{group['book_title']}**!", False


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
            print(f"✅ Cleared guild-only duplicates for server {GUILD_ID}.")
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
            title="📖 BuddyRead — Quick Start",
            description=(
                "Use commands in **server channels** or **DMs**.\n"
                "**Note:** `/buddyread` was renamed to `/bookclub`."
            ),
            color=COLORS["reading"],
        )
        embed.add_field(
            name="Most Used",
            value=(
                "`/search` — Find a book and add it to your shelf\n"
                "`/progress` — Log pages or update reading status\n"
                "`/profile` — Stats, challenge, badges\n"
                "`/bookclub month` — Start Book of the Month + post invite\n"
                "`/library` — Browse your bookshelf with covers"
            ),
            inline=False,
        )
        embed.set_footer(text="Use /help mode:Full Guide for every command.")
        await interaction.response.send_message(embed=embed, ephemeral=True)
        return

    embed = discord.Embed(
        title="📖 BuddyRead — Command Guide",
        description=(
            "Use commands in **server channels** or **DMs**.\n"
            "Finishing a book this year posts a public announcement in the reading channel.\n"
            "**Note:** `/buddyread` was renamed to `/bookclub`."
        ),
        color=COLORS["reading"],
    )
    embed.add_field(
        name="📚 Library",
        value=(
            "`/search` — Search by title or ISBN; optional **author** filter\n"
            "`/tbr` — View your Want to Read list\n"
            "`/library [member]` — Paginated bookshelf with covers\n"
            "`/edit_book` — Fix rating, year, or page count\n"
            "`/remove_book` — Remove a book from your library"
        ),
        inline=False,
    )
    embed.add_field(
        name="📖 Reading",
        value=(
            "`/progress` — Log **page**, **add_pages**, or set **status**\n"
            "Logging pages auto-starts TBR books as In Progress\n"
            "`/profile [member]` — Stats, challenge, badges & achievements\n"
            "`/challenge` — Set your reading goal for this year (1–100 books)"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Social",
        value=(
            "`/bookclub create` — Create a shared reading group\n"
            "`/bookclub month` — Create + post Book of the Month in one step\n"
            "`/bookclub join` — Join an existing reading group\n"
            "`/bookclub post` — Repost the invite in the book club channel\n"
            "`/bookclub delete` — Delete a reading group you created\n"
            "`/bookclub status` — Compare progress with progress bars\n"
            "`/leaderboard` — Server ranking (this year or all time)"
        ),
        inline=False,
    )
    embed.add_field(name="🎖️ Achievements", value=build_achievements_help(), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="search", description="Search for a book by title or ISBN")
@app_commands.describe(title="Book title or ISBN", author="Optional author name to narrow results")
async def search(interaction: discord.Interaction, title: str, author: str = None):
    await interaction.response.defer()
    api_url = "https://www.googleapis.com/books/v1/volumes"
    query = f"intitle:{title}+inauthor:{author}" if author else title
    params = {"q": query, "key": GOOGLE_BOOKS_KEY, "maxResults": 10}
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url, params=params) as response:
                if response.status != 200:
                    await interaction.followup.send("Could not connect to the Book API right now. 😢")
                    return
                data = await response.json()
                if "items" not in data:
                    await interaction.followup.send("No books found with that title. 😢")
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
                    await interaction.followup.send(embed=embed, view=SearchResultsView(items))
    except Exception:
        await interaction.followup.send("An unexpected error occurred while searching.")


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
        await interaction.response.send_message("❌ Your bookshelf is empty! Use `/search` to add books first.", ephemeral=True)
        return

    current_book = find_shelf_book(user_profile["bookshelf"], book_title)
    if not current_book:
        await interaction.response.send_message("❌ That book is not in your library.", ephemeral=True)
        return

    now = datetime.now()
    history = user_profile.get("history", [])

    if page is not None and add_pages is not None:
        await interaction.response.send_message("❌ Use either **page** or **add_pages**, not both.", ephemeral=True)
        return

    if add_pages is not None:
        if add_pages <= 0:
            await interaction.response.send_message("❌ **add_pages** must be greater than 0.", ephemeral=True)
            return
        page = current_book.get("current_page", 0) + add_pages

    if page is not None:
        if current_book["status"] == "completed":
            await interaction.response.send_message("❌ This book is already marked as Finished.", ephemeral=True)
            return

        if current_book["status"] != "reading":
            previous_status = current_book["status"]
            current_book["status"] = "reading"
            if previous_status == "to_read":
                history.append(f"📖 Started reading **{current_book['title']}**")
            elif previous_status == "abandoned":
                history.append(f"📖 Resumed reading **{current_book['title']}**")

        total_pages = current_book["total_pages"]
        if page < 0 or (total_pages > 0 and page > total_pages):
            await interaction.response.send_message("❌ Invalid page count!", ephemeral=True)
            return

        history.append(f"📈 Logged page **{page}** on *{current_book['title']}*")
        current_book["current_page"] = page
        percentage = round((page / total_pages) * 100) if total_pages > 0 else 100

        if total_pages > 0 and page == total_pages:
            await interaction.response.defer(ephemeral=True)
            current_book["status"] = "completed"
            current_book["completed_year"] = current_year()
            if is_current_year_finish(current_book):
                await mark_buddy_read_finish(user_id, current_book)
            await users_col.update_one(
                {"_id": user_id},
                {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, "last_read": now}},
            )
            if is_current_year_finish(current_book):
                completed_books = [b for b in user_profile["bookshelf"] if b["status"] == "completed"]
                await announce_book_finished(bot, interaction.user, current_book, completed_books, user_id)
            view = RatingView(current_book["book_id"], current_book["title"])
            await interaction.followup.send(f"🎉 You finished *{current_book['title']}*! Rate it below:", view=view, ephemeral=True)
        else:
            await users_col.update_one(
                {"_id": user_id},
                {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, "last_read": now}},
            )
            progress_bar_display = progress_bar(page, total_pages)
            await interaction.response.send_message(
                f"📖 **{interaction.user.display_name}** made progress on *{current_book['title']}*!\n"
                f"> `{progress_bar_display}` **{percentage}%** (Page {page}/{total_pages if total_pages > 0 else '?'})"
            )
        return

    if status is None and page is None and add_pages is None:
        await interaction.response.send_message("❌ Provide a **page**, **add_pages**, or a **status**.", ephemeral=True)
        return

    status_value = status.value
    if status_value == "completed":
        if year is not None and (year < 2000 or year > current_year()):
            await interaction.response.send_message(f"❌ Year must be between **2000** and **{current_year()}**.", ephemeral=True)
            return
        current_book["completed_year"] = year or current_year()

    status_messages = {
        "reading": ("📖 In Progress", f"📖 Marked **{current_book['title']}** as *In Progress*"),
        "completed": ("✅ Finished", f"🏆 Marked **{current_book['title']}** as *Finished*"),
        "abandoned": ("🚫 Abandoned", f"🚫 Marked **{current_book['title']}** as *Abandoned*"),
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
            {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, "last_read": now}},
        )

        if is_current_year_finish(current_book):
            completed_books = [b for b in user_profile["bookshelf"] if b["status"] == "completed"]
            await announce_book_finished(bot, interaction.user, current_book, completed_books, user_id)
        view = RatingView(current_book["book_id"], current_book["title"])
        await interaction.followup.send(f"🎉 You finished *{current_book['title']}*! Rate it below:", view=view, ephemeral=True)
    else:
        await users_col.update_one(
            {"_id": user_id},
            {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, "last_read": now}},
        )
        await interaction.response.send_message(f"{label} — **{current_book['title']}** updated!", ephemeral=True)


@bot.tree.command(name="profile", description="View reading profile, stats, and achievements")
@app_commands.describe(member="Member to view")
async def profile(interaction: discord.Interaction, member: discord.Member = None):
    target_user = member or interaction.user
    user_id = str(target_user.id)
    user_profile = await users_col.find_one({"_id": user_id})

    if not user_profile:
        await interaction.response.send_message(f"❌ {target_user.display_name} doesn't have a profile yet.", ephemeral=True)
        return

    bookshelf = user_profile.get("bookshelf", [])
    completed = [b for b in bookshelf if b["status"] == "completed"]
    reading = [b for b in bookshelf if b["status"] == "reading"]
    year_completed = books_completed_in_year(bookshelf, current_year())

    embed = discord.Embed(title=f"📚 {target_user.display_name}'s Library Profile", color=COLORS["profile"])
    embed.set_thumbnail(url=target_user.display_avatar.url)

    goal = user_profile.get("yearly_goal", 0)
    if goal > 0:
        challenge_year = current_year()
        percent = min(round((len(year_completed) / goal) * 100), 100)
        bar = progress_bar(len(year_completed), goal)
        embed.add_field(name=f"🏆 {challenge_year} Challenge", value=f"`{bar}` **{len(year_completed)} / {goal}** books ({percent}%)", inline=False)

    embed.add_field(name="📖 Reading", value=f"` {len(reading)} `", inline=True)
    embed.add_field(name="✅ Finished", value=f"` {len(completed)} `", inline=True)
    embed.add_field(name="🔖 TBR Wishlist", value=f"` {len([b for b in bookshelf if b['status'] == 'to_read'])} `", inline=True)

    avg = average_rating(completed)
    if avg:
        embed.add_field(name="⭐ Avg Rating", value=f"` {avg} ` / 5", inline=True)
    top_genre = favorite_genre(completed)
    if top_genre:
        embed.add_field(name="📂 Favorite Genre", value=top_genre, inline=True)
    if year_completed:
        embed.add_field(name="📅 Finished This Year", value=f"` {len(year_completed)} `", inline=True)

    if reading:
        current = reading[0]
        total = current.get("total_pages", 0) or 0
        current_page = current.get("current_page", 0) or 0
        pct = round((current_page / total) * 100) if total > 0 else 0
        embed.add_field(
            name="📖 Currently Reading",
            value=f"**{current['title']}**\n`{progress_bar(current_page, total)}` **{pct}%**",
            inline=False,
        )
        if current.get("book_id"):
            thumbnail = await fetch_book_thumbnail(current["book_id"])
            if thumbnail:
                embed.set_image(url=thumbnail)

    if completed:
        last = max(completed, key=lambda b: b.get("completed_year", 0))
        stars = "⭐" * last["rating"] if last.get("rating") else "No rating"
        embed.add_field(name="🏁 Latest Finish", value=f"**{last['title']}** ({stars})", inline=False)

    badges = get_profile_badges(completed)
    embed.add_field(name="🎖️ Unlocked Achievements", value="\n".join(badges) if badges else "*No badges earned yet. Start reading!*", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="challenge", description="Set your reading goal for this year")
@app_commands.describe(books_goal="How many books you want to finish this year (1–100)")
async def challenge(interaction: discord.Interaction, books_goal: int):
    if books_goal < 1 or books_goal > 100:
        await interaction.response.send_message("❌ Please set a goal between **1** and **100** books.", ephemeral=True)
        return
    user_id = str(interaction.user.id)
    current_year = datetime.now().year
    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"yearly_goal": books_goal, "username": interaction.user.name}},
        upsert=True,
    )
    await interaction.response.send_message(f"🏆 Your {current_year} Reading Challenge has been set to **{books_goal}** books!", ephemeral=True)


# BOOK CLUB (/bookclub)
buddy_group = app_commands.Group(name="bookclub", description="Create, join, and track shared reading groups")

@buddy_group.command(name="create", description="Create a shared reading group for one of your books")
@app_commands.autocomplete(book_title=book_autocomplete)
async def buddy_create(interaction: discord.Interaction, book_title: str):
    user_profile = await users_col.find_one({"_id": str(interaction.user.id)})
    shelf_book = find_shelf_book(user_profile.get("bookshelf", []), book_title) if user_profile else None
    resolved_title = shelf_book["title"] if shelf_book else book_title
    existing = await buddies_col.find_one({"book_title": {"$regex": f"^{resolved_title}$", "$options": "i"}, "closed": {"$ne": True}})
    if existing:
        await interaction.response.send_message(
            f"❌ A reading group for **{resolved_title}** already exists. Use `/bookclub join` or `/bookclub post`.",
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
        "members": [str(interaction.user.id)]
    }
    await buddies_col.insert_one(group_doc)
    
    embed = build_bookclub_invite_embed(interaction.user.display_name, resolved_title)
    if resolved_thumbnail:
        embed.set_thumbnail(url=resolved_thumbnail)
    await interaction.response.send_message(embed=embed, view=BuddyJoinView(match_id))

    if BOOKCLUB_CHANNEL_ID:
        posted = await post_bookclub_invite(bot, group_doc)
        if not posted:
            await interaction.followup.send(
                "⚠️ Group created, but I could not post it in the configured book club channel.",
                ephemeral=True,
            )

@buddy_group.command(name="month", description="Create and post Book of the Month in one step")
@app_commands.autocomplete(book_title=book_autocomplete)
async def buddy_month(interaction: discord.Interaction, book_title: str):
    user_profile = await users_col.find_one({"_id": str(interaction.user.id)})
    shelf_book = find_shelf_book(user_profile.get("bookshelf", []), book_title) if user_profile else None
    resolved_title = shelf_book["title"] if shelf_book else book_title
    existing = await buddies_col.find_one({"book_title": {"$regex": f"^{resolved_title}$", "$options": "i"}, "closed": {"$ne": True}})
    if existing:
        group = await hydrate_legacy_bookclub_group(existing)
        posted = await post_bookclub_invite(bot, group, reminder=True)
        if not posted:
            await interaction.response.send_message(
                "❌ A group already exists and I could not post in the book club channel.",
                ephemeral=True,
            )
            return
        await interaction.response.send_message(
            f"📣 Reposted **{resolved_title}** — a group for this book already exists.",
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

    posted = await post_bookclub_invite(bot, group_doc)
    if not posted:
        await interaction.response.send_message(
            "⚠️ Group created, but I could not post in the book club channel. Set `BOOKCLUB_CHANNEL_ID` and check permissions.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"📚 **Book of the Month** set to **{resolved_title}** and posted in the book club channel!",
        ephemeral=True,
    )

@buddy_group.command(name="join", description="Join an existing reading group")
@app_commands.autocomplete(group_id=buddy_autocomplete)
async def buddy_join(interaction: discord.Interaction, group_id: str):
    message, ephemeral = await join_buddy_group(group_id, interaction.user)
    await interaction.response.send_message(message, ephemeral=ephemeral)

@buddy_group.command(name="delete", description="Delete a reading group you created")
@app_commands.autocomplete(group_id=buddy_autocomplete)
async def buddy_delete(interaction: discord.Interaction, group_id: str):
    group = await buddies_col.find_one({"_id": group_id})
    if not group:
        await interaction.response.send_message("❌ Reading group not found.", ephemeral=True)
        return

    if group.get("host_id") != str(interaction.user.id):
        await interaction.response.send_message("❌ Only the group host can delete this reading group.", ephemeral=True)
        return

    await buddies_col.delete_one({"_id": group_id})
    await interaction.response.send_message(
        f"🗑️ Deleted the reading group for **{group.get('book_title', 'Unknown Title')}**.",
        ephemeral=True,
    )

@buddy_group.command(name="post", description="Post or repost a reading group invite in the book club channel")
@app_commands.autocomplete(group_id=buddy_autocomplete)
async def buddy_post(interaction: discord.Interaction, group_id: str):
    group = await buddies_col.find_one({"_id": group_id})
    if not group:
        await interaction.response.send_message("❌ Reading group not found.", ephemeral=True)
        return

    group = await hydrate_legacy_bookclub_group(group)

    posted = await post_bookclub_invite(bot, group, reminder=True)
    if not posted:
        await interaction.response.send_message(
            "❌ I could not post in the configured book club channel. Set `BOOKCLUB_CHANNEL_ID` and check permissions.",
            ephemeral=True,
        )
        return

    await interaction.response.send_message(
        f"📣 Posted **{group['book_title']}** in the book club channel.",
        ephemeral=True,
    )

@buddy_group.command(name="status", description="Check the reading progress of a shared group")
@app_commands.autocomplete(group_id=buddy_autocomplete)
async def buddy_status(interaction: discord.Interaction, group_id: str):
    group = await buddies_col.find_one({"_id": group_id})
    if not group:
        await interaction.response.send_message("❌ Reading group not found.", ephemeral=True)
        return
    group = await hydrate_legacy_bookclub_group(group)

    await interaction.response.defer()
    rows = []
    all_finished = True
    
    for member_id in group["members"]:
        member_profile = await users_col.find_one({"_id": member_id})
        name = member_profile.get("username", f"User {member_id}") if member_profile else f"User {member_id}"
        
        page_info = "Not started"
        if member_profile and member_profile.get("bookshelf"):
            match_book = find_group_book(member_profile["bookshelf"], group)
            if match_book:
                if match_book["status"] == "completed":
                    page_info = "✅ Finished"
                elif match_book["status"] == "abandoned":
                    page_info = "🚫 Abandoned"
                    all_finished = False
                elif match_book["status"] == "reading":
                    all_finished = False
                    total = match_book.get("total_pages", 0) or 0
                    current = match_book.get("current_page", 0) or 0
                    pct = round((current / total) * 100) if total > 0 else 0
                    bar = progress_bar(current, total)
                    page_info = f"`{bar}` **{pct}%** (Page {current}/{total if total > 0 else '?'})"
                else:
                    all_finished = False
                    page_info = "📌 TBR"
            else:
                all_finished = False
                
        rows.append(f"• **{name}**: {page_info}")

    if all_finished and len(group["members"]) > 0:
        await buddies_col.update_one({"_id": group_id}, {"$set": {"closed": True}})

    embed = discord.Embed(
        title=f"📊 Reading Group Status: {group['book_title']}",
        description="\n".join(rows),
        color=COLORS["social"],
    )
    embed.set_footer(text=f"👥 {len(group['members'])} members")
    thumbnail = group.get("thumbnail_url") or await fetch_book_thumbnail(group.get("book_id"))
    if thumbnail:
        embed.set_thumbnail(url=thumbnail)
    await interaction.followup.send(embed=embed)

bot.tree.add_command(buddy_group)


@bot.tree.command(name="library", description="View your entire structured bookshelf collections")
async def library(interaction: discord.Interaction, member: discord.Member = None):
    target_user = member or interaction.user
    user_id = str(target_user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    if not user_profile or not user_profile.get("bookshelf"):
        await interaction.response.send_message(f"❌ {target_user.display_name}'s library is completely empty.", ephemeral=True)
        return
    bookshelf = user_profile["bookshelf"]
    view = LibraryView(target_user.display_name, bookshelf)
    embed, book_id = view.get_embed()
    if book_id:
        thumbnail = await fetch_book_thumbnail(book_id)
        if thumbnail:
            embed.set_thumbnail(url=thumbnail)
    await interaction.response.send_message(embed=embed, view=view)

@bot.tree.command(name="leaderboard", description="See the top readers in this server")
@app_commands.describe(period="Count books finished this year or all time")
@app_commands.choices(period=[
    app_commands.Choice(name="This Year", value="year"),
    app_commands.Choice(name="All Time", value="all"),
])
async def leaderboard(interaction: discord.Interaction, period: app_commands.Choice[str] = None):
    await interaction.response.defer()
    period_value = period.value if period else "year"
    cursor = users_col.find()
    all_users = await cursor.to_list(length=100)
    leaderboard_data = []
    for user_data in all_users:
        bookshelf = user_data.get("bookshelf", [])
        if period_value == "year":
            completed_count = len(books_completed_in_year(bookshelf, current_year()))
        else:
            completed_count = len([b for b in bookshelf if b["status"] == "completed"])
        if completed_count > 0:
            leaderboard_data.append({"username": user_data.get("username", "Unknown Reader"), "completed": completed_count})
    leaderboard_data = sorted(leaderboard_data, key=lambda x: x["completed"], reverse=True)
    if not leaderboard_data:
        await interaction.followup.send("🏆 The leaderboard is empty!")
        return
    medal_emojis = ["🥇", "🥈", "🥉"]
    period_label = "This Year" if period_value == "year" else "All Time"
    leaderboard_rows = []
    for rank, entry in enumerate(leaderboard_data):
        prefix = medal_emojis[rank] if rank < 3 else f"` #{rank + 1} `"
        leaderboard_rows.append(f"{prefix} **{entry['username']}** — `{entry['completed']}` books finished")
    paginator = PaginatorView(
        title=f"🏆 Server Reading Leaderboard — {period_label}",
        data_list=leaderboard_rows,
        color=COLORS["achievements"],
        items_per_page=10,
    )
    await interaction.followup.send(embed=paginator.get_embed(), view=paginator)

@bot.tree.command(name="edit_book", description="Edit rating, year, or page count for a book")
@app_commands.autocomplete(title=book_autocomplete)
@app_commands.describe(title="Book to edit", rating="Star rating (1–5)", year="Year finished", total_pages="Total pages in the book")
async def edit_book(
    interaction: discord.Interaction,
    title: str,
    rating: int = None,
    year: int = None,
    total_pages: int = None,
):
    if rating is None and year is None and total_pages is None:
        await interaction.response.send_message("❌ Provide at least one field to update: **rating**, **year**, or **total_pages**.", ephemeral=True)
        return
    if rating is not None and (rating < 1 or rating > 5):
        await interaction.response.send_message("❌ Rating must be between **1** and **5**.", ephemeral=True)
        return
    if year is not None and (year < 2000 or year > current_year()):
        await interaction.response.send_message(f"❌ Year must be between **2000** and **{current_year()}**.", ephemeral=True)
        return
    if total_pages is not None and total_pages < 1:
        await interaction.response.send_message("❌ Total pages must be at least **1**.", ephemeral=True)
        return

    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    if not user_profile or not user_profile.get("bookshelf"):
        await interaction.response.send_message("❌ Your bookshelf is empty!", ephemeral=True)
        return

    book = find_shelf_book(user_profile["bookshelf"], title)
    if not book:
        await interaction.response.send_message("❌ Could not find that book.", ephemeral=True)
        return

    updates = []
    if rating is not None:
        book["rating"] = rating
        updates.append(f"rating → {'⭐' * rating}")
    if year is not None:
        book["completed_year"] = year
        updates.append(f"year → {year}")
    if total_pages is not None:
        book["total_pages"] = total_pages
        updates.append(f"pages → {total_pages}")

    await users_col.update_one({"_id": user_id}, {"$set": {"bookshelf": user_profile["bookshelf"]}})
    await interaction.response.send_message(
        f"✏️ Updated **{book['title']}**: {', '.join(updates)}",
        ephemeral=True,
    )

@bot.tree.command(name="remove_book", description="Remove a specific book from your library")
@app_commands.autocomplete(title=book_autocomplete)
async def remove_book(interaction: discord.Interaction, title: str):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    if not user_profile or not user_profile.get("bookshelf"):
        await interaction.response.send_message("❌ Your bookshelf is empty!", ephemeral=True)
        return
    bookshelf = user_profile["bookshelf"]
    book = find_shelf_book(bookshelf, title)
    if not book:
        await interaction.response.send_message("❌ Could not find that book.", ephemeral=True)
        return
    book_id = book.get("book_id")
    removed_title = book["title"]
    if book_id:
        updated_bookshelf = [b for b in bookshelf if b.get("book_id") != book_id]
    else:
        updated_bookshelf = [b for b in bookshelf if b["title"].lower() != removed_title.lower()]
    await users_col.update_one({"_id": user_id}, {"$set": {"bookshelf": updated_bookshelf}})
    await interaction.response.send_message(f"🗑️ Successfully removed **{removed_title}** from your library!", ephemeral=True)

@bot.tree.command(name="tbr", description="View your 'Want to Read' book list")
async def tbr(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    if not user_profile or not user_profile.get("bookshelf"):
        await interaction.response.send_message("🔖 Your TBR list is empty!", ephemeral=True)
        return
    want_to_read = [f"▪️ **{b['title']}**" for b in user_profile["bookshelf"] if b["status"] == "to_read"]
    if not want_to_read:
        await interaction.response.send_message("🔖 You don't have any books marked as 'Want to Read' right now.", ephemeral=True)
        return
    paginator = PaginatorView(title=f"🔖 {interaction.user.display_name}'s TBR List", data_list=want_to_read, color=0x9013fe, items_per_page=8)
    await interaction.response.send_message(embed=paginator.get_embed(), view=paginator)


# 12. RUN THE BOT
async def main():
    if os.getenv("PORT"):
        await start_health_server()
    async with bot:
        await bot.start(DISCORD_TOKEN)


if __name__ == "__main__":
    asyncio.run(main())
