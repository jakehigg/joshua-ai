"""The Telegram adapter: long polling in, split replies out.

The adapter turns a Telegram update into a ``TurnEvent`` and posts it to core,
and turns core's ``send`` back into one or more Telegram messages. Long polling
runs in the container event loop; ``start`` and ``stop`` drive the
``python-telegram-bot`` application. Every inbound message passes the inbound
``Guard`` before it reaches core; an unknown sender never reaches core.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path
from typing import Protocol, runtime_checkable
from uuid import uuid4

from joshua_shared.config import JoshuaConfig
from joshua_shared.contracts import Attachment, Chat, Handle, TurnEvent
from joshua_shared.log import get_logger
from telegram import LinkPreviewOptions, Message, Update
from telegram.constants import ChatAction
from telegram.error import TelegramError
from telegram.ext import Application, ContextTypes, MessageHandler, filters

from joshua_channels.core_client import CoreClient
from joshua_channels.guard import REASON_TRUNCATED, REASON_UNKNOWN, Guard
from joshua_channels.registry import AdapterHealth

logger = get_logger("channels.telegram")

CHANNEL_TYPE = "telegram"

# Telegram caps one message at 4096 characters.
TELEGRAM_MAX = 4096

# The longest poll-error reason reported on ``/readyz``.
REASON_MAX = 200

# The reply core delivers when a turn fails. Sent only on a 5xx from core.
TURN_FAILED = "Sorry, something went wrong handling that."

# The reply an unknown sender gets when the channel policy is ``reply``.
UNKNOWN_SENDER_REPLY = "Sorry, I do not know you, so I cannot help here."

# The text a message with an attachment and no caption carries to core.
NO_CAPTION_TEXT = "(file attached — no caption)"

_DM_PREFIX = "dm:"
_GROUP_PREFIX = "group:"


@runtime_checkable
class AttachmentPipeline(Protocol):
    """Turn downloaded files into ``Attachment`` records.

    The pipeline ticket owns extraction, normalization, and storage under the
    data volume. The adapter downloads to ``inbox_dir`` and hands the directory
    over; the pipeline returns one ``Attachment`` per stored file, each ``path``
    files-MCP relative (``attachments/YYYY/MM/<file>``).
    """

    async def process(
        self, inbox_dir: Path, *, person_id: str | None, group_id: str | None
    ) -> list[Attachment]: ...


def split_message(text: str, limit: int = TELEGRAM_MAX) -> list[str]:
    """Split ``text`` into parts of at most ``limit`` characters.

    Empty text yields no parts. The split prefers line boundaries; a single line
    over the limit is cut hard.
    """
    if not text:
        return []
    parts: list[str] = []
    current = ""
    for line in text.splitlines(keepends=True):
        while len(line) > limit:
            if current:
                parts.append(current)
                current = ""
            parts.append(line[:limit])
            line = line[limit:]
        if len(current) + len(line) > limit:
            parts.append(current)
            current = line
        else:
            current += line
    if current:
        parts.append(current)
    return parts


class TelegramAdapter:
    """One Telegram bot: inbound polling and outbound send."""

    channel_type = CHANNEL_TYPE

    def __init__(
        self,
        *,
        bot_token: str,
        guard: Guard,
        core_client: CoreClient | None,
        settings_provider: Callable[[], JoshuaConfig],
        pipeline: AttachmentPipeline | None = None,
        data_dir: str = "/data",
        bot: object | None = None,
    ) -> None:
        self._bot_token = bot_token
        self._guard = guard
        self._core = core_client
        self._settings_provider = settings_provider
        self._pipeline = pipeline
        self._data_dir = Path(data_dir)
        self._application: Application | None = None
        self._bot = bot
        self._poll_errors = 0
        self._last_poll_error: str | None = None
        self._polling_ok = True

    # --- lifecycle ---------------------------------------------------------

    async def start(self) -> None:
        """Build the application and start long polling in the running loop."""
        application = Application.builder().token(self._bot_token).build()
        application.add_handler(
            MessageHandler(
                (filters.TEXT | filters.PHOTO | filters.Document.ALL) & ~filters.COMMAND,
                self._on_message,
            )
        )
        await application.initialize()
        await application.start()
        if application.updater is not None:
            await application.updater.start_polling(error_callback=self._on_poll_error)
        self._application = application
        self._bot = application.bot
        logger.info({"message": "telegram polling started"})

    def _on_poll_error(self, error: TelegramError) -> None:
        """Record a ``get_updates`` error for ``/readyz``. Never leaks the token.

        The polling loop calls this on every failed poll and keeps retrying. A
        conflicting instance, a bad or revoked token, or a network fault raises
        here and sets the adapter to failing.
        """
        self._poll_errors += 1
        self._last_poll_error = self._redact(str(error) or type(error).__name__)
        self._polling_ok = False
        logger.warning({"message": "telegram poll error", "reason": self._last_poll_error})

    def _on_poll_ok(self) -> None:
        """Record a successful poll for ``/readyz``.

        A delivered update proves ``get_updates`` works now, so a transient error
        that recovered does not keep the check false. The error counter stays for
        the operator, but the adapter reports ok again.
        """
        self._polling_ok = True

    def _redact(self, text: str) -> str:
        """Drop the bot token from an error text and cap its length."""
        if self._bot_token and self._bot_token in text:
            text = text.replace(self._bot_token, "<redacted>")
        return text[:REASON_MAX]

    async def health(self) -> AdapterHealth:
        """Report the adapter state for ``/readyz``.

        The report is the current state, not history. A started adapter whose
        last poll worked is ``ok``, even after an earlier error that recovered. An
        adapter whose last poll raised is failing; ``reason`` names that error.
        ``errors`` counts every poll that raised, so the operator sees a past
        error on a recovered adapter too.
        """
        if self._polling_ok:
            if self._poll_errors:
                return AdapterHealth(ok=True, detail={"errors": self._poll_errors})
            return AdapterHealth(ok=True)
        return AdapterHealth(
            ok=False,
            reason=self._last_poll_error,
            detail={"errors": self._poll_errors},
        )

    async def stop(self) -> None:
        """Stop long polling and shut the application down."""
        application = self._application
        if application is None:
            return
        if application.updater is not None:
            await application.updater.stop()
        await application.stop()
        await application.shutdown()
        self._application = None
        logger.info({"message": "telegram polling stopped"})

    # --- inbound -----------------------------------------------------------

    async def _on_message(self, update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
        """Normalize one Telegram update into a turn and submit it to core."""
        self._on_poll_ok()
        message = update.effective_message
        user = update.effective_user
        chat = update.effective_chat
        if message is None or user is None or chat is None:
            return

        text = message.text or message.caption or ""
        has_attachment = bool(message.photo or message.document)
        if not text and not has_attachment:
            return

        sender_handle = str(user.id)
        chat_id = str(chat.id)
        chat_kind = "dm" if chat.type == "private" else "group"

        cfg = self._settings_provider()
        verdict = self._guard.check(
            channel_type=CHANNEL_TYPE,
            sender_handle=sender_handle,
            chat_id=chat_id,
            chat_kind=chat_kind,
            chat_title=chat.title,
            text_len=len(text),
            attachment_bytes=_attachment_bytes(message),
        )
        if not verdict.allowed:
            await self._on_refused(context.bot, chat_id, verdict.reason, cfg)
            return

        if verdict.reason == REASON_TRUNCATED:
            text = text[: cfg.channels.limits.max_text_chars]

        attachments = await self._collect_attachments(
            message, context.bot, verdict.person_id, verdict.group_id
        )
        if not text:
            text = NO_CAPTION_TEXT

        event = TurnEvent(
            channel=f"{CHANNEL_TYPE}:{chat_id}",
            chat=Chat(kind=chat_kind, id=chat_id, title=chat.title or user.full_name),
            handle=Handle(type=CHANNEL_TYPE, id=sender_handle),
            text=text,
            attachments=attachments,
            message_id=str(message.message_id),
        )

        await context.bot.send_chat_action(chat_id=chat.id, action=ChatAction.TYPING)
        await self._submit(context.bot, chat_id, event)

    async def _on_refused(
        self, bot: object, chat_id: str, reason: str | None, cfg: JoshuaConfig
    ) -> None:
        """Apply the ``unknown_sender`` policy. Rate-limit refusals stay silent."""
        telegram_cfg = cfg.channels.telegram
        policy = telegram_cfg.unknown_sender if telegram_cfg is not None else "drop"
        if reason == REASON_UNKNOWN and policy == "reply":
            await bot.send_message(  # type: ignore[attr-defined]
                chat_id=chat_id,
                text=UNKNOWN_SENDER_REPLY,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )

    async def _submit(self, bot: object, chat_id: str, event: TurnEvent) -> None:
        """Post the turn to core. A server-side failure gets a fixed reply."""
        if self._core is None:
            logger.warning({"message": "no core client; turn dropped", "channel": event.channel})
            return
        try:
            ack = await self._core.submit_turn(event)
        except Exception:  # noqa: BLE001 — a core outage is a 5xx-equivalent for the sender
            logger.warning({"message": "submit_turn raised", "channel": event.channel})
            await self._send_failed(bot, chat_id)
            return
        if not ack.accepted and ack.status >= 500:
            await self._send_failed(bot, chat_id)

    async def _send_failed(self, bot: object, chat_id: str) -> None:
        await bot.send_message(  # type: ignore[attr-defined]
            chat_id=chat_id,
            text=TURN_FAILED,
            link_preview_options=LinkPreviewOptions(is_disabled=True),
        )

    async def _collect_attachments(
        self, message: Message, bot: object, person_id: str | None, group_id: str | None
    ) -> list[Attachment]:
        """Download photos and documents, then hand them to the pipeline."""
        if not (message.photo or message.document):
            return []
        if self._pipeline is None:
            logger.warning({"message": "no attachment pipeline; attachment skipped"})
            return []
        inbox_dir = self._data_dir / "inbox" / uuid4().hex
        inbox_dir.mkdir(parents=True, exist_ok=True)
        await _download_attachments(message, bot, inbox_dir)
        return await self._pipeline.process(inbox_dir, person_id=person_id, group_id=group_id)

    # --- outbound ----------------------------------------------------------

    async def send(self, chat_id: str, text: str, attachments: list[Path]) -> int:
        """Send a reply as split text parts and one message per attachment.

        Returns the number of Telegram messages sent. Link previews are disabled
        on every text message, because Telegram's servers fetch a previewed URL.
        """
        if self._bot is None:
            raise RuntimeError("telegram adapter not started")
        parts = 0
        for chunk in split_message(text):
            await self._bot.send_message(  # type: ignore[attr-defined]
                chat_id=chat_id,
                text=chunk,
                link_preview_options=LinkPreviewOptions(is_disabled=True),
            )
            parts += 1
        for path in attachments:
            if _is_image(path):
                await self._bot.send_photo(chat_id=chat_id, photo=path)  # type: ignore[attr-defined]
            else:
                await self._bot.send_document(chat_id=chat_id, document=path)  # type: ignore[attr-defined]
            parts += 1
        return parts

    # --- resolve -----------------------------------------------------------

    def resolve_ref(self, ref: str) -> str | None:
        """Map ``dm:<person_id>`` or ``group:<group_id>`` to a chat id, or None."""
        cfg = self._settings_provider()
        if ref.startswith(_DM_PREFIX):
            person = cfg.person(ref[len(_DM_PREFIX) :])
            return person.handles.get(CHANNEL_TYPE) if person is not None else None
        if ref.startswith(_GROUP_PREFIX):
            group_id = ref[len(_GROUP_PREFIX) :]
            for group in cfg.groups:
                if group.channel == CHANNEL_TYPE and group.id == group_id:
                    return group.chat_id
            return None
        return None


def _attachment_bytes(message: Message) -> int:
    """Return the largest declared attachment size, without downloading."""
    size = 0
    if message.photo:
        size = max(size, message.photo[-1].file_size or 0)
    if message.document:
        size = max(size, message.document.file_size or 0)
    return size


async def _download_attachments(message: Message, bot: object, inbox_dir: Path) -> None:
    """Download the largest photo size and any document into ``inbox_dir``."""
    if message.photo:
        largest = message.photo[-1]
        tg_file = await bot.get_file(largest.file_id)  # type: ignore[attr-defined]
        await tg_file.download_to_drive(inbox_dir / f"{largest.file_unique_id}.jpg")
    if message.document:
        document = message.document
        tg_file = await bot.get_file(document.file_id)  # type: ignore[attr-defined]
        name = document.file_name or document.file_unique_id
        await tg_file.download_to_drive(inbox_dir / name)


_IMAGE_SUFFIXES = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".heic", ".bmp"}


def _is_image(path: Path) -> bool:
    return path.suffix.lower() in _IMAGE_SUFFIXES
