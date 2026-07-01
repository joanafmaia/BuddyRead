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

if not all([DISCORD_TOKEN, MONGO_URI, GOOGLE_BOOKS_KEY]):
    raise RuntimeError(
        "Missing required environment variables. "
        "Copy .env.example to .env and fill in DISCORD_TOKEN, MONGO_URI, and GOOGLE_BOOKS_KEY."
    )

db_client = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())

db = db_client["book_bot_db"]
users_col = db["users"]
buddies_col = db["buddy_reads"]


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
                    return data.get("volumeInfo", {}).get("imageLinks", {}).get("thumbnail", "").replace("http://", "https://")
    except Exception:
        pass
    return ""


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


async def announce_book_finished(channel, member, book, completed_books, user_id):
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
    await channel.send(embed=embed)


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
                color=0xe6a15c,
            )
            if self.review_text.value:
                rating_embed.add_field(name="Review", value=f"> *{self.review_text.value}*", inline=False)
            thumbnail = await fetch_book_thumbnail(self.book_id)
            if thumbnail:
                rating_embed.set_thumbnail(url=thumbnail)
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
class BookshelfButtons(discord.ui.View):
    def __init__(self, book_id, title, total_pages, categories=None):
        super().__init__(timeout=None)
        self.book_id = book_id
        self.title = title
        self.total_pages = int(total_pages) if total_pages else 0
        self.categories = categories or []
        self.genres = list(normalize_genres(self.categories))
        self.subgenres = list(normalize_subgenres(self.categories))

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
                book_exists = True
                break
        
        if not book_exists:
            bookshelf.append({
                "book_id": self.book_id, "title": self.title, "status": status,
                "current_page": 0, "total_pages": self.total_pages, "rating": None, "review": None,
                "categories": self.categories, "genres": self.genres, "subgenres": self.subgenres,
            })
            
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
        await self.update_bookshelf(interaction.user.id, interaction.user.name, "completed")
        user_profile = await users_col.find_one({"_id": str(interaction.user.id)})
        book = next((b for b in user_profile["bookshelf"] if b["book_id"] == self.book_id), None)
        if book:
            await mark_buddy_read_finish(interaction.user.id, book)
            await users_col.update_one(
                {"_id": str(interaction.user.id)},
                {"$set": {"bookshelf": user_profile["bookshelf"]}},
            )
            completed_books = [b for b in user_profile["bookshelf"] if b["status"] == "completed"]
            await announce_book_finished(interaction.channel, interaction.user, book, completed_books, interaction.user.id)
        view = RatingView(self.book_id, self.title)
        await interaction.response.send_message("🎉 Awesome! Please select a rating below:", view=view, ephemeral=True)


# 6. BUDDY READ JOIN BUTTON
class BuddyJoinView(discord.ui.View):
    def __init__(self, match_id):
        super().__init__(timeout=None)
        self.match_id = match_id

    @discord.ui.button(label="Join Reading Group", style=discord.ButtonStyle.success, emoji="👥")
    async def join_group(self, interaction: discord.Interaction, button: discord.ui.Button):
        user_id = str(interaction.user.id)
        group = await buddies_col.find_one({"_id": self.match_id})
        
        if not group:
            await interaction.response.send_message("❌ This reading group no longer exists.", ephemeral=True)
            return
            
        if user_id in group["members"]:
            await interaction.response.send_message("🤝 You are already a member of this reading group!", ephemeral=True)
            return

        await buddies_col.update_one({"_id": self.match_id}, {"$push": {"members": user_id}})
        await interaction.response.send_message(f"👥 **{interaction.user.display_name}** successfully joined the room to read **{group['book_title']}**!")


# 7. AUTOCOMPLETE FUNCTIONS
async def book_autocomplete(interaction: discord.Interaction, current: str):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    if not user_profile or "bookshelf" not in user_profile:
        return []
    status_labels = {
        "reading": "In Progress",
        "completed": "Finished",
        "to_read": "Want to Read",
        "abandoned": "Abandoned",
    }
    return [
        app_commands.Choice(
            name=f"{b['title'][:80]} ({status_labels.get(b['status'], b['status'])})",
            value=b["title"],
        )
        for b in user_profile["bookshelf"]
        if current.lower() in b["title"].lower()
    ][:25]

async def buddy_autocomplete(interaction: discord.Interaction, current: str):
    cursor = buddies_col.find()
    groups = await cursor.to_list(length=25)
    return [app_commands.Choice(name=f"{g['book_title']} (Host: {g['host_name']})", value=g["_id"]) for g in groups if current.lower() in g["book_title"].lower()][:25]


