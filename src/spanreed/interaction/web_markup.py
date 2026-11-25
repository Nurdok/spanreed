"""Turn what is said to the user into safe HTML for the web UI.

A text comes in one of three ways, as it does for Telegram, and the page
shows it as Telegram would:

- as HTML, in the tags that Telegram's HTML parse mode takes;
- as Markdown, in Telegram's MarkdownV2;
- as plain text.

Everything is escaped, and only what is known is put back as a tag, so a
text can never bring markup of its own to the page.

A block of code is drawn as one: ``<pre class="code"><code>``. If it names
its language, Pygments marks its parts for colour, in ``<span>`` elements
with Pygments' short class names. The page has the colours.
"""

from __future__ import annotations

import html
import logging
import re

from pygments import highlight
from pygments.formatters import HtmlFormatter
from pygments.lexers import get_lexer_by_name
from pygments.util import ClassNotFound

from spanreed.interaction.surface import Content

_logger = logging.getLogger(__name__)

# Code that is longer than this is shown without colours.
HIGHLIGHT_MAX = 20_000

# The tags Telegram's HTML parse mode accepts, which is what plugins write.
_ALLOWED_TAGS = {
    "b",
    "strong",
    "i",
    "em",
    "u",
    "ins",
    "s",
    "strike",
    "del",
    "code",
    "pre",
    "blockquote",
    "tg-spoiler",
}
_TAG_RE = re.compile(r"&lt;(/?)([a-zA-Z-]+)((?:\s.*?)?)&gt;")
# Matches the (already escaped) attribute text of an <a> tag; the captured
# URL is still escaped, which is what goes back into the attribute.
_HREF_RE = re.compile(
    r"^\s+href=(?:&quot;|&#x27;)?(https?://(?:[^\s&]|&amp;)+?)(?:&quot;|&#x27;|\s|$)"
)
# An entity that the text was written with, as it looks once the text is
# escaped. HTML for Telegram writes ``&amp;`` where it means ``&``.
_ENTITY_RE = re.compile(r"&amp;(lt|gt|amp|quot|#\d{1,7}|#x[0-9a-fA-F]{1,6});")
_ANY_TAG_RE = re.compile(r"<[^>]+>")
_BLOCK_TAG_RE = re.compile(r"</?(?:pre|blockquote)\b[^>]*>")

# What may name a language: it goes into an attribute of the block.
_LANGUAGE = r"[A-Za-z0-9_+#.-]{1,32}"
_LANGUAGE_RE = re.compile(_LANGUAGE + r"\Z")
# A text that says it is Python and is the report of an error is read as
# that: its lines are not Python.
_PYTHON = {"python", "py", "python3", "py3"}
_TRACEBACK = "Traceback (most recent call last)"

# In escaped HTML: a block of code, with the language Telegram's way, and
# one without.
_HTML_CODE_BLOCK_RE = re.compile(
    r"&lt;pre&gt;\s*&lt;code class=(?:&quot;|&#x27;)language-(" + _LANGUAGE + r")"
    r"(?:&quot;|&#x27;)&gt;(.*?)&lt;/code&gt;\s*&lt;/pre&gt;",
    re.S | re.I,
)
_HTML_PRE_RE = re.compile(r"&lt;pre&gt;(.*?)&lt;/pre&gt;", re.S | re.I)

