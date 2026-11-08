import os
import random
import sqlite3
import discord

from discord.ext import commands
from discord import app_commands
from dotenv import load_dotenv

load_dotenv()


# ==========================================
# CONFIGURATION
# ==========================================

TOKEN = os.getenv("DISCORD_TOKEN")

EMOJIS = [

    "🔥", "⚡", "💥", "☄️", "🌋", "✨", "💫", "🌟",
    "🌙", "🌑", "🌕", "🪐", "🚀", "👽", "🛸", "⭐",
    "👑", "💎", "💍", "🏆", "🥇", "💰", "🤑", "🪙",
    "🌸", "🌺", "🌻", "🌹", "🌷", "🍀", "🌴", "🍁",
    "🍄", "🌿", "🌵", "🌼", "🪷", "🌳",
    "🐉", "🐲", "🦋", "🦇", "🐺", "🦊", "🐼", "🐸",
    "🐍", "🦈", "🐬", "🦁", "🐯", "🐻", "🦄", "🐧",
    "🎮", "🕹️", "👾", "🎯", "🏹", "⚔️", "🛡️", "🎲",
    "❤️", "🧡", "💛", "💚", "💙", "💜", "🖤", "🤍",
    "🤎", "🩷", "🩵", "💖", "💗", "💓", "💝",
    "🎧", "🎵", "🎶", "🎼", "🎤", "🎹", "🎸", "🥁",
    "🌈", "🌪️", "❄️", "☀️", "🌧️", "⛈️", "🌊", "🌬️",
    "🍓", "🍒", "🍉", "🍇", "🍍", "🥑", "🍕", "🍭",
    "♠️", "♥️", "♦️", "♣️", "🔮", "🧿", "☯️", "♾️",
    "🎭", "🎪", "🎡", "🎆", "🎇", "🪄", "🧸", "🎀",
    "🪽", "🪼", "🦚", "🪸", "🧩", "🔱", "💠", "🔔"
]

# ==========================================
# DATABASE
# ==========================================

db = sqlite3.connect("join_to_create.db")
db.row_factory = sqlite3.Row

db.execute("""
CREATE TABLE IF NOT EXISTS configs (
    guild_id INTEGER PRIMARY KEY,
    trigger_id INTEGER,
    category_id INTEGER
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS temp_channels (
    channel_id INTEGER PRIMARY KEY,
    guild_id INTEGER,
    owner_id INTEGER,
    emoji TEXT,
    locked INTEGER DEFAULT 0
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS trusted (
    channel_id INTEGER,
    user_id INTEGER,
    PRIMARY KEY (channel_id, user_id)
)
""")

db.execute("""
CREATE TABLE IF NOT EXISTS saved_nicknames (
    guild_id INTEGER,
    user_id INTEGER,
    original_nick TEXT,
    PRIMARY KEY (guild_id, user_id)
)
""")

db.commit()


def get_channel(channel_id):
    return db.execute(
        "SELECT * FROM temp_channels WHERE channel_id = ?",
        (channel_id,)
    ).fetchone()


def get_config(guild_id):
    return db.execute(
        "SELECT * FROM configs WHERE guild_id = ?",
        (guild_id,)
    ).fetchone()


def get_trusted(channel_id):
    rows = db.execute(
        "SELECT user_id FROM trusted WHERE channel_id = ?",
        (channel_id,)
    ).fetchall()

    return [row["user_id"] for row in rows]


def remove_channel_data(channel_id):
    db.execute(
        "DELETE FROM temp_channels WHERE channel_id = ?",
        (channel_id,)
    )
    db.execute(
        "DELETE FROM trusted WHERE channel_id = ?",
        (channel_id,)
    )
    db.commit()


# ==========================================
# BOT SETUP
# ==========================================

intents = discord.Intents.default()
intents.members = True
intents.voice_states = True
intents.message_content = True  # needed for the !leaderboard prefix command

bot = commands.Bot(
    command_prefix="!",
    intents=intents
)