@bot.event
async def on_ready():
    try:
        if GUILD_ID:
            guild = discord.Object(id=int(GUILD_ID))
            bot.tree.copy_global_to(guild=guild)
            synced = await bot.tree.sync(guild=guild)
            print(f"✅ Synced {len(synced)} commands to guild {GUILD_ID}.")
        else:
            synced = await bot.tree.sync()
            print(f"✅ Synced {len(synced)} commands globally.")
    except Exception as e:
        print(f"❌ Erro na sincronização: {e}")
        
    print(f"🚀 Bot {bot.user} está online e pronto!")



# 8. ALL COMMANDS

@bot.tree.command(name="help", description="List all BuddyRead commands")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(
        title="📖 BuddyRead — Command Guide",
        description="Everything you need to manage your reading life:",
        color=0x50e3c2,
    )
    embed.add_field(
        name="📚 Library",
        value=(
            "`/search` — Find a book and add it to your shelf\n"
            "`/tbr` — View your Want to Read list\n"
            "`/library` — View a full bookshelf (yours or someone else's)\n"
            "`/remove_book` — Remove a book from your library"
        ),
        inline=False,
    )
    embed.add_field(
        name="📖 Reading",
        value=(
            "`/progress` — Log pages or update status (In Progress, Finished, Abandoned)\n"
            "`/profile` — View stats, badges, and achievements\n"
            "`/challenge` — Set your yearly reading goal"
        ),
        inline=False,
    )
    embed.add_field(
        name="👥 Social",
        value=(
            "`/buddyread create` — Start a group reading event\n"
            "`/buddyread status` — See everyone's progress in a group\n"
            "`/leaderboard` — Server reading rankings"
        ),
        inline=False,
    )
    embed.add_field(name="🎖️ Achievements", value=build_achievements_help(), inline=False)
    await interaction.response.send_message(embed=embed, ephemeral=True)


@bot.tree.command(name="search", description="Search for a book by title or ISBN")
async def search(interaction: discord.Interaction, title: str):
    await interaction.response.defer()
    api_url = f"https://www.googleapis.com/books/v1/volumes?q={title}&key={GOOGLE_BOOKS_KEY}&maxResults=1"
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(api_url) as response:
                if response.status != 200:
                    await interaction.followup.send("Could not connect to the Book API right now. 😢")
                    return
                data = await response.json()
                if "items" not in data:
                    await interaction.followup.send("No books found with that title. 😢")
                    return

                volume_info = data["items"][0]["volumeInfo"]
                book_id = data["items"][0]["id"]
                book_title = volume_info.get("title", "Unknown Title")
                authors = ", ".join(volume_info.get("authors", ["Unknown Author"]))
                description = volume_info.get("description", "No description available.")[:400] + "..."
                pages = volume_info.get("pageCount", 0)
                categories = volume_info.get("categories", [])
                thumbnail = volume_info.get("imageLinks", {}).get("thumbnail", "").replace("http://", "https://")

                embed = discord.Embed(title=f"📖 {book_title}", description=f"**Author(s):** {authors}\n**Pages:** {pages if pages > 0 else '???'}\n\n*{description}*", color=0x4a90e2)
                if categories:
                    embed.add_field(name="Genre", value=categories[0], inline=True)
                if thumbnail: embed.set_thumbnail(url=thumbnail)
                await interaction.followup.send(embed=embed, view=BookshelfButtons(book_id, book_title, pages, categories))
    except Exception:
        await interaction.followup.send("An unexpected error occurred while searching.")


