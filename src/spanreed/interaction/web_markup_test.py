"""Tests of how what is said to the user becomes HTML for the web UI."""

import html
import re

import pytest

from spanreed.interaction.surface import Content
from spanreed.interaction.web_markup import (
    HIGHLIGHT_MAX,
    code_block,
    plain_text,
    render_html,
)


def _markdown(text: str) -> str:
    return render_html(Content(text, parse_html=False, parse_markdown=True))


def _text_of(rendered: str) -> str:
    """Return what a reader sees of some HTML: the text, without the tags."""
    return html.unescape(re.sub(r"<[^>]+>", "", rendered))


# -- the three ways a text is written -------------------------------------------


def test_html_keeps_telegram_tags_and_escapes_the_rest() -> None:
    rendered = render_html(
        Content("<b>Hi</b> <i>there</i> <script>alert(1)</script> <code>x</code>")
    )
    assert rendered == (
        "<b>Hi</b> <i>there</i> &lt;script&gt;alert(1)&lt;/script&gt; <code>x</code>"
    )


def test_html_drops_attributes_from_allowed_tags() -> None:
    assert render_html(Content('<b onclick="x()">a</b>')) == "<b>a</b>"


def test_html_links_only_http() -> None:
    ok = render_html(Content('<a href="https://x.test/p?a=1&b=2">go</a>'))
    assert ok == (
        '<a href="https://x.test/p?a=1&amp;b=2" target="_blank"'
        ' rel="noopener noreferrer">go</a>'
    )
    bad = render_html(Content('<a href="javascript:alert(1)">go</a>'))
    assert "javascript" not in bad.split(">")[0]
    assert bad.startswith("<a>")


def test_plain_text_is_fully_escaped() -> None:
    assert render_html(Content("<b>x</b>", parse_html=False)) == (
        "&lt;b&gt;x&lt;/b&gt;"
    )
    # Also what would be Markdown, when Markdown is not asked for.
    assert render_html(Content("```python\nx\n```", parse_html=False)) == (
        "```python\nx\n```"
    )


def test_html_goes_first_where_both_are_asked_for() -> None:
    both = Content("<b>*x*</b>", parse_html=True, parse_markdown=True)

    # As on Telegram.
    assert render_html(both) == "<b>*x*</b>"


def test_html_keeps_the_entities_it_was_written_with() -> None:
    # HTML for Telegram has to write these three as entities.
    assert render_html(Content("Tom &amp; Jerry &lt;3 &quot;x&quot; &#8594;")) == (
        "Tom &amp; Jerry &lt;3 &quot;x&quot; &#8594;"
    )
    # A tag that was written as text stays text.
    assert render_html(Content("&lt;b&gt;not bold&lt;/b&gt;")) == (
        "&lt;b&gt;not bold&lt;/b&gt;"
    )
    # And a bare one is escaped, as before.
    assert render_html(Content("a & b < c")) == "a &amp; b &lt; c"
    # In the address of a link too.
    assert 'href="https://x.test/?a=1&amp;b=2"' in render_html(
        Content('<a href="https://x.test/?a=1&amp;b=2">go</a>')
    )
    # Code says what it means either way.
    for written in ("if a &lt; b &amp;&amp; c", "if a < b && c"):
        assert _text_of(render_html(Content(f"<pre>{written}</pre>"))) == (
            "if a < b && c"
        )


# -- a block of code ------------------------------------------------------------


def test_a_fenced_block_that_names_its_language_is_coloured() -> None:
    rendered = _markdown(
        "Exception retrieved from storage:\n\n```python\n"
        "Obsidian plugin error: {'user': 1, 'message': 'read EADDRNOTAVAIL'}\n```"
    )

    # One block, on a line of its own: the line breaks around it are gone.
    assert rendered.startswith(
        'Exception retrieved from storage:<pre class="code" data-lang="python"><code>'
    )
    assert rendered.endswith("</code></pre>")
    assert "```" not in rendered
    # Its parts are marked: a string, and a number.
    assert "<span class=\"s1\">'user'</span>" in rendered
    assert '<span class="mi">1</span>' in rendered
    # And it reads as it was written.
    assert _text_of(rendered) == (
        "Exception retrieved from storage:"
        "Obsidian plugin error: {'user': 1, 'message': 'read EADDRNOTAVAIL'}"
    )