@bot.event
async def on_ready():
    print(f"Logged in as {bot.user}")

    if not getattr(bot, "synced", False):
        await bot.tree.sync()
        bot.synced = True

    print("Join to Create bot is ready!")


# ==========================================
# NICKNAME SYSTEM
# ==========================================

async def change_nickname(member, emoji):

    row = db.execute("""
        SELECT original_nick
        FROM saved_nicknames
        WHERE guild_id = ? AND user_id = ?
    """, (member.guild.id, member.id)).fetchone()

    if row is None:
        original = member.nick

        db.execute("""
            INSERT INTO saved_nicknames
            (guild_id, user_id, original_nick)
            VALUES (?, ?, ?)
        """, (member.guild.id, member.id, original))

        db.commit()

    else:
        original = row["original_nick"]

    # Use the original nickname, not the emoji nickname.
    base_name = original if original else member.name

    # Format: 🔥 PlayerName 🔥
    available_length = 32 - (len(emoji) * 2) - 2
    base_name = base_name[:max(1, available_length)]

    new_nickname = f"{emoji} {base_name} {emoji}"

    try:
        await member.edit(
            nick=new_nickname,
            reason="Joined a temporary voice channel"
        )
    except discord.Forbidden:
        print(f"Cannot change nickname for {member}")
    except discord.HTTPException as error:
        print(f"Nickname error: {error}")


async def restore_nickname(member):

    row = db.execute("""
        SELECT original_nick
        FROM saved_nicknames
        WHERE guild_id = ? AND user_id = ?
    """, (member.guild.id, member.id)).fetchone()

    if row is None:
        return

    try:
        await member.edit(
            nick=row["original_nick"],
            reason="Left a temporary voice channel"
        )

        db.execute("""
            DELETE FROM saved_nicknames
            WHERE guild_id = ? AND user_id = ?
        """, (member.guild.id, member.id))

        db.commit()

    except discord.Forbidden:
        print(f"Cannot restore nickname for {member}")
    except discord.HTTPException as error:
        print(f"Restore error: {error}")


# ==========================================
# VOICE PERMISSIONS
# ==========================================

async def apply_channel_permissions(channel, owner_id, locked):

    guild = channel.guild

    # Lock/unlock affects CONNECT only.
    # VIEW_CHANNEL is never changed.

    everyone_overwrite = channel.overwrites_for(
        guild.default_role
    )

    everyone_overwrite.connect = False if locked else None

    await channel.set_permissions(
        guild.default_role,
        overwrite=everyone_overwrite
    )

    # Owner can always connect.
    owner = guild.get_member(owner_id)

    if owner:
        owner_overwrite = channel.overwrites_for(owner)
        owner_overwrite.connect = True

        await channel.set_permissions(
            owner,
            overwrite=owner_overwrite
        )

    # Trusted members can connect when locked.
    for user_id in get_trusted(channel.id):

        member = guild.get_member(user_id)

        if member and locked:
            overwrite = channel.overwrites_for(member)
            overwrite.connect = True

            await channel.set_permissions(
                member,
                overwrite=overwrite
            )


# ==========================================
# VOICE CHANNEL EVENTS
# ==========================================

