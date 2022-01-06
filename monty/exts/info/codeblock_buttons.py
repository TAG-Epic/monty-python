import asyncio
import dataclasses
import logging
from typing import TYPE_CHECKING, Optional, Union

import disnake
from disnake.ext import commands

from monty.bot import Bot
from monty.constants import Emojis, URLs


if TYPE_CHECKING:
    from monty.exts.eval import Snekbox
    from monty.exts.info.codeblock._cog import CodeBlockCog

logger = logging.getLogger(__name__)

TIMEOUT = 180


@dataclasses.dataclass
class CodeblockMessage:
    """Represents a message that was already parsed to determine the code."""

    parsed_code: str
    reactions: set[Union[disnake.PartialEmoji, str]]


class CodeButtons(commands.Cog):
    """Adds automatic buttons to codeblocks if they match commands."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.messages: dict[int, CodeblockMessage] = {}
        self.actions = {Emojis.black: self.format_black}
        self.black_endpoint = URLs.black_formatter

    @commands.Cog.listener()
    async def on_message(self, message: disnake.Message) -> None:
        """See if a message matches the pattern."""
        if not message.guild:
            return

        if not message.channel.permissions_for(message.guild.me).add_reactions:
            return

        if not (snekbox := self.get_snekbox()):
            logger.trace("Could not parse message as the snekbox cog is not loaded.")
            return None
        code = snekbox.prepare_input(message.content, require_fenced=True)
        if not code or code.count("\n") < 2:
            logger.trace("Parsed message but either no code was found or was too short.")
            return None
        # not required, but recommended
        if (codeblock := self.get_codeblock_cog()) and not codeblock.is_python_code(code):
            logger.trace("Code blocks exist but they are not python code.")
            return

        logger.debug("Adding reactions since message passes.")
        for react in self.actions.keys():
            await message.add_reaction(react)
        self.messages[message.id] = CodeblockMessage(
            code,
            {*self.actions.keys()},
        )

        await asyncio.sleep(TIMEOUT)

        try:
            cb_msg = self.messages.pop(message.id)
        except KeyError:
            return
        for reaction in cb_msg.reactions:
            await message.remove_reaction(reaction, message.guild.me)

    @commands.Cog.listener()
    async def on_message_edit(self, before: disnake.Message, after: disnake.Message) -> None:
        """Listen for edits and relay them to the on_message listener."""
        await self.on_message(after)

    @commands.Cog.listener()
    async def on_reaction_add(self, reaction: disnake.Reaction, user: disnake.User) -> None:
        """Listen for reactions on codeblock messages."""
        if not reaction.message.guild:
            return

        # DO ignore bots on reaction add
        if user.bot:
            return

        if user.id == self.bot.user.id:
            return

        if not (code_block := self.messages.get(reaction.message.id)):
            return

        if str(reaction.emoji) not in code_block.reactions:
            print(reaction.emoji)
            return
        meth = self.actions[str(reaction.emoji)]
        await meth(reaction.message)
        self.messages.pop(reaction.message.id)
        await reaction.message.remove_reaction(reaction, reaction.message.guild.me)

    def get_snekbox(self) -> Optional["Snekbox"]:
        """Get the Snekbox cog. This method serves for typechecking."""
        return self.bot.get_cog("Snekbox")

    def get_codeblock_cog(self) -> Optional["CodeBlockCog"]:
        """Get the Codeblock cog. This method serves for typechecking."""
        return self.bot.get_cog("Code Block")

    async def format_black(self, message: disnake.Message) -> None:
        """Format the provided message with black."""
        json = {
            "source": self.messages[message.id].parsed_code,
            "options": {"line_length": 110},
        }
        await message.channel.trigger_typing()
        async with self.bot.http_session.post(self.black_endpoint, json=json) as resp:
            if resp.status != 200:
                logger.error("Black endpoint returned not a 200")
                await message.channel.send(
                    "Something went wrong internally when formatting the code. Please report this."
                )
                return
            json: dict = await resp.json()
        formatted = json["formatted_code"].strip()
        if json["source_code"].strip() == formatted:
            logger.debug("code was formatted with black but no changes were made.")
            await message.reply(
                "Formatted with black but no changes were made! :ok_hand:",
                fail_if_not_exists=False,
            )
            return
        paste = await self.get_snekbox().upload_output(formatted, "python")
        if not paste:
            await message.channel.send("Sorry, something went wrong!")
            return
        button = disnake.ui.Button(
            style=disnake.ButtonStyle.url,
            label="Click to open in workbin",
            url=paste,
        )
        await message.reply(
            "Formatted with black. Click the button below to view on the pastebin.",
            fail_if_not_exists=False,
            components=button,
        )


def setup(bot: Bot) -> None:
    """Add the CodeButtons cog to the bot."""
    if not URLs.black_formatter:
        logger.warning("Not loading codeblock buttons as black_formatter is not set.")
        return
    bot.add_cog(CodeButtons(bot))