@bot.tree.command(name="progress", description="Log pages read or update reading status for a book")
@app_commands.autocomplete(book_title=book_autocomplete)
@app_commands.describe(page="Current page number (for books in progress)", status="Update status without logging pages")
@app_commands.choices(status=[
    app_commands.Choice(name="In Progress", value="reading"),
    app_commands.Choice(name="Finished", value="completed"),
    app_commands.Choice(name="Abandoned", value="abandoned"),
])
async def progress(
    interaction: discord.Interaction,
    book_title: str,
    page: int = None,
    status: app_commands.Choice[str] = None,
):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})

    if not user_profile or not user_profile.get("bookshelf"):
        await interaction.response.send_message("❌ Your bookshelf is empty! Use `/search` to add books first.", ephemeral=True)
        return

    current_book = next((b for b in user_profile["bookshelf"] if b["title"].lower() == book_title.lower()), None)
    if not current_book:
        await interaction.response.send_message("❌ That book is not in your library.", ephemeral=True)
        return

    now = datetime.now()
    history = user_profile.get("history", [])

    if page is not None:
        if current_book["status"] != "reading":
            await interaction.response.send_message("❌ Book is not actively marked as 'In Progress'. Set status first or use `/search` to start reading.", ephemeral=True)
            return

        total_pages = current_book["total_pages"]
        if page < 0 or (total_pages > 0 and page > total_pages):
            await interaction.response.send_message("❌ Invalid page count!", ephemeral=True)
            return

        history.append(f"📈 Logged page **{page}** on *{current_book['title']}*")
        current_book["current_page"] = page
        percentage = round((page / total_pages) * 100) if total_pages > 0 else 100

        if total_pages > 0 and page == total_pages:
            current_book["status"] = "completed"
            await mark_buddy_read_finish(user_id, current_book)
            await users_col.update_one(
                {"_id": user_id},
                {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, "last_read": now}},
            )
            completed_books = [b for b in user_profile["bookshelf"] if b["status"] == "completed"]
            await announce_book_finished(interaction.channel, interaction.user, current_book, completed_books, user_id)
            view = RatingView(current_book["book_id"], current_book["title"])
            await interaction.response.send_message(f"🎉 You finished *{current_book['title']}*! Rate it below:", view=view, ephemeral=True)
        else:
            await users_col.update_one(
                {"_id": user_id},
                {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, "last_read": now}},
            )
            filled_blocks = round(percentage / 10)
            progress_bar = "⬛" * filled_blocks + "⬜" * (10 - filled_blocks)
            await interaction.response.send_message(
                f"📖 **{interaction.user.display_name}** made progress on *{current_book['title']}*!\n"
                f"> `{progress_bar}` **{percentage}%** (Page {page}/{total_pages})"
            )
        return

    if status is None:
        await interaction.response.send_message("❌ Provide a **page** number or a **status**.", ephemeral=True)
        return

    status_value = status.value
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
        await mark_buddy_read_finish(user_id, current_book)

    await users_col.update_one(
        {"_id": user_id},
        {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, "last_read": now}},
    )

    if status_value == "completed":
        completed_books = [b for b in user_profile["bookshelf"] if b["status"] == "completed"]
        await announce_book_finished(interaction.channel, interaction.user, current_book, completed_books, user_id)
        view = RatingView(current_book["book_id"], current_book["title"])
        await interaction.response.send_message(f"🎉 You finished *{current_book['title']}*! Rate it below:", view=view, ephemeral=True)
    else:
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

    embed = discord.Embed(title=f"📚 {target_user.display_name}'s Library Profile", color=0x2b2d31)
    embed.set_thumbnail(url=target_user.display_avatar.url)

    # Desafio Anual Dinâmico
    goal = user_profile.get("yearly_goal", 0)
    if goal > 0:
        current_year = datetime.now().year
        percent = min(round((len(completed) / goal) * 100), 100)
        bar = "🟩" * round(percent / 10) + "⬛" * (10 - round(percent / 10))
        embed.add_field(name=f"🏆 {current_year} Challenge", value=f"`{bar}` **{len(completed)} / {goal}** books ({percent}%)", inline=False)

    embed.add_field(name="📖 Reading", value=f"` {len(reading)} `", inline=True)
    embed.add_field(name="✅ Finished", value=f"` {len(completed)} `", inline=True)
    embed.add_field(name="🔖 TBR Wishlist", value=f"` {len([b for b in bookshelf if b['status'] == 'to_read'])} `", inline=True)

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


# BUDDY READ (/buddyread)
buddy_group = app_commands.Group(name="buddyread", description="Read books synchronized with your group")

@buddy_group.command(name="create", description="Launch a public reading club event for a specific book")
@app_commands.autocomplete(book_title=book_autocomplete)
async def buddy_create(interaction: discord.Interaction, book_title: str):
    match_id = f"buddy_{interaction.user.id}_{int(datetime.now().timestamp())}"
    group_doc = {
        "_id": match_id,
        "book_title": book_title,
        "host_id": str(interaction.user.id),
        "host_name": interaction.user.display_name,
        "members": [str(interaction.user.id)]
    }
    await buddies_col.insert_one(group_doc)
    
    embed = discord.Embed(title="👥 New Buddy Read Event Started!", description=f"**{interaction.user.display_name}** wants to read **{book_title}** together! Click the button below to join the journey.", color=0x2ecc71)
    await interaction.response.send_message(embed=embed, view=BuddyJoinView(match_id))