@bot.event
async def on_voice_state_update(member, before, after):

    if member.bot:
        return

    guild = member.guild

    config = get_config(guild.id)

    before_channel = before.channel
    after_channel = after.channel

    before_data = (
        get_channel(before_channel.id)
        if before_channel else None
    )

    after_data = (
        get_channel(after_channel.id)
        if after_channel else None
    )

    # --------------------------------------
    # JOIN TO CREATE
    # --------------------------------------

    if config and after_channel:

        if after_channel.id == config["trigger_id"]:

            emoji = random.choice(EMOJIS)

            category = (
                guild.get_channel(config["category_id"])
                if config["category_id"]
                else after_channel.category
            )

            try:
                new_channel = await guild.create_voice_channel(
                    name=f"{emoji}・{member.display_name}",
                    category=category,
                    overwrites={
                        member: discord.PermissionOverwrite(
                            connect=True
                        )
                    },
                    reason="Join to Create"
                )

                db.execute("""
                    INSERT INTO temp_channels
                    (channel_id, guild_id, owner_id, emoji, locked)
                    VALUES (?, ?, ?, ?, 0)
                """, (
                    new_channel.id,
                    guild.id,
                    member.id,
                    emoji
                ))

                db.commit()

                await member.move_to(new_channel)

                print(f"Created channel: {new_channel.name}")

            except discord.Forbidden:
                print("Missing permissions to create or move members.")
            except discord.HTTPException as error:
                print(f"Channel creation error: {error}")

            # No early return here: if the member came from another
            # temporary channel, the cleanup below must still delete it
            # when it is empty. The nickname is not restored because the
            # is_trigger check below skips it.

    # --------------------------------------
    # MEMBER JOINS A TEMPORARY CHANNEL
    # --------------------------------------

    if after_data:

        await change_nickname(
            member,
            after_data["emoji"]
        )

    # --------------------------------------
    # MEMBER LEAVES A TEMPORARY CHANNEL
    # --------------------------------------

    elif before_data:

        # Moving to the trigger channel means a new
        # temporary channel is about to be created.
        is_trigger = (
            config
            and after_channel
            and after_channel.id == config["trigger_id"]
        )

        if not is_trigger:
            await restore_nickname(member)

    # --------------------------------------
    # DELETE EMPTY CHANNEL
    # --------------------------------------

    if before_data and before_channel:

        if len(before_channel.members) == 0:

            remove_channel_data(before_channel.id)

            try:
                await before_channel.delete(
                    reason="Temporary voice channel is empty"
                )

                print("Deleted empty temporary channel.")

            except discord.NotFound:
                pass
            except discord.Forbidden:
                print("Cannot delete temporary channel.")
            except discord.HTTPException as error:
                print(f"Delete error: {error}")


# ==========================================
# HELPER: GET OWNER'S CURRENT CHANNEL
# ==========================================

async def get_owned_channel(interaction):

    if not interaction.guild:
        await interaction.response.send_message(
            "This command only works in a server.",
            ephemeral=True
        )
        return None

    if not interaction.user.voice:
        await interaction.response.send_message(
            "You must be in your temporary voice channel.",
            ephemeral=True
        )
        return None

    channel = interaction.user.voice.channel
    data = get_channel(channel.id)

    if not data:
        await interaction.response.send_message(
            "This is not a temporary voice channel.",
            ephemeral=True
        )
        return None

    if data["owner_id"] != interaction.user.id:
        await interaction.response.send_message(
            "Only the channel owner can use this command.",
            ephemeral=True
        )
        return None

    return channel


# ==========================================
# SETUP COMMAND
# ==========================================

@bot.tree.command(
    name="setup",
    description="Set up the Join to Create system."
)
@app_commands.checks.has_permissions(manage_guild=True)
async def setup(
    interaction: discord.Interaction,
    trigger: discord.VoiceChannel,
    category: discord.CategoryChannel = None
):

    if category is None:
        category = trigger.category

    db.execute("""
        INSERT OR REPLACE INTO configs
        (guild_id, trigger_id, category_id)
        VALUES (?, ?, ?)
    """, (
        interaction.guild.id,
        trigger.id,
        category.id if category else None
    ))

    db.commit()

    await interaction.response.send_message(
        f"Join to Create configured!\n"
        f"Trigger: {trigger.mention}\n"
        f"Category: {category.name if category else 'None'}",
        ephemeral=True
    )


# ==========================================
# LOCK COMMAND
# ==========================================

@bot.tree.command(
    name="lock",
    description="Lock your temporary voice channel."
)
async def lock(interaction: discord.Interaction):

    channel = await get_owned_channel(interaction)

    if not channel:
        return

    data = get_channel(channel.id)

    await apply_channel_permissions(
        channel,
        data["owner_id"],
        True
    )

    db.execute(
        "UPDATE temp_channels SET locked = 1 WHERE channel_id = ?",
        (channel.id,)
    )

    db.commit()

    await interaction.response.send_message(
        "🔒 Your voice channel is locked. Visibility is unchanged.",
        ephemeral=True
    )


