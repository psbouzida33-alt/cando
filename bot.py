"""Private Voice Bot.

Python (discord.py) port of the original discord.js bot.

Members join the "➕ Join To Create" voice channel and the bot creates a
private room for them, with a control panel (buttons) to manage it. Each
room gets a random emoji that is shown on the nickname of everyone inside.
"""

from __future__ import annotations

import contextlib
import json
import logging
import os
import random
from dataclasses import dataclass, field
from pathlib import Path

import discord
from aiohttp import web
from discord import app_commands
from dotenv import load_dotenv

ROOMS_FILE = Path("rooms.json")
NICKS_FILE = Path("nicknames.json")
TRIGGER_NAME = "➕ Join To Create"
EMBED_COLOR = 0x8B5CF6
NICK_MAX = 32  # Discord's nickname length limit

# Random emoji given to every new room (same list as join_to_create.py).
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
    "🪽", "🪼", "🦚", "🪸", "🧩", "🔱", "💠", "🔔",
]

log = logging.getLogger("private-voice")


# --------------------------------------------------------------------------
# Room storage (rooms.json)
# --------------------------------------------------------------------------

@dataclass
class Room:
    owner_id: int
    trusted: list[int] = field(default_factory=list)
    locked: bool = False
    emoji: str = ""  # shown on the nickname of everyone inside ("" = none)

    def to_json(self) -> dict:
        return {
            "ownerId": str(self.owner_id),
            "trusted": [str(user_id) for user_id in self.trusted],
            "locked": self.locked,
            "emoji": self.emoji,
        }

    @classmethod
    def from_json(cls, data: dict) -> Room:
        return cls(
            owner_id=int(data["ownerId"]),
            trusted=[int(user_id) for user_id in data.get("trusted", [])],
            locked=bool(data.get("locked", False)),
            emoji=str(data.get("emoji", "")),
        )


rooms: dict[int, Room] = {}


def load_rooms() -> None:
    if not ROOMS_FILE.exists():
        return
    try:
        saved = json.loads(ROOMS_FILE.read_text(encoding="utf-8"))
        for channel_id, data in saved.items():
            rooms[int(channel_id)] = Room.from_json(data)
    except (OSError, ValueError, KeyError, TypeError):
        log.exception("Could not load rooms")


def save_rooms() -> None:
    payload = {str(channel_id): room.to_json() for channel_id, room in rooms.items()}
    tmp = ROOMS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2), encoding="utf-8")
    os.replace(tmp, ROOMS_FILE)  # atomic: never leaves a half-written file


# --------------------------------------------------------------------------
# Helpers
# --------------------------------------------------------------------------

async def resolve_member(guild: discord.Guild, user_id: int) -> discord.Member | None:
    member = guild.get_member(user_id)
    if member is None:
        try:
            member = await guild.fetch_member(user_id)
        except discord.NotFound:
            return None
    return member


async def report_error(interaction: discord.Interaction, error: Exception) -> None:
    log.error("Bot error", exc_info=error)
    message = "Something went wrong. Check bot permissions."
    with contextlib.suppress(discord.HTTPException):
        if interaction.response.is_done():
            await interaction.followup.send(message, ephemeral=True)
        else:
            await interaction.response.send_message(message, ephemeral=True)


def is_role_target(target: discord.abc.Snowflake) -> bool:
    """True for a role overwrite (a role missing from the cache shows up as a
    type-aware discord.Object, so check for that too)."""
    return isinstance(target, discord.Role) or (
        isinstance(target, discord.Object) and target.type is discord.Role
    )


async def update_permissions(channel: discord.VoiceChannel, room: Room) -> None:
    """Apply the room's state on top of the channel's existing permissions.

    Rooms are open by default. The bot only manages:
      * @everyone: View Channel (always allowed) and Connect (Lock button),
        set explicitly to allow/deny. Nothing else about @everyone changes.
      * the owner and the bot: full access.
      * trusted members: can always see and join, even when the room is
        locked.
    Every other overwrite (Speak, Video, Read Message History... inherited
    from the category, other roles) is left exactly as it is.
    """
    guild = channel.guild

    # Start from the current role overwrites, keyed by id so a target can't
    # appear twice. Member overwrites are deliberately NOT carried over: they
    # are rebuilt below, so an untrusted member or the previous owner loses
    # their special access.
    current = {
        target.id: (target, overwrite)
        for target, overwrite in channel.overwrites.items()
        if is_role_target(target)
    }

    def patch(target: discord.Role | discord.Member, **changes: bool) -> None:
        overwrite = channel.overwrites_for(target)  # fresh copy of what's there now
        overwrite.update(**changes)
        current[target.id] = (target, overwrite)

    patch(guild.default_role, view_channel=True, connect=not room.locked)

    for member_id in room.trusted:
        member = await resolve_member(guild, member_id)
        if member is not None:
            patch(member, view_channel=True, connect=True)

    # The owner and the bot always have full access (patched after the trusted
    # members so they win, e.g. after an ownership transfer).
    full_access = dict(
        view_channel=True,
        connect=True,
        manage_channels=True,
        move_members=True,
    )

    owner = await resolve_member(guild, room.owner_id)
    if owner is not None:
        patch(owner, **full_access)

    patch(guild.me, **full_access)

    await channel.edit(overwrites=dict(current.values()))


