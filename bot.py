import os
import discord
import certifi
from discord.ext import commands, tasks
import aiohttp
from motor.motor_asyncio import AsyncIOMotorClient
from datetime import datetime, timedelta
from discord import app_commands
import random
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

if not all([DISCORD_TOKEN, MONGO_URI, GOOGLE_BOOKS_KEY]):
    raise RuntimeError(
        "Missing required environment variables. "
        "Copy .env.example to .env and fill in DISCORD_TOKEN, MONGO_URI, and GOOGLE_BOOKS_KEY."
    )

db_client = AsyncIOMotorClient(MONGO_URI, tlsCAFile=certifi.where())

db = db_client["book_bot_db"]
users_col = db["users"]
quotes_col = db["quotes"]        # Nova tabela de citações
buddies_col = db["buddy_reads"]  # Nova tabela de leituras conjuntas


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
            
            thumbnail = ""
            api_url = f"https://www.googleapis.com/books/v1/volumes/{self.book_id}?key={GOOGLE_BOOKS_KEY}"
            try:
                async with aiohttp.ClientSession() as session:
                    async with session.get(api_url) as resp:
                        if resp.status == 200:
                            b_data = await resp.json()
                            thumbnail = b_data.get("volumeInfo", {}).get("imageLinks", {}).get("thumbnail", "").replace("http://", "https://")
            except Exception:
                pass

            review_embed = discord.Embed(
                title=f"🌟 Book Completed & Reviewed!",
                description=f"**{interaction.user.display_name}** just finished reading **{self.title}**!",
                color=0xe6a15c
            )
            review_embed.add_field(name="Rating", value=stars, inline=True)
            if self.review_text.value:
                review_embed.add_field(name="Review", value=f"> *{self.review_text.value}*", inline=False)
            
            if thumbnail:
                review_embed.set_thumbnail(url=thumbnail)
                
            await interaction.channel.send(embed=review_embed)
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
    def __init__(self, book_id, title, total_pages):
        super().__init__(timeout=None)
        self.book_id = book_id
        self.title = title
        self.total_pages = int(total_pages) if total_pages else 0

    async def update_bookshelf(self, user_id, username, status):
        user_profile = await users_col.find_one({"_id": str(user_id)})
        if not user_profile:
            user_profile = {"_id": str(user_id), "username": username, "yearly_goal": 0, "bookshelf": [], "history": [], "reminders": True, "last_read": datetime.now()}
            await users_col.insert_one(user_profile)

        bookshelf = user_profile.get("bookshelf", [])
        history = user_profile.get("history", [])
        book_exists = False
        
        for book in bookshelf:
            if book["book_id"] == self.book_id:
                book["status"] = status
                book_exists = True
                break
        
        if not book_exists:
            bookshelf.append({
                "book_id": self.book_id, "title": self.title, "status": status,
                "current_page": 0, "total_pages": self.total_pages, "rating": None, "review": None
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
        await interaction.response.send_message(f"📖 Started reading **{self.title}**! Use `/progress` to update pages.", ephemeral=True)

    @discord.ui.button(label="Mark as Read", style=discord.ButtonStyle.secondary, emoji="✅")
    async def completed(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self.update_bookshelf(interaction.user.id, interaction.user.name, "completed")
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
        await interaction.response.send_message(f"👥 **{interaction.user.display_name}** successfully joined the room to read **{group['book_title']}**!", inline=False)


# 7. AUTOCOMPLETE FUNCTIONS
async def book_autocomplete(interaction: discord.Interaction, current: str):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    if not user_profile or "bookshelf" not in user_profile: return []
    return [app_commands.Choice(name=b["title"], value=b["title"]) for b in user_profile["bookshelf"] if current.lower() in b["title"].lower()][:25]

async def reading_autocomplete(interaction: discord.Interaction, current: str):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})
    if not user_profile or "bookshelf" not in user_profile: return []
    return [app_commands.Choice(name=b["title"], value=b["title"]) for b in user_profile["bookshelf"] if b["status"] == "reading" and current.lower() in b["title"].lower()][:25]

async def buddy_autocomplete(interaction: discord.Interaction, current: str):
    cursor = buddies_col.find()
    groups = await cursor.to_list(length=25)
    return [app_commands.Choice(name=f"{g['book_title']} (Host: {g['host_name']})", value=g["_id"]) for g in groups if current.lower() in g["book_title"].lower()][:25]


@bot.event
async def on_ready():
    await bot.tree.sync()
    if not daily_reminder_loop.is_running():
        daily_reminder_loop.start()
    print(f"Logged in as {bot.user} and all premium systems are deployed!")


# 8. ALL COMMANDS (SISTEMA INTEGRADO)

@bot.tree.command(name="help", description="How to use the Reading Bot")
async def help_command(interaction: discord.Interaction):
    embed = discord.Embed(title="📖 Master Reading Guide", description="Welcome! Complete suite for managing your library:", color=0x50e3c2)
    embed.add_field(name="1️⃣ Tracking Core", value="`/search` — Find books & add to collections.\n`/progress` — Track pages read & review.\n`/profile` — Stats and Badges.\n`/library` — Complete virtual shelf.", inline=False)
    embed.add_field(name="2️⃣ Advanced & Social Features", value="`/buddyread_create` — Start group reading.\n`/buddyread_status` — Compare pages in a group.\n`/quote add` — Save iconic phrases.\n`/quote random` — Get inspiration.\n`/history` — Your complete timeline.", inline=False)
    embed.add_field(name="3️⃣ Configuration", value="`/reminders` — Toggle daily automated pings.", inline=False)
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
                thumbnail = volume_info.get("imageLinks", {}).get("thumbnail", "").replace("http://", "https://")

                embed = discord.Embed(title=f"📖 {book_title}", description=f"**Author(s):** {authors}\n**Pages:** {pages if pages > 0 else '???'}\n\n*{description}*", color=0x4a90e2)
                if thumbnail: embed.set_thumbnail(url=thumbnail)
                await interaction.followup.send(embed=embed, view=BookshelfButtons(book_id, book_title, pages))
    except Exception:
        await interaction.followup.send("An unexpected error occurred while searching.")


@bot.tree.command(name="progress", description="Update your page progress for your active book")
@app_commands.autocomplete(book_title=reading_autocomplete)
async def progress(interaction: discord.Interaction, book_title: str, page: int):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})

    if not user_profile or not user_profile.get("bookshelf"):
        await interaction.response.send_message("❌ Your bookshelf is empty!", ephemeral=True)
        return

    current_book = next((b for b in user_profile["bookshelf"] if b["title"].lower() == book_title.lower() and b["status"] == "reading"), None)
    if not current_book:
        await interaction.response.send_message("❌ Book is not actively marked as 'Currently Reading'.", ephemeral=True)
        return

    total_pages = current_book["total_pages"]
    if page < 0 or (total_pages > 0 and page > total_pages):
        await interaction.response.send_message("❌ Invalid page count!", ephemeral=True)
        return

    # Cálculos de tempo para Medidor de Conquistas (Night Owl / Speedrunner)
    now = datetime.now()
    history = user_profile.get("history", [])
    history.append(f"📈 Logged page **{page}** on *{current_book['title']}*")

    current_book["current_page"] = page
    percentage = round((page / total_pages) * 100) if total_pages > 0 else 100

    if total_pages > 0 and page == total_pages:
        current_book["status"] = "completed"
        await users_col.update_one({"_id": user_id}, {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, "last_read": now}})
        view = RatingView(current_book["book_id"], current_book["title"])
        await interaction.response.send_message(f"🎉 You finished *{current_book['title']}*!", view=view)
    else:
        await users_col.update_one({"_id": user_id}, {"$set": {"bookshelf": user_profile["bookshelf"], "history": history, "last_read": now}})
        filled_blocks = round(percentage / 10)
        progress_bar = "⬛" * filled_blocks + "⬜" * (10 - filled_blocks)
        await interaction.response.send_message(f"📖 **{interaction.user.display_name}** made progress on *{current_book['title']}*!\n> `{progress_bar}` **{percentage}%** (Page {page}/{total_pages})")