# In MarkdownV2: a block of code between two fences, with its language on
# the line of the first if it names one, and code inside a line.
_FENCE_RE = re.compile(r"```(?:([A-Za-z0-9_+#.-]*)\n)?(.*?)```", re.S)
_INLINE_CODE_RE = re.compile(r"`((?:\\.|[^`\\\n])+)`")
# Any visible ASCII character can be written with a backslash before it,
# and then it is that character and no markup.
_ESCAPED_RE = re.compile(r"\\([\x21-\x7e])")
_LINK_RE = re.compile(r"\[([^\]\n]+)\]\((https?://[^\s)]+)\)")
# The marks of MarkdownV2, each with the tag it gives. Underline goes
# before italic: its mark holds the other.
_MARKS = [
    (re.compile(r"\|\|(?=\S)(.+?)(?<=\S)\|\|"), '<span class="spoiler">', "</span>"),
    (re.compile(r"__(?=\S)(.+?)(?<=\S)__"), "<u>", "</u>"),
    (re.compile(r"\*(?=\S)(.+?)(?<=\S)\*"), "<b>", "</b>"),
    (re.compile(r"_(?=\S)(.+?)(?<=\S)_"), "<i>", "</i>"),
    (re.compile(r"~(?=\S)(.+?)(?<=\S)~"), "<s>", "</s>"),
]
# Lines that start with ``>``, which is escaped by then, are a quotation.
_QUOTE_RE = re.compile(r"(?:^&gt;[^\n]*(?:\n|\Z))+", re.M)

# A block is a line of its own already: the line breaks next to it would
# add empty lines.
_AROUND_BLOCK_RE = re.compile(
    r"\n*(<(pre|blockquote)\b.*?</\2>)\n*", re.S  # the whole block
)


class _Kept:
    """Pieces of finished HTML, kept out of the way of what comes after.

    A piece is put back at the end, where ``keep`` left a mark for it.
    """

    _MARK_RE = re.compile("\x00(\\d+)\x00")

    def __init__(self) -> None:
        self._pieces: list[str] = []

    def keep(self, piece: str) -> str:
        """Keep a piece, and return the mark that stands for it."""
        self._pieces.append(piece)
        return f"\x00{len(self._pieces) - 1}\x00"

    def put_back(self, text: str) -> str:
        """Return the text with every piece in the place of its mark."""
        # A piece can hold the mark of another, so go on until none is left.
        for _ in range(len(self._pieces) + 1):
            text, found = self._MARK_RE.subn(
                lambda match: self._pieces[int(match.group(1))], text
            )
            if not found:
                break
        return text


def code_block(code: str, language: str = "") -> str:
    """Return a block of code as HTML, with colours if its language is known."""
    language = language.strip().lower()
    if not _LANGUAGE_RE.match(language):
        language = ""
    coloured = _coloured(code, language) if language else None
    inner = html.escape(code) if coloured is None else coloured
    label = f' data-lang="{language}"' if language else ""
    return f'<pre class="code"{label}><code>{inner}</code></pre>'


def _coloured(code: str, language: str) -> str | None:
    """Return the code with its parts marked, or ``None`` if that cannot be."""
    if len(code) > HIGHLIGHT_MAX:
        return None
    name = language
    if language in _PYTHON and code.lstrip().startswith(_TRACEBACK):
        name = "pytb"
    try:
        # As it is written: a lexer would take the empty lines at the ends.
        lexer = get_lexer_by_name(name, stripnl=False, ensurenl=False)
    except ClassNotFound:
        return None
    try:
        # The formatter ends the last line, which the block does itself.
        return highlight(code, lexer, HtmlFormatter(nowrap=True)).removesuffix("\n")
    except Exception:
        _logger.exception(f"Could not colour a block of {language} code.")
        return None


def _tidy(rendered: str) -> str:
    """Take out the line breaks next to a block."""
    return _AROUND_BLOCK_RE.sub(r"\1", rendered)