# ==========================================
# UNLOCK COMMAND
# ==========================================

@bot.tree.command(
    name="unlock",
    description="Unlock your temporary voice channel."
)
async def unlock(interaction: discord.Interaction):

    channel = await get_owned_channel(interaction)

    if not channel:
        return

    data = get_channel(channel.id)

    await apply_channel_permissions(
        channel,
        data["owner_id"],
        False
    )

    db.execute(
        "UPDATE temp_channels SET locked = 0 WHERE channel_id = ?",
        (channel.id,)
    )

    db.commit()

    await interaction.response.send_message(
        "🔓 Your voice channel is unlocked!",
        ephemeral=True
    )


# ==========================================
# TRUST COMMAND
# ==========================================

@bot.tree.command(
    name="trust",
    description="Allow someone to join your locked channel."
)
async def trust(
    interaction: discord.Interaction,
    member: discord.Member
):

    channel = await get_owned_channel(interaction)

    if not channel:
        return

    if member.bot:
        await interaction.response.send_message(
            "You cannot trust a bot with this command.",
            ephemeral=True
        )
        return

    if member.id == interaction.user.id:
        await interaction.response.send_message(
            "You are already the channel owner.",
            ephemeral=True
        )
        return

    db.execute("""
        INSERT OR IGNORE INTO trusted
        (channel_id, user_id)
        VALUES (?, ?)
    """, (channel.id, member.id))

    db.commit()

    data = get_channel(channel.id)

    if data["locked"]:
        overwrite = channel.overwrites_for(member)
        overwrite.connect = True

        await channel.set_permissions(
            member,
            overwrite=overwrite
        )

    await interaction.response.send_message(
        f"🛡️ {member.mention} is now trusted!",
        ephemeral=True
    )


# ==========================================
# UNTRUST COMMAND
# ==========================================

@bot.tree.command(
    name="untrust",
    description="Remove someone's trusted access."
)
async def untrust(
    interaction: discord.Interaction,
    member: discord.Member
):

    channel = await get_owned_channel(interaction)

    if not channel:
        return

    db.execute("""
        DELETE FROM trusted
        WHERE channel_id = ? AND user_id = ?
    """, (channel.id, member.id))

    db.commit()

    # Remove only the explicit CONNECT permission.
    overwrite = channel.overwrites_for(member)
    overwrite.connect = None

    await channel.set_permissions(
        member,
        overwrite=overwrite
    )

    await interaction.response.send_message(
        f"❌ {member.mention} is no longer trusted.",
        ephemeral=True
    )


# ==========================================
# RENAME COMMAND
# ==========================================

@bot.tree.command(
    name="name",
    description="Change your temporary voice channel name."
)
async def rename(
    interaction: discord.Interaction,
    name: app_commands.Range[str, 1, 85]
):

    channel = await get_owned_channel(interaction)

    if not channel:
        return

    data = get_channel(channel.id)

    new_name = f"{data['emoji']}・{name.strip()}"
    await channel.edit(name=new_name[:100])

    await interaction.response.send_message(
        f"Your channel is now named **{new_name}**.",
        ephemeral=True
    )


# ==========================================
# USER LIMIT COMMAND
# ==========================================

@bot.tree.command(
    name="limit",
    description="Set your temporary voice channel user limit."
)
async def limit(
    interaction: discord.Interaction,
    number: app_commands.Range[int, 0, 99]
):

    channel = await get_owned_channel(interaction)

    if not channel:
        return

    await channel.edit(user_limit=number)

    message = (
        "Unlimited users."
        if number == 0
        else f"Maximum users: **{number}**."
    )

    await interaction.response.send_message(
        f"👥 {message}",
        ephemeral=True
    )


# ==========================================
# TRANSFER OWNERSHIP
# ==========================================