@bot.tree.command(name="profile", description="Check your reading profile card, stats and unlocked achievements")
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

    # 🎖️ SISTEMA DINÂMICO DE BADGES / CONQUISTAS
    badges = []
    if len(completed) >= 1: badges.append("✨ **First Step:** Finished your first book!")
    if len(completed) >= 5: badges.append("📚 **Bookworm:** Finished 5 books total.")
    
    # Conferir se leu algum calhamaço (+400 pág)
    if any(b for b in completed if b.get("total_pages", 0) >= 400):
        badges.append("🐉 **Leviathan Slayer:** Read a book with 400+ pages.")

    embed.add_field(name="🎖️ Unlocked Achievements", value="\n".join(badges) if badges else "*No badges earned yet. Start reading!*", inline=False)
    await interaction.response.send_message(embed=embed)


@bot.tree.command(name="history", description="See your detailed physical timeline of reading activity")
async def history(interaction: discord.Interaction):
    user_id = str(interaction.user.id)
    user_profile = await users_col.find_one({"_id": user_id})

    if not user_profile or not user_profile.get("history"):
        await interaction.response.send_message("❌ You have no recorded reading history logs yet.", ephemeral=True)
        return

    paginator = PaginatorView(title="📅 Your Personal Reading Timeline", data_list=user_profile["history"][::-1], color=0x4a90e2, items_per_page=6)
    await interaction.response.send_message(embed=paginator.get_embed(), view=paginator)


# 9. FEATURE: CITAS FAVORITAS (/QUOTE)
quote_group = app_commands.Group(name="quote", description="Manage iconic book quotes and fragments")