def test_code_is_shown_as_written_whatever_it_holds() -> None:
    code = (
        "if a < b and c > d:\n    return '<script>alert(1)</script>' & \"q\"\n\n\tdone"
    )

    for rendered in (
        _markdown(f"```python\n{code}\n```"),
        _markdown(f"```\n{code}\n```"),
        code_block(code, "python"),
        code_block(code),
    ):
        assert "<script>" not in rendered
        assert _text_of(rendered) == code


def test_the_report_of_an_error_is_read_as_one() -> None:
    report = (
        "Traceback (most recent call last):\n"
        '  File "/opt/spanreed/x.py", line 3, in refresh\n'
        "    response.raise_for_status()\n"
        "HTTPError: 503 Server Error"
    )

    rendered = code_block(report, "python")

    # The lexer for tracebacks, not for Python: the first line is one thing.
    assert '<span class="gt">Traceback (most recent call last):</span>' in rendered
    assert _text_of(rendered) == report


def test_a_block_without_a_language_or_with_one_nobody_knows_has_no_colours() -> None:
    assert _markdown("```\nx = 1\n```") == '<pre class="code"><code>x = 1</code></pre>'
    assert _markdown("```x = 1```") == '<pre class="code"><code>x = 1</code></pre>'
    # The name is shown all the same.
    assert _markdown("```nosuchlang\nx = 1\n```") == (
        '<pre class="code" data-lang="nosuchlang"><code>x = 1</code></pre>'
    )


def test_what_is_no_name_of_a_language_is_code() -> None:
    # A first line with more than a name on it is the first line of the code.
    assert _markdown("```print(1)\nprint(2)```") == (
        '<pre class="code"><code>print(1)\nprint(2)</code></pre>'
    )
    # And nothing but a name gets into the attribute.
    for language in ('py"><script>', "a b", "x" * 40, ""):
        assert "data-lang" not in code_block("x", language)
    assert 'data-lang="c++"' in code_block("int x;", " C++ ")