@bot.tree.command(
    name="transfer",
    description="Transfer your channel ownership."
)
async def transfer(
    interaction: discord.Interaction,
    member: discord.Member
):

    channel = await get_owned_channel(interaction)

    if not channel:
        return

    if member.bot or member not in channel.members:
        await interaction.response.send_message(
            "The new owner must be a human member in your voice channel.",
            ephemeral=True
        )
        return

    db.execute("""
        UPDATE temp_channels
        SET owner_id = ?
        WHERE channel_id = ?
    """, (member.id, channel.id))

    db.commit()

    data = get_channel(channel.id)

    # Remove the old owner's special connection permission
    # unless they are trusted.
    if interaction.user.id not in get_trusted(channel.id):
        old_overwrite = channel.overwrites_for(interaction.user)
        old_overwrite.connect = None

        await channel.set_permissions(
            interaction.user,
            overwrite=old_overwrite
        )

    await apply_channel_permissions(
        channel,
        member.id,
        bool(data["locked"])
    )

    await interaction.response.send_message(
        f"👑 Ownership transferred to {member.mention}!",
        ephemeral=True
    )


# ==========================================
# CLAIM OWNERSHIP
# ==========================================

@bot.tree.command(
    name="claim",
    description="Claim an abandoned temporary voice channel."
)
async def claim(interaction: discord.Interaction):

    if not interaction.user.voice:
        await interaction.response.send_message(
            "Join a temporary voice channel first.",
            ephemeral=True
        )
        return

    channel = interaction.user.voice.channel
    data = get_channel(channel.id)

    if not data:
        await interaction.response.send_message(
            "This is not a temporary voice channel.",
            ephemeral=True
        )
        return

    old_owner = channel.guild.get_member(data["owner_id"])

    if old_owner and old_owner in channel.members:
        await interaction.response.send_message(
            "The current owner is still in the channel.",
            ephemeral=True
        )
        return

    db.execute("""
        UPDATE temp_channels
        SET owner_id = ?
        WHERE channel_id = ?
    """, (interaction.user.id, channel.id))

    db.commit()

    await apply_channel_permissions(
        channel,
        interaction.user.id,
        bool(data["locked"])
    )

    await interaction.response.send_message(
        "👑 You are now the channel owner!",
        ephemeral=True
    )


# ==========================================
# DELETE COMMAND
# ==========================================

@bot.tree.command(
    name="delete",
    description="Delete your temporary voice channel."
)
async def delete(interaction: discord.Interaction):

    channel = await get_owned_channel(interaction)

    if not channel:
        return

    members = list(channel.members)

    # Restore all nicknames before deleting.
    for member in members:
        await restore_nickname(member)

    remove_channel_data(channel.id)

    await channel.delete(
        reason="Channel owner requested deletion"
    )

    await interaction.response.send_message(
        "🗑️ Your temporary voice channel was deleted.",
        ephemeral=True
    )


# ==========================================
# STATUS COMMAND
# ==========================================

