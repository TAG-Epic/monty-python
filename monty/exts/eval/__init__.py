import asyncio
import contextlib
import datetime
import re
import textwrap
from functools import partial
from signal import Signals
from typing import Any, Optional, Tuple, overload

import aiohttp
from disnake import (
    CommandInteraction,
    HTTPException,
    Message,
    ModalInteraction,
    NotFound,
    Reaction,
    TextInputStyle,
    User,
)
from disnake.ext.commands import Cog, Context, command, guild_only, slash_command
from disnake.ui import Modal

from monty.bot import Bot
from monty.constants import Guilds, Paste, URLs
from monty.log import get_logger
from monty.utils import scheduling
from monty.utils.exceptions import APIError
from monty.utils.extensions import invoke_help_command
from monty.utils.messages import wait_for_deletion
from monty.utils.services import send_to_paste_service


log = get_logger(__name__)

INLINE_EVAL_REGEX = re.compile(r"\$(?P<fence>`+)(.+)(?P=fence)")

ESCAPE_REGEX = re.compile("[`\u202E\u200B]{3,}")
FORMATTED_CODE_REGEX = re.compile(
    r"(?P<delim>(?P<block>```)|``?)"  # code delimiter: 1-3 backticks; (?P=block) only matches if it's a block
    r"(?(block)(?:(?P<lang>[a-z]+)\n)?)"  # if we're in a block, match optional language (only letters plus newline)
    r"(?:[ \t]*\n)*"  # any blank (empty or tabs/spaces only) lines before the code
    r"(?P<code>.*?)"  # extract all code inside the markup
    r"\s*"  # any more whitespace before the end of the code markup
    r"(?P=delim)",  # match the exact same delimiter from the start again
    re.DOTALL | re.IGNORECASE,  # "." also matches newlines, case insensitive
)
RAW_CODE_REGEX = re.compile(
    r"^(?:[ \t]*\n)*"  # any blank (empty or tabs/spaces only) lines before the code
    r"(?P<code>.*?)"  # extract all the rest as code
    r"\s*$",  # any trailing whitespace until the end of the string
    re.DOTALL,  # "." also matches newlines
)

MAX_PASTE_LEN = 10000


SIGKILL = 9

REEVAL_EMOJI = "\U0001f501"  # :repeat:
REEVAL_TIMEOUT = 30

HEADERS = {}
if URLs.snekbox_auth:
    HEADERS["Authorization"] = URLs.snekbox_auth
PLACEHOLDER_CODE = """
from random import choice

print(choice(("single quotes", 'double quotes')))

""".lstrip()


class EvalModal(Modal):
    """Modal for evaluation."""

    def __init__(self, snekbox: "Snekbox", *, title: str = "Eval Code"):
        super().__init__(title=title, components=[])
        self.snekbox = snekbox
        self.custom_id = "snekbox_eval"
        self.add_text_input(label="Code", custom_id="code", style=TextInputStyle.long, placeholder=PLACEHOLDER_CODE)

    async def callback(self, inter: ModalInteraction) -> None:
        """Evaluate the provided code."""
        await inter.response.defer()
        await self.snekbox.send_eval(inter, inter.text_values["code"], original_source=True)