def _from_html(text: str) -> str:
    """Render a text that is written in Telegram's HTML."""
    kept = _Kept()
    escaped = html.escape(text.replace("\x00", ""))
    # The code first: what is inside is code, also where it looks like a tag.
    escaped = _HTML_CODE_BLOCK_RE.sub(
        lambda m: kept.keep(code_block(_written(m.group(2)), m.group(1))), escaped
    )
    escaped = _HTML_PRE_RE.sub(
        lambda m: kept.keep(code_block(_written(m.group(1)))), escaped
    )

    def restore(match: re.Match[str]) -> str:
        closing, tag, attrs = match.group(1), match.group(2).lower(), match.group(3)
        if tag in _ALLOWED_TAGS:
            if tag == "tg-spoiler":
                return "</span>" if closing else '<span class="spoiler">'
            return f"<{closing}{tag}>"
        if tag == "a":
            if closing:
                return "</a>"
            href = _HREF_RE.match(attrs or "")
            if href:
                return (
                    f'<a href="{href.group(1)}" target="_blank"'
                    ' rel="noopener noreferrer">'
                )
            return "<a>"
        return match.group(0)

    # Last, so that a tag that was written as text, in entities, stays text.
    restored = _ENTITY_RE.sub(r"&\1;", _TAG_RE.sub(restore, escaped))
    return _tidy(kept.put_back(restored))


def _written(escaped: str) -> str:
    """Return the code that a piece of escaped HTML stands for.

    Once for the escaping that was done here, and once for the entities
    that the text itself was written with: ``&lt;`` for ``<``.
    """
    return html.unescape(html.unescape(escaped))


def _code_text(written: str) -> str:
    """Return code as it is meant, from how MarkdownV2 has it written.

    Inside code only a backtick and a backslash are written with a
    backslash before them.
    """
    return re.sub(r"\\([`\\])", r"\1", written)


def _fenced(match: re.Match[str]) -> str:
    code = _code_text(match.group(2))
    # The line break before the closing fence is not part of the code.
    return code_block(code.removesuffix("\n"), match.group(1) or "")


def _from_markdown(text: str) -> str:
    """Render a text that is written in Telegram's MarkdownV2.

    A mark without its other half stays as it is written. Telegram would
    refuse such a text. The page shows it.
    """
    kept = _Kept()
    text = text.replace("\x00", "")
    text = _FENCE_RE.sub(lambda m: kept.keep(_fenced(m)), text)
    text = _INLINE_CODE_RE.sub(
        lambda m: kept.keep(f"<code>{html.escape(_code_text(m.group(1)))}</code>"),
        text,
    )
    text = _ESCAPED_RE.sub(lambda m: kept.keep(html.escape(m.group(1))), text)
    text = html.escape(text)
    text = _LINK_RE.sub(
        lambda m: kept.keep(
            f'<a href="{m.group(2)}" target="_blank" rel="noopener noreferrer">'
        )
        + m.group(1)
        + kept.keep("</a>"),
        text,
    )
    for mark, opening, closing in _MARKS:
        text = mark.sub(opening + r"\1" + closing, text)
    text = _QUOTE_RE.sub(_quoted, text)
    return _tidy(kept.put_back(text))


def _quoted(match: re.Match[str]) -> str:
    lines = [line.removeprefix("&gt;") for line in match.group(0).splitlines()]
    return "<blockquote>" + "\n".join(lines) + "</blockquote>\n"


def render_html(content: Content) -> str:
    """Render what is said to the user as safe HTML for the browser.

    A text that is sent as HTML keeps Telegram's tags (no attributes,
    except an http(s) ``href`` on ``<a>``) and the entities it was written
    with. One that is sent as Markdown is
    read as MarkdownV2. HTML goes first where both are asked for, as it
    does on Telegram. Any other text stays fully escaped. Newlines are
    kept by the page's ``white-space: pre-wrap``.
    """
    if content.parse_html:
        return _from_html(content.text)
    if content.parse_markdown:
        return _from_markdown(content.text)
    return html.escape(content.text)


def plain_text(content: Content, limit: int) -> str:
    """Return what is said as one line of plain text, for a notification.

    At most ``limit`` characters, with the markup taken out.
    """
    text = content.text
    if content.parse_html:
        text = html.unescape(_ANY_TAG_RE.sub("", text))
    elif content.parse_markdown:
        # A block is a line of its own, so it is set apart. The other
        # tags are inside a line and leave nothing.
        apart = _BLOCK_TAG_RE.sub(" ", render_html(content))
        text = html.unescape(_ANY_TAG_RE.sub("", apart))
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: limit - 1] + "…"
    return text