async def create_room(member: discord.Member, trigger: discord.VoiceChannel) -> None:
    guild = member.guild
    room = Room(owner_id=member.id, emoji=random.choice(EMOJIS))

    channel = await guild.create_voice_channel(
        name=f"{member.display_name}'s room"[:100],
        category=trigger.category,
    )

    rooms[channel.id] = room
    save_rooms()

    try:
        await update_permissions(channel, room)
        await member.move_to(channel)
    except Exception:
        # Don't leave an orphan channel behind (e.g. the member already left).
        rooms.pop(channel.id, None)
        save_rooms()
        with contextlib.suppress(discord.HTTPException):
            await channel.delete()
        raise

    embed = discord.Embed(
        color=EMBED_COLOR,
        title="🎙️ Private Voice Room",
        description=(
            f"Owner: <@{member.id}>\n\n"
            "Manage your room using the buttons below.\n\n"
            "🔒 Lock stops everyone else from joining.\n"
            "🛡️ Trust lets selected members join even when the room is locked."
        ),
    )
    await channel.send(embed=embed, view=PanelView())


# --------------------------------------------------------------------------
# Room emoji on nicknames
#
# While a member is inside a room their nickname shows the room's emoji on
# the left ("🔥 Ahmed"). When they leave, the original nickname comes back.
# The originals are kept in nicknames.json so a restart doesn't lose them.
# --------------------------------------------------------------------------

# user id -> nickname they had before the emoji (None = they had no nickname)
saved_nicks: dict[int, str | None] = {}


def load_nicks() -> None:
    if not NICKS_FILE.exists():
        return
    try:
        saved = json.loads(NICKS_FILE.read_text(encoding="utf-8"))
        for user_id, nick in saved.items():
            saved_nicks[int(user_id)] = nick
    except (OSError, ValueError, TypeError):
        log.exception("Could not load saved nicknames")


def save_nicks() -> None:
    payload = {str(user_id): nick for user_id, nick in saved_nicks.items()}
    tmp = NICKS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    os.replace(tmp, NICKS_FILE)