@quote_group.command(name="add", description="Save a remarkable quote from a book")
async def quote_add(interaction: discord.Interaction, book_title: str, text: str):
    quote_doc = {
        "user_id": str(interaction.user.id),
        "username": interaction.user.name,
        "book_title": book_title,
        "text": text,
        "date_added": datetime.now()
    }
    await quotes_col.insert_one(quote_doc)
    await interaction.response.send_message(f"📝 Quote successfully added to the server archives from **{book_title}**!", ephemeral=True)

@quote_group.command(name="random", description="Summon a random iconic quote saved by the community")
async def quote_random(interaction: discord.Interaction):
    cursor = quotes_col.find()
    all_quotes = await cursor.to_list(length=500)
    
    if not all_quotes:
        await interaction.response.send_message("❌ No quotes have been saved in this server yet. Use `/quote add` to store some!")
        return
        
    q = random.choice(all_quotes)
    embed = discord.Embed(description=f"> *\"{q['text']}\"*", color=0x9013fe)
    embed.set_footer(text=f"📖 {q['book_title']} — Shared by {q['username']}")
    await interaction.response.send_message(embed=embed)

bot.tree.add_command(quote_group)


# 10. FEATURE: LEITURAS CONJUNTAS (/BUDDYREAD)
buddy_group = app_commands.Group(name="buddyread", description="Read books synchronized with your group")

@buddy_group.command(name="create", description="Launch a public reading club event for a specific book")
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
                if match_book["status"] == "completed": page_info = "✅ Completed!"
                else: page_info = f"Page `{match_book['current_page']}/{match_book['total_pages']}`"
                
        rows.append(f"• **{name}**: {page_info}")

    embed = discord.Embed(title=f"📊 Status Room: {group['book_title']}", description="\n".join(rows), color=0x7ed321)
    await interaction.followup.send(embed=embed)

bot.tree.add_command(buddy_group)


# 11. FEATURE: CONFIG DE LEMBRETES E PROMPT DE ALERTA DIÁRIO DO BOT
@bot.tree.command(name="reminders", description="Enable or disable active daily check-in book tracking messages")
async def reminders_toggle(interaction: discord.Interaction, enabled: bool):
    user_id = str(interaction.user.id)
    await users_col.update_one({"_id": user_id}, {"$set": {"reminders": enabled}}, upsert=True)
    status_text = "enabled" if enabled else "disabled"
    await interaction.response.send_message(f"🔔 Automated reading check-in alerts are now **{status_text}** for your user profile.", ephemeral=True)


@tasks.loop(hours=24)
async def daily_reminder_loop():
    """Roda a cada 24h procurando usuários inativos há mais de 2 dias"""
    cursor = users_col.find({"reminders": True})
    all_users = await cursor.to_list(length=1000)
    
    now = datetime.now()
    for u in all_users:
        last_read = u.get("last_read")
        if last_read and (now - last_read) > timedelta(days=2):
            # Encontrar o livro que ele está ativamente lendo
            reading_books = [b for b in u.get("bookshelf", []) if b["status"] == "reading"]
            if reading_books:
                book = reading_books[0]
                try:
                    discord_user = await bot.fetch_user(int(u["_id"]))
                    if discord_user:
                        await discord_user.send(f"👋 Hey {discord_user.display_name}! We noticed you haven't logged pages for **{book['title']}** in a couple of days. How about reading at least 5 pages today to crush your challenge? 📖✨")
                except Exception:
                    pass  # Evita quebrar a tarefa caso a DM do usuário seja fechada


# REUTILIZAÇÃO DE COMANDOS BASE DO PACOTE ANTERIOR (Com correções de compatibilidade)
@bot.tree.command(name="challenge", description="Set your reading goal for this year")
async def challenge(interaction: discord.Interaction, books_goal: int):
    user_id = str(interaction.user.id)
    current_year = datetime.now().year
    await users_col.update_one({"_id": user_id}, {"$set": {"yearly_goal": books_goal, "username": interaction.user.name}}, upsert=True)
    await interaction.response.send_message(f"🏆 Your {current_year} Reading Challenge has been set to **{books_goal}** books!", ephemeral=True)

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
    tbr_list = [f"▪️ {b['title']}" for b in bookshelf if b["status"] == "to_read"]
    embed = discord.Embed(title=f"📖 {target_user.display_name}'s Book Collections", color=0x7ed321)
    embed.add_field(name="⚡ Currently Reading", value="\n".join(reading_list) if reading_list else "*No books in progress.*", inline=False)
    embed.add_field(name="✨ Completed Books", value="\n".join(completed_list[:15]) if completed_list else "*No completed books yet.*", inline=False)
    embed.add_field(name="📌 To Be Read (TBR)", value="\n".join(tbr_list[:15]) if tbr_list else "*TBR collection is empty.*", inline=False)
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
if __name__ == "__main__":
    bot.run(DISCORD_TOKEN)