@bot.tree.command(
    name="status",
    description="Show your temporary voice channel information."
)
async def status(interaction: discord.Interaction):

    channel = await get_owned_channel(interaction)

    if not channel:
        return

    data = get_channel(channel.id)

    trusted_users = get_trusted(channel.id)

    embed = discord.Embed(
        title="Voice Channel Status",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="Channel",
        value=channel.mention,
        inline=False
    )

    embed.add_field(
        name="Emoji",
        value=data["emoji"],
        inline=True
    )

    embed.add_field(
        name="Lock",
        value="🔒 Locked" if data["locked"] else "🔓 Unlocked",
        inline=True
    )

    embed.add_field(
        name="Members",
        value=str(len(channel.members)),
        inline=True
    )

    embed.add_field(
        name="Trusted users",
        value=str(len(trusted_users)),
        inline=True
    )

    embed.add_field(
        name="User limit",
        value=str(channel.user_limit or "Unlimited"),
        inline=True
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ==========================================
# HELP COMMAND
# ==========================================

@bot.tree.command(
    name="help",
    description="Show all Join to Create commands."
)
async def help_command(interaction: discord.Interaction):

    embed = discord.Embed(
        title="Join to Create Commands",
        description="Manage your temporary voice channel.",
        color=discord.Color.blurple()
    )

    embed.add_field(
        name="🔒 Permissions",
        value=(
            "`/lock` - Lock your channel\n"
            "`/unlock` - Unlock your channel\n"
            "`/trust @user` - Trust someone\n"
            "`/untrust @user` - Remove trust"
        ),
        inline=False
    )

    embed.add_field(
        name="⚙️ Management",
        value=(
            "`/name` - Rename channel\n"
            "`/limit` - Set user limit\n"
            "`/delete` - Delete channel\n"
            "`/status` - Channel information"
        ),
        inline=False
    )

    embed.add_field(
        name="👑 Ownership",
        value=(
            "`/claim` - Claim abandoned channel\n"
            "`/transfer @user` - Transfer ownership"
        ),
        inline=False
    )

    embed.add_field(
        name="🛠️ Administration",
        value="`/setup` - Configure Join to Create",
        inline=False
    )

    await interaction.response.send_message(
        embed=embed,
        ephemeral=True
    )


# ==========================================
# LEADERBOARD COMMAND (prefix: !leaderboard)
# ==========================================

@bot.command(name="leaderboard", aliases=["voicestats", "rooms"])
@commands.guild_only()
async def leaderboard(ctx):
    guild = ctx.guild

    # 1. Récupérer uniquement les salons vocaux qui ont au moins 1 personne connectée
    active_channels = [vc for vc in guild.voice_channels if len(vc.members) > 0]

    # Trier du plus actif (plus grand nombre de membres) au moins actif
    active_channels.sort(key=lambda vc: len(vc.members), reverse=True)

    # Nombre total de personnes en vocal sur tout le serveur
    total_in_voice = sum(len(vc.members) for vc in guild.voice_channels)

    # Création de l'embed principal (style "Command Deck")
    embed = discord.Embed(
        title="🎙️ ELITE VOICE • COMMAND DECK",
        description=f"Personnes connectées en vocal en ce moment : **{total_in_voice}**",
        color=discord.Color.from_rgb(111, 75, 186)  # Thème violet/sombre
    )

    if not active_channels:
        embed.add_field(
            name="Aucun salon actif",
            value="Il n'y a personne dans les salons vocaux actuellement.",
            inline=False
        )
        await ctx.send(embed=embed)
        return

    # 2. Afficher le top des salons actifs (par exemple, les 5 premiers)
    for i, vc in enumerate(active_channels[:5], start=1):
        member_count = len(vc.members)

        # Barre de progression textuelle (ex: 3 membres = 3 blocs pleins)
        bar_length = min(member_count, 10)
        progress_bar = "█" * bar_length + "░" * (10 - bar_length)

        # Liste des pseudos des membres connectés dans ce salon spécifique
        member_names = ", ".join([m.display_name for m in vc.members[:5]])
        if member_count > 5:
            member_names += f" *et {member_count - 5} de plus...*"

        # Vérifier si le salon est verrouillé (basé sur les permissions du rôle @everyone)
        is_locked = vc.permissions_for(guild.default_role).connect == False
        lock_emoji = "🔒" if is_locked else "🔓"

        field_value = (
            f"`{progress_bar}` {member_count} / ∞\n"
            f"👤 **Membres :** {member_names}"
        )

        embed.add_field(
            name=f"#{i:02d} | {vc.name} {lock_emoji}",
            value=field_value,
            inline=False
        )

    embed.set_footer(
        text=f"Demandé par {ctx.author.display_name} • Mise à jour en direct",
        icon_url=ctx.author.avatar.url if ctx.author.avatar else None
    )

    await ctx.send(embed=embed)


# ==========================================
# RUN BOT
# ==========================================

if not TOKEN:
    raise RuntimeError(
        "Set the DISCORD_TOKEN environment variable first."
    )

bot.run(TOKEN)