def utf16_len(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def emoji_nick(emoji: str, name: str) -> str:
    """'<emoji> <name>', with the name cut to fit Discord's nickname limit
    (measured in UTF-16 units, the strictest way to count)."""
    room_for_name = max(1, NICK_MAX - utf16_len(emoji) - 1)
    while len(name) > 1 and utf16_len(name) > room_for_name:
        name = name[:-1]
    return f"{emoji} {name}"


def is_trigger_channel(channel: discord.abc.GuildChannel | None) -> bool:
    return isinstance(channel, discord.VoiceChannel) and channel.name == TRIGGER_NAME


async def show_room_emoji(member: discord.Member, emoji: str) -> None:
    first_time = member.id not in saved_nicks
    if first_time:
        # Remember the real nickname BEFORE changing it, so nothing can lose it.
        saved_nicks[member.id] = member.nick
        save_nicks()

    original = saved_nicks[member.id]
    new_nick = emoji_nick(emoji, original or member.global_name or member.name)

    if member.nick == new_nick:
        return

    try:
        await member.edit(nick=new_nick, reason="Joined a private voice room")
    except discord.HTTPException as error:
        # Can't rename this member (server owner, higher role, missing
        # Manage Nicknames...). Don't track them: there is nothing to restore.
        log.warning("Cannot set nickname for %s: %s", member, error)
        if first_time:
            saved_nicks.pop(member.id, None)
            save_nicks()


async def restore_nickname(member: discord.Member) -> None:
    if member.id not in saved_nicks:
        return

    try:
        await member.edit(nick=saved_nicks[member.id], reason="Left the private voice room")
    except discord.NotFound:
        pass  # they left the server: nothing to restore
    except discord.Forbidden:
        log.warning("Cannot restore nickname for %s", member)
    except discord.HTTPException:
        log.exception("Could not restore nickname for %s", member)
        return  # keep it saved so a later restore can try again

    saved_nicks.pop(member.id, None)
    save_nicks()


async def sync_nickname(
    member: discord.Member, channel: discord.abc.GuildChannel | None
) -> None:
    """Run when a member's voice channel changes: show the room's emoji while
    they are in a room, put their own nickname back everywhere else."""
    room = rooms.get(channel.id) if channel is not None else None

    if room is not None and room.emoji:
        await show_room_emoji(member, room.emoji)
    elif not is_trigger_channel(channel):
        # (Waiting in the trigger channel is skipped: their room is being
        # created and they'll be moved straight into it.)
        await restore_nickname(member)


async def restore_stale_nicknames(guild: discord.Guild) -> None:
    """After a (re)start: give the nickname back to anyone who is no longer in
    a room, e.g. because they left while the bot was offline."""
    for user_id in list(saved_nicks):
        try:
            member = await resolve_member(guild, user_id)
            if member is None:  # no longer in the server
                saved_nicks.pop(user_id, None)
                continue

            channel = member.voice.channel if member.voice else None
            if channel is not None and (channel.id in rooms or is_trigger_channel(channel)):
                continue  # still inside a room

            await restore_nickname(member)
        except discord.HTTPException:
            log.exception("Could not check saved nickname of %s", user_id)

    save_nicks()


# --------------------------------------------------------------------------
# Member picker (trust / untrust / kick / transfer)
# --------------------------------------------------------------------------

PICKER_PROMPTS = {
    "trust": "🛡️ Select a member to trust.",
    "untrust": "❌ Select a trusted member to remove.",
    "kick": "➡️ Select a member to kick.",
    "transfer": "👑 Select the new owner.",
}


class MemberSelect(discord.ui.UserSelect):
    def __init__(self, action: str, room_id: int) -> None:
        super().__init__(placeholder="Select a server member", min_values=1, max_values=1)
        self.action = action
        self.room_id = room_id

    async def callback(self, interaction: discord.Interaction) -> None:
        room = rooms.get(self.room_id)

        if (
            room is None
            or interaction.user.id != room.owner_id
            or interaction.channel_id != self.room_id
        ):
            await interaction.response.send_message(
                "Only the room owner can do this.", ephemeral=True
            )
            return

        channel = interaction.channel
        target = self.values[0]

        if target.id == room.owner_id:
            await interaction.response.edit_message(
                content="You cannot select yourself.", view=None
            )
            return

        # Permission edits can be slow (rate limits): acknowledge first.
        await interaction.response.defer()

        member = await resolve_member(interaction.guild, target.id)
        if member is None:
            text = "Member not found in this server."

        elif self.action == "trust":
            if member.id not in room.trusted:
                room.trusted.append(member.id)
            await update_permissions(channel, room)
            save_rooms()
            text = f"🛡️ {member} is now trusted!"

        elif self.action == "untrust":
            if member.id not in room.trusted:
                text = "This member is not trusted."
            else:
                room.trusted.remove(member.id)
                await update_permissions(channel, room)
                save_rooms()
                text = f"❌ {member} is no longer trusted."

        elif self.action == "kick":
            in_room = member.voice is not None and member.voice.channel is not None \
                and member.voice.channel.id == self.room_id
            if not in_room:
                text = "This member is not in your room."
            else:
                await member.move_to(None)
                text = f"➡️ {member} was kicked."

        elif self.action == "transfer":
            if member.id not in room.trusted:
                text = "Trust this member before transferring ownership."
            else:
                room.owner_id = member.id
                await update_permissions(channel, room)
                save_rooms()
                text = f"👑 {member} is now the room owner."

        else:  # unknown action — should never happen
            text = "Unknown action."

        await interaction.edit_original_response(content=text, view=None)


class MemberSelectView(discord.ui.View):
    def __init__(self, action: str, room_id: int) -> None:
        super().__init__(timeout=300)
        self.add_item(MemberSelect(action, room_id))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await report_error(interaction, error)


# --------------------------------------------------------------------------
# Modals (rename / user limit)
# --------------------------------------------------------------------------

MODAL_TITLES = {
    "rename": "Rename Room",
    "limit": "Change User Limit",
}

MODAL_LABELS = {
    "rename": "New room name",
    "limit": "Maximum users (0-99)",
}


class RoomModal(discord.ui.Modal):
    def __init__(self, action: str) -> None:
        super().__init__(title=MODAL_TITLES[action])
        self.action = action
        self.value = discord.ui.TextInput(
            style=discord.TextStyle.short,
            required=True,
        )
        self.add_item(discord.ui.Label(text=MODAL_LABELS[action], component=self.value))

    async def on_submit(self, interaction: discord.Interaction) -> None:
        room = rooms.get(interaction.channel_id)

        if room is None or interaction.user.id != room.owner_id:
            await interaction.response.send_message(
                "Only the room owner can manage this room.", ephemeral=True
            )
            return

        channel = interaction.channel
        value = self.value.value.strip()

        if self.action == "rename":
            if not value:
                await interaction.response.send_message("Enter a room name.", ephemeral=True)
                return
            await interaction.response.defer(ephemeral=True)
            await channel.edit(name=value[:100])

        else:  # limit
            try:
                limit = int(value)
            except ValueError:
                limit = -1
            if not 0 <= limit <= 99:
                await interaction.response.send_message(
                    "Enter a number from 0 to 99.", ephemeral=True
                )
                return
            await interaction.response.defer(ephemeral=True)
            await channel.edit(user_limit=limit)

        await interaction.followup.send("Room settings updated!", ephemeral=True)

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_error(interaction, error)


# --------------------------------------------------------------------------
# Control panel
# --------------------------------------------------------------------------

class PanelView(discord.ui.View):
    """Buttons attached to every room. Persistent (fixed custom_ids), so the
    panels keep working after the bot restarts."""

    def __init__(self) -> None:
        super().__init__(timeout=None)

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        room = rooms.get(interaction.channel_id)

        if room is None:
            await interaction.response.send_message("Room unavailable.", ephemeral=True)
            return False

        if interaction.user.id != room.owner_id:
            await interaction.response.send_message(
                "Only the voice owner can use this panel.", ephemeral=True
            )
            return False

        return True

    async def on_error(self, interaction: discord.Interaction, error: Exception, item) -> None:
        await report_error(interaction, error)

    # ---- row 1 ----

    @discord.ui.button(label="🔒 Lock / Unlock", style=discord.ButtonStyle.secondary,
                       custom_id="lock", row=0)
    async def lock_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        room = rooms[interaction.channel_id]
        await interaction.response.defer(ephemeral=True)

        room.locked = not room.locked
        await update_permissions(interaction.channel, room)
        save_rooms()

        await interaction.followup.send(
            "🔒 Room locked. Only you and trusted members can join."
            if room.locked
            else "🔓 Room unlocked. Everyone can join.",
            ephemeral=True,
        )

    @discord.ui.button(label="✏️ Rename", style=discord.ButtonStyle.primary,
                       custom_id="rename", row=0)
    async def rename_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RoomModal("rename"))

    @discord.ui.button(label="👥 User Limit", style=discord.ButtonStyle.secondary,
                       custom_id="limit", row=0)
    async def limit_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await interaction.response.send_modal(RoomModal("limit"))

    # ---- row 2 ----

    @discord.ui.button(label="🛡️ Trust", style=discord.ButtonStyle.success,
                       custom_id="trust", row=1)
    async def trust_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick_member(interaction, "trust")

    @discord.ui.button(label="❌ Untrust", style=discord.ButtonStyle.secondary,
                       custom_id="untrust", row=1)
    async def untrust_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick_member(interaction, "untrust")

    @discord.ui.button(label="➡️ Kick", style=discord.ButtonStyle.danger,
                       custom_id="kick", row=1)
    async def kick_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick_member(interaction, "kick")

    @discord.ui.button(label="👑 Transfer", style=discord.ButtonStyle.primary,
                       custom_id="transfer", row=1)
    async def transfer_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        await self._pick_member(interaction, "transfer")

    @discord.ui.button(label="🗑️ Delete", style=discord.ButtonStyle.danger,
                       custom_id="delete", row=1)
    async def delete_button(self, interaction: discord.Interaction, button: discord.ui.Button):
        channel = interaction.channel
        members = list(channel.members)
        rooms.pop(channel.id, None)
        save_rooms()

        await interaction.response.send_message("Room deleted!", ephemeral=True)

        # Give everyone inside their nickname back before the channel goes.
        for member in members:
            await restore_nickname(member)

        await channel.delete()

    @staticmethod
    async def _pick_member(interaction: discord.Interaction, action: str) -> None:
        await interaction.response.send_message(
            PICKER_PROMPTS[action],
            view=MemberSelectView(action, interaction.channel_id),
            ephemeral=True,
        )


