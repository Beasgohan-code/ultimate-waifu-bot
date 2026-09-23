"""Rich messages (Bot API 9.5+) — the card format for spawns, pulls and receipts.

Rich messages are Telegram's document-like layout primitive: a list of typed
blocks (paragraph, photo, table, buttons, divider, footer, quote, list, code,
details…) with inline buttons that belong to the *content* rather than to the
message. It is what makes a /summon card feel native instead of "photo + caption".

The builder below produces ``InputRichMessage`` payloads and can emit an HTML
fallback for the same content, because:

* old Bot API servers (and self-hosted ones) reject the method;
* Telegram may refuse rich messages in a few chat contexts.

so every call site gets exactly the same text either way.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from html import escape
from typing import Any

from aiogram.methods import SendRichMessage
from aiogram.types import (
    CopyTextButton,
    DisabledButton,
    InputMediaPhoto,
    InputRichBlockButtons,
    InputRichBlockCollage,
    InputRichBlockDetails,
    InputRichBlockDivider,
    InputRichBlockFooter,
    InputRichBlockList,
    InputRichBlockListItem,
    InputRichBlockParagraph,
    InputRichBlockPhoto,
    InputRichBlockPreformatted,
    InputRichBlockPullQuotation,
    InputRichBlockSectionHeading,
    InputRichBlockTable,
    InputRichMessage,
    RichBlockCaption,
    RichBlockTableCell,
    RichMessageButton,
    RichTextBold,
    RichTextCustomEmoji,
    RichTextItalic,
)

from waifu.utils.text import truncate

RichNode = Any  # aiogram types | str — a plain str is a literal text node
ButtonStyle = str  # "success" | "danger" | "warning" | "premium" | "default"


def _text(parts: Iterable[RichNode] | RichNode) -> list[RichNode]:
    if isinstance(parts, (str, dict)) or hasattr(parts, "model_dump"):
        return [parts]  # type: ignore[list-item]
    return list(parts)


@dataclass(slots=True)
class RichButton:
    """A button inside the rich message (rendered as a real, tappable button)."""

    text: str
    callback_data: str | None = None
    url: str | None = None
    style: ButtonStyle | None = None
    icon_emoji_id: str | None = None
    disabled: bool = False
    copy_text: str | None = None
    web_app_url: str | None = None

    def to_aiogram(self) -> RichMessageButton:
        # Rich-message buttons have no ``icon_custom_emoji_id`` field (unlike
        # inline keyboard buttons in API 9.4), so the icon is a real text node.
        text: Any = self.text[:64]
        if self.icon_emoji_id:
            text = [
                RichTextCustomEmoji(custom_emoji_id=self.icon_emoji_id, alternative_text=""),
                text,
            ]
        kwargs: dict[str, Any] = {"text": text}
        if self.style:
            kwargs["style"] = self.style
        if self.disabled:
            kwargs["disabled"] = DisabledButton()
        if self.copy_text:
            kwargs["copy_text"] = CopyTextButton(text=self.copy_text[:1024])
        if self.url:
            kwargs["url"] = self.url
        elif self.callback_data:
            kwargs["callback_data"] = self.callback_data[:64]
        elif self.web_app_url:
            from aiogram.types import WebAppInfo

            kwargs["web_app"] = WebAppInfo(url=self.web_app_url)
        else:
            kwargs["disabled"] = kwargs.get("disabled") or DisabledButton()
        return RichMessageButton(**kwargs)


class RichMessageBuilder:
    """Chainable builder producing a rich message plus its HTML fallback.

    Example (a spawn card)::

        card = (
            RichMessageBuilder()
            .heading("Kiyomi — Midnight Blade")
            .photo("https://…/k.png", caption="⭐ Legendary  ⚡92")
            .table([["Stat", "Value"], ["Power", "92"], ["Element", "Dark"]])
            .quote("The blade remembers what you forget.", credit="Kiyomi")
            .buttons([RichButton("Summon", callback_data="spn:claim:12", style="success")])
            .footer("expires in 90 s")
        )
        await send_rich(bot, chat_id, card)
    """

    __slots__ = ("_blocks", "_html", "_media")

    def __init__(self) -> None:
        self._blocks: list[Any] = []
        self._html: list[str] = []
        self._media: list[dict[str, str]] = []

    # ------------------------------------------------------------- structure
    def heading(self, text: str, *, size: int = 2) -> RichMessageBuilder:
        self._blocks.append(
            InputRichBlockSectionHeading(
                text=_format_plain(text, RichTextBold),
                size=size,
            )
        )
        self._html.append(f"<b>{escape(text)}</b>")
        return self

    def paragraph(self, *nodes: RichNode, html: str | None = None) -> RichMessageBuilder:
        """Mixed-style paragraph: pass str for literal text, or RichText* nodes."""
        parts = list(nodes) or ["…"]
        self._blocks.append(InputRichBlockParagraph(text=_text(parts)))
        self._html.append(html or _plain_text(parts))
        return self

    def line(self, text: str, *, html: str | None = None) -> RichMessageBuilder:
        """One plain line of text."""
        self._blocks.append(InputRichBlockParagraph(text=_format_plain(text, None)))
        self._html.append(html if html is not None else escape(text))
        return self

    def bold(self, text: str) -> RichMessageBuilder:
        self._blocks.append(InputRichBlockParagraph(text=_format_plain(text, RichTextBold)))
        self._html.append(f"<b>{escape(text)}</b>")
        return self

    def photo(
        self,
        media: str,
        *,
        caption: str | None = None,
        credit: str | None = None,
        spoiler: bool = False,
    ) -> RichMessageBuilder:
        """Photo block. ``media`` is a file_id or URL — never a raw re-upload."""
        # parse_mode is explicitly None: blocks carry their own formatting, and
        # leaving it unset would inherit the bot default and confuse the
        # serializer for a caption that is already rich text.
        photo = InputMediaPhoto(
            media=media,
            parse_mode=None,
            show_caption_above_media=False,
            has_spoiler=spoiler or None,
        )
        block_caption = (
            RichBlockCaption(
                text=_format_plain(caption, None),
                credit=_format_plain(credit, RichTextItalic) if credit else None,
            )
            if caption
            else None
        )
        self._blocks.append(InputRichBlockPhoto(photo=photo, caption=block_caption))
        if caption:
            self._html.append(f"<i>{escape(caption)}</i>")
        self._media.append({"type": "photo", "media": media})
        return self

    def collage(self, *medias: str, caption: str | None = None) -> RichMessageBuilder:
        """Multi-art collage — used by /h-stats and 10-pull highlights."""
        blocks = [
            InputRichBlockPhoto(
                photo=InputMediaPhoto(media=m, parse_mode=None, show_caption_above_media=False)
            )
            for m in medias
            if m
        ]
        self._blocks.append(
            InputRichBlockCollage(
                blocks=blocks,
                caption=RichBlockCaption(text=_format_plain(caption, None)) if caption else None,
            )
        )
        if caption:
            self._html.append(escape(caption))
        return self

    def table(
        self,
        rows: list[list[object]],
        *,
        caption: str | None = None,
        compact: bool = True,
        bordered: bool = True,
    ) -> RichMessageBuilder:
        """Table block — perfect for /price, /guarantee and /stats breakdowns."""
        cells = []
        for row_index, row in enumerate(rows):
            cells.append(
                [
                    RichBlockTableCell(
                        # aiogram marks align/valign as required even though the
                        # Bot API defaults them; passing them is harmless.
                        align="left" if row_index else "center",
                        valign="top",
                        text=_format_plain(str(value), RichTextBold if row_index == 0 else None),
                        is_header=True if row_index == 0 else None,
                    )
                    for value in row
                ]
            )
        self._blocks.append(
            InputRichBlockTable(
                cells=cells,
                caption=_format_plain(caption, None) if caption else None,
                is_compact=compact or None,
                is_bordered=bordered or None,
            )
        )
        self._html.append(_table_to_html(rows))
        return self

    def list(self, *items: str, numbered: bool = False) -> RichMessageBuilder:
        self._blocks.append(
            InputRichBlockList(
                items=[
                    InputRichBlockListItem(
                        blocks=[InputRichBlockParagraph(text=_format_plain(item, None))],
                        value=index if numbered else None,
                    )
                    for index, item in enumerate(items, start=1)
                ]
            )
        )
        tag = "ol" if numbered else "ul"
        self._html.append(
            f"<{tag}>" + "".join(f"<li>{escape(i)}</li>" for i in items) + f"</{tag}>"
        )
        return self

    def quote(self, text: str, *, credit: str | None = None) -> RichMessageBuilder:
        self._blocks.append(
            InputRichBlockPullQuotation(
                text=_format_plain(text, RichTextItalic),
                credit=_format_plain(credit, None) if credit else None,
            )
        )
        self._html.append(f"<blockquote>{escape(text)}</blockquote>")
        return self

    def code(self, text: str, *, language: str | None = None) -> RichMessageBuilder:
        self._blocks.append(
            InputRichBlockPreformatted(text=_format_plain(text, None), language=language)
        )
        self._html.append(f"<code>{escape(text)}</code>")
        return self

    def details(self, summary: str, *lines: str, open: bool = False) -> RichMessageBuilder:
        """Collapsible block — used for /guarantee maths and audit payloads."""
        self._blocks.append(
            InputRichBlockDetails(
                summary=_format_plain(summary, None),
                blocks=[InputRichBlockParagraph(text=_format_plain(line, None)) for line in lines],
                is_open=open or None,
            )
        )
        self._html.append(f"<b>{escape(summary)}</b>\n" + "\n".join(escape(line) for line in lines))
        return self

    def buttons(self, buttons: list[RichButton], *, align: str | None = None) -> RichMessageBuilder:
        if buttons:
            self._blocks.append(
                InputRichBlockButtons(buttons=[b.to_aiogram() for b in buttons], align=align)
            )
        return self

    def divider(self) -> RichMessageBuilder:
        self._blocks.append(InputRichBlockDivider())
        return self

    def footer(self, text: str) -> RichMessageBuilder:
        self._blocks.append(InputRichBlockFooter(text=_format_plain(text, None)))
        self._html.append(f"<i>{escape(text)}</i>")
        return self

    def raw(self, block: Any) -> RichMessageBuilder:
        self._blocks.append(block)
        return self

    # ---------------------------------------------------------------- output
    @property
    def is_empty(self) -> bool:
        return not self._blocks

    def build(self) -> InputRichMessage:
        if not self._blocks:
            self.paragraph("…")
        return InputRichMessage(blocks=list(self._blocks))

    def fallback_html(self, *, limit: int = 1000) -> str:
        """The same content as a caption, for clients/servers without rich support."""
        joined = "\n".join(part for part in self._html if part)
        return truncate(joined, limit) or "…"

    def to_method(
        self,
        chat_id: int,
        *,
        message_thread_id: int | None = None,
        reply_to: int | None = None,
        disable_notification: bool = False,
        protect_content: bool = False,
        message_effect_id: str | None = None,
        allow_paid_broadcast: bool = False,
    ) -> SendRichMessage:
        return SendRichMessage(
            chat_id=chat_id,
            rich_message=self.build(),
            message_thread_id=message_thread_id,
            reply_to_message_id=reply_to,
            disable_notification=disable_notification or None,
            protect_content=protect_content or None,
            message_effect_id=message_effect_id,
            allow_paid_broadcast=allow_paid_broadcast or None,
        )


def rich_log(title: str, body: str, *, detail: str = "") -> InputRichMessage:
    """One owner-log event as a native rich message (Bot API 9.5+).

    ``title`` (the emoji + event name) is the section heading, ``body`` the line
    itself, and an optional ``detail`` a code block for machine detail (ids,
    amounts, versions). Callers still pass the plain string as the fallback, so
    a server without the feature renders the identical line.
    """
    builder = RichMessageBuilder().heading(title).paragraph(body)
    if detail:
        builder.code(detail)
    return builder.build()


def gift_receipt_rich(
    *,
    name: str,
    series: str = "",
    rarity: str = "",
    note: str = "",
    from_name: str = "",
    media: str = "",
) -> InputRichMessage:
    """The private gift receipt, rendered natively (heading + art + rarity + note).

    Takes plain strings only — the service layer must not build aiogram types.
    ``media`` is a file_id or URL (a photo block); without it the receipt is a
    text card, exactly like the HTML fallback.
    """
    builder = RichMessageBuilder().heading(f"🎁 {name}")
    if media:
        builder.photo(media, caption=f"{series} · {rarity}".strip(" ·") or None)
    if note:
        builder.quote(truncate(note, 120))
    builder.footer(f"From: {from_name}" if from_name else "From: an anonymous admirer 🎭")
    return builder.build()


def rich_digest(title: str, rows: list[list[object]], *, footer: str = "") -> InputRichMessage:
    """The weekly owner digest: heading + a metrics table + a timestamp footer."""
    builder = (
        RichMessageBuilder().heading(title).table([["metric", "last 7 days"], *rows], compact=True)
    )
    if footer:
        builder.footer(footer)
    return builder.build()


# --------------------------------------------------------------------- helpers
def _format_plain(text: RichNode, wrapper: type | None) -> list[RichNode]:
    """Wrap a string in an optional style node; ``None`` means "leave literal"."""
    rendered = str(text)
    return [wrapper(text=rendered)] if wrapper is not None else [rendered]


def _plain_text(nodes: tuple[RichNode, ...] | list[RichNode]) -> str:
    out: list[str] = []
    for node in nodes:
        if isinstance(node, str):
            out.append(escape(node))
        elif hasattr(node, "text"):
            inner = node.text  # RichTextBold/Italic/… nest one level
            out.append(escape(inner if isinstance(inner, str) else str(inner)))
        else:
            out.append(escape(str(node)))
    return " ".join(out).strip()


def _table_to_html(rows: list[list[object]]) -> str:
    if not rows:
        return ""
    header = " | ".join(str(c) for c in rows[0])
    body = "\n".join(" | ".join(str(c) for c in row) for row in rows[1:])
    return f"<code>{escape(header)}</code>\n{body}".strip()