class Snekbox(Cog):
    """Safe evaluation of Python code using Snekbox."""

    def __init__(self, bot: Bot):
        self.bot = bot
        self.jobs = {}

    async def post_eval(self, code: str) -> dict:
        """Send a POST request to the Snekbox API to evaluate code and return the results."""
        url = URLs.snekbox_eval_api
        data = {"input": code}
        try:
            async with self.bot.http_session.post(
                url,
                json=data,
                raise_for_status=True,
                headers=HEADERS,
                timeout=10,
            ) as resp:
                return await resp.json()
        except aiohttp.ClientConnectorError:
            raise APIError("snekbox", 0, "Snekbox backend is offline or misconfigured.")

    async def upload_output(self, output: str, extension: str = "text") -> Optional[str]:
        """Upload the eval output to a paste service and return a URL to it if successful."""
        log.trace("Uploading full output to paste service...")

        if len(output) > MAX_PASTE_LEN:
            log.info("Full output is too long to upload")
            return None
        return await send_to_paste_service(output, extension=extension)

    @overload
    @staticmethod
    def prepare_input(code: str, *, require_fenced: bool = False) -> str:
        ...

    @overload
    @staticmethod
    def prepare_input(code: str, *, require_fenced: bool = True) -> Optional[str]:
        ...

    @staticmethod
    def prepare_input(code: str, *, require_fenced: bool = False) -> Optional[str]:
        """
        Extract code from the Markdown, format it, and insert it into the code template.

        If there is any code block, ignore text outside the code block.
        Use the first code block, but prefer a fenced code block.
        If there are several fenced code blocks, concatenate only the fenced code blocks.
        """
        if match := list(FORMATTED_CODE_REGEX.finditer(code)):
            blocks = [block for block in match if block.group("block")]

            if len(blocks) > 1:
                code = "\n".join(block.group("code") for block in blocks)
                info = "several code blocks"
            else:
                match = match[0] if len(blocks) == 0 else blocks[0]
                code, block, lang, delim = match.group("code", "block", "lang", "delim")
                if block:
                    info = (f"'{lang}' highlighted" if lang else "plain") + " code block"
                else:
                    info = f"{delim}-enclosed inline code"
        elif require_fenced:
            return None
        else:
            code = RAW_CODE_REGEX.fullmatch(code).group("code")
            info = "unformatted or badly formatted code"

        code = textwrap.dedent(code)
        log.trace(f"Extracted {info} for evaluation:\n{code}")
        return code

    @staticmethod
    def get_results_message(results: dict) -> Tuple[str, str]:
        """Return a user-friendly message and error corresponding to the process's return code."""
        stdout, returncode = results["stdout"], results["returncode"]
        msg = f"Your eval job has completed with return code {returncode}"
        error = ""

        if returncode is None:
            msg = "Your eval job has failed"
            error = stdout.strip()
        elif returncode == 128 + SIGKILL:
            msg = "Your eval job timed out or ran out of memory"
        elif returncode == 255:
            msg = "Your eval job has failed"
            error = "A fatal NsJail error occurred"
        else:
            # Try to append signal's name if one exists
            try:
                name = Signals(returncode - 128).name
                msg = f"{msg} ({name})"
            except ValueError:
                pass

        return msg, error

    @staticmethod
    def get_status_emoji(results: dict) -> str:
        """Return an emoji corresponding to the status code or lack of output in result."""
        if not results["stdout"].strip():  # No output
            return ":warning:"
        elif results["returncode"] == 0:  # No error
            return ":white_check_mark:"
        else:  # Exception
            return ":x:"

    async def format_output(self, output: str) -> Tuple[str, Optional[str]]:
        """
        Format the output and return a tuple of the formatted output and a URL to the full output.

        Prepend each line with a line number. Truncate if there are over 10 lines or 1000 characters
        and upload the full output to a paste service.
        """
        log.trace("Formatting output...")

        output = output.rstrip("\n")
        original_output = output  # To be uploaded to a pasting service if needed
        paste_link = None

        if "<@" in output:
            output = output.replace("<@", "<@\u200B")  # Zero-width space

        if "<!@" in output:
            output = output.replace("<!@", "<!@\u200B")  # Zero-width space

        if ESCAPE_REGEX.findall(output):
            paste_link = await self.upload_output(original_output) or "too long to upload"
            return "Code block escape attempt detected; will not output result", paste_link

        truncated = False
        lines = output.count("\n")

        if lines > 0:
            output = [f"{i:03d} | {line}" for i, line in enumerate(output.split("\n"), 1)]
            output = output[:11]  # Limiting to only 11 lines
            output = "\n".join(output)

        if lines > 10:
            truncated = True
            if len(output) >= 1000:
                output = f"{output[:1000]}\n... (truncated - too long, too many lines)"
            else:
                output = f"{output}\n... (truncated - too many lines)"
        elif len(output) >= 1000:
            truncated = True
            output = f"{output[:1000]}\n... (truncated - too long)"

        if truncated:
            paste_link = await self.upload_output(original_output) or "too long to upload"

        output = output or "[No output]"

        return output, paste_link

    @overload
    async def send_eval(
        self, ctx: Context, code: str, return_result: bool = True, original_source: bool = False
    ) -> tuple[str, Optional[str]]:
        """Send eval and receive a tuple of the msg and paste link."""
        pass

    @overload
    async def send_eval(
        self, ctx: Context, code: str, return_result: bool = False, original_source: bool = False
    ) -> Message:
        """Return the bot response from an eval invocation."""
        pass

    async def send_eval(
        self, ctx: Context, code: str, return_result: bool = False, original_source: bool = False
    ) -> Any:
        """
        Evaluate code, format it, and send the output to the corresponding channel.

        Return the bot response.
        """
        if isinstance(ctx, Context):
            await ctx.trigger_typing()
        results = await self.post_eval(code)
        msg, error = self.get_results_message(results)

        if error:
            output, paste_link = error, None
        else:
            output, paste_link = await self.format_output(results["stdout"])
            if paste_link and Paste.alias_url and ctx.guild and ctx.guild.id == Guilds.nextcord:
                paste_link = paste_link.replace(".disnake.", ".nextcord.")

        icon = self.get_status_emoji(results)
        msg = f"{ctx.author.mention} {icon} {msg}.\n\n```\n{output}\n```"

        log.info(f"{ctx.author}'s job had a return code of {results['returncode']}")

        if original_source:
            original_source = await self.upload_output(code)
            msg += f"\nOriginal code link: {original_source}"

        if return_result:
            return msg, paste_link

        if paste_link:
            msg = f"{msg}\nFull output: {paste_link}"

        if hasattr(ctx, "reply"):
            response = await ctx.reply(msg)
        else:
            await ctx.send(msg)
            response = await ctx.original_message()

        scheduling.create_task(wait_for_deletion(response, (ctx.author.id,)), event_loop=self.bot.loop)

        return response

    async def continue_eval(self, ctx: Context, response: Message) -> Optional[str]:
        """
        Check if the eval session should continue.

        Return the new code to evaluate or None if the eval session should be terminated.
        """
        _predicate_eval_message_edit = partial(predicate_eval_message_edit, ctx)
        _predicate_emoji_reaction = partial(predicate_eval_emoji_reaction, ctx)

        with contextlib.suppress(NotFound):
            try:
                _, new_message = await self.bot.wait_for(
                    "message_edit", check=_predicate_eval_message_edit, timeout=REEVAL_TIMEOUT
                )
                await ctx.message.add_reaction(REEVAL_EMOJI)
                await self.bot.wait_for("reaction_add", check=_predicate_emoji_reaction, timeout=30)

                code = await self.get_code(new_message)
                await ctx.message.remove_reaction(REEVAL_EMOJI, ctx.me)
                with contextlib.suppress(HTTPException):
                    await response.delete()

            except asyncio.TimeoutError:
                await ctx.message.remove_reaction(REEVAL_EMOJI, ctx.me)
                return None

            return code

    async def get_code(self, message: Message) -> Optional[str]:
        """
        Return the code from `message` to be evaluated.

        If the message is an invocation of the eval command, return the first argument or None if it
        doesn't exist. Otherwise, return the full content of the message.
        """
        log.trace(f"Getting context for message {message.id}.")
        new_ctx = await self.bot.get_context(message)

        if new_ctx.command is self.eval_command:
            log.trace(f"Message {message.id} invokes eval command.")
            split = message.content.split(maxsplit=1)
            code = split[1] if len(split) > 1 else None
        else:
            log.trace(f"Message {message.id} does not invoke eval command.")
            code = message.content

        return code

    @slash_command(name="eval")
    async def slash_eval(self, inter: CommandInteraction) -> None:
        """Open a modal to send python code to be evaluated."""
        await inter.response.send_modal(EvalModal(self))

    @command(name="eval", aliases=("e",))
    @guild_only()
    async def eval_command(self, ctx: Context, *, code: str = None) -> None:
        """
        Run Python code and get the results.

        This command supports multiple lines of code, including code wrapped inside a formatted code
        block. Code can be re-evaluated by editing the original message within 10 seconds and
        clicking the reaction that subsequently appears.

        We've done our best to make this sandboxed, but do let us know if you manage to find an
        issue with it!
        """
        if ctx.author.id in self.jobs:
            await ctx.reply(f"{ctx.author.mention} You've already got a job running - " "please wait for it to finish!")
            return

        if not code:  # None or empty string
            await invoke_help_command(ctx)
            return

        log.info(f"Received code from {ctx.author} for evaluation:\n{code}")

        while True:
            self.jobs[ctx.author.id] = datetime.datetime.now()
            code = self.prepare_input(code)
            try:
                response = await self.send_eval(ctx, code)
            finally:
                del self.jobs[ctx.author.id]

            code = await self.continue_eval(ctx, response)
            if not code:
                break
            log.info(f"Re-evaluating code from message {ctx.message.id}:\n{code}")

    @Cog.listener()
    async def on_message(self, message: Message) -> None:
        """Evaluate code in the message automatically."""
        if not message.guild:
            return

        if message.author.bot:
            return

        code = "\n".join([m[-1].strip() for m in INLINE_EVAL_REGEX.findall(message.content)])

        if not code:
            return
        await self.send_eval(message, code, return_result=False)


def predicate_eval_message_edit(ctx: Context, old_msg: Message, new_msg: Message) -> bool:
    """Return True if the edited message is the context message and the content was indeed modified."""
    return new_msg.id == ctx.message.id and old_msg.content != new_msg.content


def predicate_eval_emoji_reaction(ctx: Context, reaction: Reaction, user: User) -> bool:
    """Return True if the reaction REEVAL_EMOJI was added by the context message author on this message."""
    return reaction.message.id == ctx.message.id and user.id == ctx.author.id and str(reaction) == REEVAL_EMOJI


def setup(bot: Bot) -> None:
    """Load the Snekbox cog."""
    bot.add_cog(Snekbox(bot))