@buddy_group.command(name="status", description="Compare your current status vs friends inside a group room")
@app_commands.autocomplete(group_id=buddy_autocomplete)
async def buddy_status(interaction: discord.Interaction, group_id: str):
    group = await buddies_col.find_one({"_id": group_id})
    if not group:
        await interaction.response.send_message("❌ Group event not found.", ephemeral=True)
        return

    await interaction.response.defer()
    rows = []
    
    for member_id in group["members"]:
        member_profile = await users_col.find_one({"_id": member_id})
        name = member_profile.get("username", f"User {member_id}") if member_profile else f"User {member_id}"
        
        page_info = "Not started"
        if member_profile and "bookshelf" in member_profile:
            match_book = next((b for b in member_profile["bookshelf"] if group["book_title"].lower() in b["title"].lower()), None)
            if match_book:
                if match_book["status"] == "completed":
                    page_info = "✅ Finished"
                elif match_book["status"] == "abandoned":
                    page_info = "🚫 Abandoned"
                elif match_book["status"] == "reading":
                    page_info = "📖 In Progress"
                else:
                    page_info = f"Page `{match_book['current_page']}/{match_book['total_pages']}`"
                
        rows.append(f"• **{name}**: {page_info}")

    embed = discord.Embed(title=f"📊 Status Room: {group['book_title']}", description="\n".join(rows), color=0x7ed321)
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
    reading_list = [f"▪️ **{b['title']}** (`{b['current_page']}/{b['total_pages']}` pages)" for b in bookshelf if b["status"] == "reading"]
    completed_list = [f"▪️ **{b['title']}** ({'⭐'*b['rating'] if b.get('rating') else 'No Rating'})" for b in bookshelf if b["status"] == "completed"]
    abandoned_list = [f"▪️ **{b['title']}**" for b in bookshelf if b["status"] == "abandoned"]
    tbr_list = [f"▪️ {b['title']}" for b in bookshelf if b["status"] == "to_read"]
    embed = discord.Embed(title=f"📖 {target_user.display_name}'s Book Collections", color=0x7ed321)
    embed.add_field(name="⚡ In Progress", value="\n".join(reading_list) if reading_list else "*No books in progress.*", inline=False)
    embed.add_field(name="✨ Finished", value="\n".join(completed_list[:15]) if completed_list else "*No finished books yet.*", inline=False)
    embed.add_field(name="🚫 Abandoned", value="\n".join(abandoned_list[:15]) if abandoned_list else "*No abandoned books.*", inline=False)
    embed.add_field(name="📌 Want to Read", value="\n".join(tbr_list[:15]) if tbr_list else "*TBR collection is empty.*", inline=False)
    await interaction.response.send_message(embed=embed)

@bot.tree.command(name="leaderboard", description="See the top readers in this server")
async def leaderboard(interaction: discord.Interaction):
    await interaction.response.defer()
    cursor = users_col.find()
    all_users = await cursor.to_list(length=100)
    leaderboard_data = []
    for user_data in all_users:
        bookshelf = user_data.get("bookshelf", [])
        completed_count = len([b for b in bookshelf if b["status"] == "completed"])
        if completed_count > 0:
            leaderboard_data.append({"username": user_data.get("username", "Unknown Reader"), "completed": completed_count})
    leaderboard_data = sorted(leaderboard_data, key=lambda x: x["completed"], reverse=True)
    if not leaderboard_data:
        await interaction.followup.send("🏆 The leaderboard is empty!")
        return
    medal_emojis = ["🥇", "🥈", "🥉"]
    leaderboard_rows = []
    for rank, entry in enumerate(leaderboard_data):
        prefix = medal_emojis[rank] if rank < 3 else f"` #{rank + 1} `"
        leaderboard_rows.append(f"{prefix} **{entry['username']}** — `{entry['completed']}` books finished")
    paginator = PaginatorView(title="🏆 Server Reading Leaderboard", data_list=leaderboard_rows, color=0xf5a623, items_per_page=10)
    await interaction.followup.send(embed=paginator.get_embed(), view=paginator)

@bot.tree.command(name="remove_book", description="Remove a specific book from your library")
@app_commands.autocomplete(title=book_autocomplete)
async def remove_book(interaction: discord.Interaction, title: str):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    if not user_profile or not user_profile.get("bookshelf"):
        await interaction.response.send_message("❌ Your bookshelf is empty!", ephemeral=True)
        return
    bookshelf = user_profile["bookshelf"]
    updated_bookshelf = [b for b in bookshelf if b["title"].lower() != title.lower()]
    if len(bookshelf) == len(updated_bookshelf):
        await interaction.response.send_message(f"❌ Could not find that book.", ephemeral=True)
        return
    await users_col.update_one({"_id": user_id}, {"$set": {"bookshelf": updated_bookshelf}})
    await interaction.response.send_message(f"🗑️ Successfully removed **{title}** from your library!", ephemeral=True)

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