# --------------------------------------------------------------------------
# /setup command
# --------------------------------------------------------------------------

@app_commands.command(name="setup", description="Create Join To Create")
@app_commands.guild_only()
async def setup_command(interaction: discord.Interaction) -> None:
    if not interaction.permissions.administrator:
        await interaction.response.send_message(
            "Administrator permission required.", ephemeral=True
        )
        return

    existing = discord.utils.get(interaction.guild.voice_channels, name=TRIGGER_NAME)
    if existing is not None:
        await interaction.response.send_message(
            "Join To Create already exists!", ephemeral=True
        )
        return

    trigger = await interaction.guild.create_voice_channel(name=TRIGGER_NAME)
    await interaction.response.send_message(
        f"Created {trigger.mention}! Join it to create your room.", ephemeral=True
    )


async def on_app_command_error(
    interaction: discord.Interaction, error: app_commands.AppCommandError
) -> None:
    await report_error(interaction, error)


# --------------------------------------------------------------------------
# Bot
# --------------------------------------------------------------------------

async def start_health_server(port: int) -> web.AppRunner:
    """Tiny HTTP server so web hosts (e.g. Render) see an open port and an
    uptime monitor (e.g. UptimeRobot) has something to ping."""

    async def health(_request: web.Request) -> web.Response:
        return web.Response(text="OK")

    app = web.Application()
    app.router.add_get("/", health)

    runner = web.AppRunner(app)
    await runner.setup()
    await web.TCPSite(runner, "0.0.0.0", port).start()
    log.info("Health server listening on port %s", port)
    return runner