def test_very_long_code_is_shown_without_colours() -> None:
    code = "x = 1\n" * (HIGHLIGHT_MAX // 6 + 1)

    rendered = code_block(code, "python")

    assert "<span" not in rendered
    assert _text_of(rendered) == code


def test_several_blocks_and_the_text_between_them() -> None:
    rendered = _markdown("one\n```json\n[1]\n```\ntwo\n\n```\nplain\n```\nthree")

    assert re.sub(r"<span[^>]*>|</span>", "", rendered) == (
        'one<pre class="code" data-lang="json"><code>[1]</code></pre>'
        'two<pre class="code"><code>plain</code></pre>three'
    )


def test_a_fence_that_is_not_closed_stays_as_it_is_written() -> None:
    assert _markdown("see ```python\nx = 1") == "see ```python\nx = 1"


def test_code_in_html_is_a_block_too() -> None:
    rendered = render_html(
        Content(
            "The file:\n"
            '<pre><code class="language-json">{"a": [1, true], "b": "<b>"}</code></pre>\n'
            "<pre>plain <b>pre</b>\n  kept</pre>"
        )
    )

    first, second = re.findall(r"<pre.*?</pre>", rendered, re.S)
    assert first.startswith('<pre class="code" data-lang="json"><code>')
    assert '<span class="nt">"a"</span>' in first
    assert _text_of(first) == '{"a": [1, true], "b": "<b>"}'
    # Inside code a tag is text.
    assert (
        second
        == '<pre class="code"><code>plain &lt;b&gt;pre&lt;/b&gt;\n  kept</code></pre>'
    )
    assert rendered.startswith("The file:<pre")


# -- the rest of Markdown -------------------------------------------------------


def test_the_marks_of_markdown_become_tags() -> None:
    assert _markdown("*bold* _italic_ __under__ ~gone~ ||secret||") == (
        "<b>bold</b> <i>italic</i> <u>under</u> <s>gone</s>"
        ' <span class="spoiler">secret</span>'
    )
    assert _markdown("*bold _and italic_*") == "<b>bold <i>and italic</i></b>"


def test_code_inside_a_line_is_not_read_as_markdown() -> None:
    assert _markdown("run `a_b_c * 2 < 3` now") == (
        "run <code>a_b_c * 2 &lt; 3</code> now"
    )
    # Inside code, a backtick and a backslash are written with a backslash.
    assert _markdown(r"`a\`b\\c`") == r"<code>a`b\c</code>"
    assert _text_of(_markdown("```\na\\`b\\\\n\n```")) == "a`b\\n"


def test_a_character_with_a_backslash_before_it_is_that_character() -> None:
    assert _markdown(r"1\.5 is not \*bold\* and \_this\_ is \<b\>") == (
        "1.5 is not *bold* and _this_ is &lt;b&gt;"
    )


def test_a_mark_without_its_other_half_stays() -> None:
    assert _markdown("2 * 3 and snake_case and a ~ b") == (
        "2 * 3 and snake_case and a ~ b"
    )


def test_a_markdown_link_goes_to_http_only() -> None:
    assert _markdown("[the *run*](https://x.test/r?a=1&b=2)") == (
        '<a href="https://x.test/r?a=1&amp;b=2" target="_blank"'
        ' rel="noopener noreferrer">the <b>run</b></a>'
    )
    # Anything else is no link, and stays as it is written.
    assert _markdown("[x](javascript:alert(1))") == "[x](javascript:alert(1))"
    # A quote in an address cannot end the attribute.
    rendered = _markdown('[x](https://a.test/"onclick="x)')
    assert rendered.startswith('<a href="https://a.test/&quot;onclick=&quot;x"')


def test_lines_that_start_with_a_quote_mark_are_a_quotation() -> None:
    assert _markdown("he said:\n>one\n>two\nand left") == (
        "he said:<blockquote>one\ntwo</blockquote>and left"
    )


def test_markdown_never_lets_a_tag_through() -> None:
    rendered = _markdown("<script>alert(1)</script> *<img src=x onerror=y>*")

    assert "<script" not in rendered and "<img" not in rendered
    assert rendered == (
        "&lt;script&gt;alert(1)&lt;/script&gt; <b>&lt;img src=x onerror=y&gt;</b>"
    )


@pytest.mark.parametrize(
    "text", ["a\x000\x00b", "`x` \x000\x00", "\x00", "```\n\x001\x00\n```"]
)
def test_a_text_cannot_stand_in_for_a_kept_piece(text: str) -> None:
    # The marks that the renderer leaves for itself are not taken from a text.
    assert "\x00" not in _markdown(text)
    assert "\x00" not in render_html(Content(text))


# -- one line, for a notification -----------------------------------------------


def test_plain_text_strips_markup_and_shortens() -> None:
    assert plain_text(Content("<b>Did you</b>\n  exercise &amp; stretch?"), 240) == (
        "Did you exercise & stretch?"
    )
    assert plain_text(Content("<b>kept</b>", parse_html=False), 240) == "<b>kept</b>"
    assert len(plain_text(Content("x" * 1000), 240)) == 240


def test_plain_text_of_markdown_has_no_fences_and_no_marks() -> None:
    content = Content(
        "Exception retrieved from *storage*:\n\n```python\nValueError: boom\n```",
        parse_html=False,
        parse_markdown=True,
    )

    assert plain_text(content, 240) == (
        "Exception retrieved from storage: ValueError: boom"
    )