class VoiceBot(discord.Client):
    def __init__(self, guild_id: int) -> None:
        intents = discord.Intents.none()
        intents.guilds = True
        intents.voice_states = True
        intents.members = True  # privileged: enable it in the Developer Portal
        super().__init__(intents=intents)

        self.guild_object = discord.Object(id=guild_id)
        self.tree = app_commands.CommandTree(self)
        self.tree.on_error = on_app_command_error
        self._health_runner: web.AppRunner | None = None

    async def setup_hook(self) -> None:
        # Only when PORT is set (Render sets it), so local runs open no port.
        port = os.getenv("PORT")
        if port:
            self._health_runner = await start_health_server(int(port))

        self.add_view(PanelView())  # make old panels work after a restart
        self.tree.add_command(setup_command, guild=self.guild_object)
        await self.tree.sync(guild=self.guild_object)
        log.info("Slash command registered.")

    async def close(self) -> None:
        if self._health_runner is not None:
            await self._health_runner.cleanup()
        await super().close()

    async def on_ready(self) -> None:
        log.info("Connected as %s", self.user)
        log.info("Private Voice Bot is online!")

        guild = self.get_guild(self.guild_object.id)
        if guild is not None:
            try:
                await restore_stale_nicknames(guild)
            except Exception:
                log.exception("Nickname cleanup error")

    async def on_voice_state_update(
        self,
        member: discord.Member,
        before: discord.VoiceState,
        after: discord.VoiceState,
    ) -> None:
        joined, left = after.channel, before.channel

        if is_trigger_channel(joined) and joined != left:
            try:
                await create_room(member, joined)
            except Exception:
                log.exception("Room creation error")

        if left is not None and left.id in rooms and not left.members:
            rooms.pop(left.id, None)
            save_rooms()
            try:
                await left.delete()
            except discord.HTTPException:
                log.exception("Could not delete empty room")

        # Room emoji on the nickname while inside a room, original one after.
        # (Last, and guarded, so a nickname problem can never stop a room from
        # being created or cleaned up.)
        if joined != left and not member.bot:
            try:
                await sync_nickname(member, joined)
            except Exception:
                log.exception("Nickname error")


def main() -> None:
    load_dotenv()

    token = os.getenv("DISCORD_TOKEN")
    guild_id = os.getenv("GUILD_ID")

    if not token or not guild_id:
        raise SystemExit("Check your .env configuration (DISCORD_TOKEN and GUILD_ID are required).")

    try:
        guild_id_int = int(guild_id)
    except ValueError:
        raise SystemExit("GUILD_ID must be a numeric server ID.") from None

    load_rooms()
    load_nicks()
    VoiceBot(guild_id_int).run(token, root_logger=True)


if __name__ == "__main__":
    main()
