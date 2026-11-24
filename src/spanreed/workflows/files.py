"""Blocks that hand files from one step to another: download one, send one.

A file is a value like any other. A step that gets files, such as the
attachments of an email, gives them as a list, and a step that takes a file
(write it into the vault, send it) reads its ``data``.

The download follows what a link in an email often leads through: HTTP
redirects, and pages whose only job is to send the browser on. It only
fetches from public addresses.
"""

from __future__ import annotations

import asyncio
import ipaddress
import logging
import re
import socket
from dataclasses import dataclass
from typing import Any
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

from spanreed.interaction import UserInteractionApi
from spanreed.workflows.blocks import Action, Block, BlockKind, NoOutput, StepContext

_logger = logging.getLogger(__name__)

# How many times a download goes on to another address: by an HTTP redirect,
# and by a page that only sends the browser on.
MAX_HTTP_REDIRECTS = 10
MAX_PAGE_REDIRECTS = 5
TIMEOUT_S = 60

_UNSAFE_IN_NAMES_RE = re.compile(r'[\\/:*?"<>|]')


@dataclass
class File:
    """A file that one step hands to another."""

    # The name of the file, without what a file name cannot hold.
    name: str
    # The name up to its extension, and the extension with its dot (or
    # nothing): ``receipt`` and ``.pdf``.
    stem: str
    extension: str
    # The MIME type, such as application/pdf.
    type: str
    data: bytes
    # In bytes.
    size: int


def safe_name(name: str) -> str:
    """Return a name without the characters that file names cannot hold."""
    return _UNSAFE_IN_NAMES_RE.sub("", name).strip()


def file_of(name: str, mime_type: str, data: bytes) -> File:
    """Return a file with a name that is safe to write it under."""
    name = safe_name(name)
    dot = name.rfind(".")
    stem, extension = (name[:dot], name[dot:]) if dot > 0 else (name, "")
    return File(
        name=name,
        stem=stem,
        extension=extension,
        type=mime_type,
        data=data,
        size=len(data),
    )


# -- downloading ---------------------------------------------------------------


class NotAFile(Exception):
    """A link does not lead to a file. Trying again would not help."""


@dataclass
class _Response:
    """The answer to one request."""

    status: int
    reason: str
    # The address that answered.
    url: str
    headers: dict[str, str]
    data: bytes


async def _check_public(url: str) -> None:
    """Raise ``NotAFile`` unless a URL is http(s) to a public address.

    A workflow runs on the server. Without this, a link in an email could
    make it fetch from the server's own network.
    """
    parts = urlparse(url)
    if parts.scheme not in ("http", "https") or not parts.hostname:
        raise NotAFile("Only http and https links can be downloaded.")
    port = parts.port or (443 if parts.scheme == "https" else 80)
    try:
        found = await asyncio.get_running_loop().getaddrinfo(
            parts.hostname, port, type=socket.SOCK_STREAM
        )
    except OSError:
        raise NotAFile(f"{parts.hostname} cannot be found.") from None
    for info in found:
        if not ipaddress.ip_address(info[4][0]).is_global:
            raise NotAFile("The link leads to an address that is not public.")


async def _get(url: str, max_bytes: int) -> _Response:
    """Make one request, without following a redirect.

    Raise ``NotAFile`` if the answer is larger than ``max_bytes``.
    """
    too_large = f"The file is larger than {max_bytes / (1024 * 1024):g} MB."
    timeout = aiohttp.ClientTimeout(total=TIMEOUT_S)
    async with aiohttp.ClientSession(timeout=timeout) as session:
        async with session.get(url, allow_redirects=False) as response:
            headers = {name.lower(): value for name, value in response.headers.items()}
            length = headers.get("content-length", "")
            if length.isdigit() and int(length) > max_bytes:
                raise NotAFile(too_large)
            data = bytearray()
            async for chunk in response.content.iter_chunked(8192):
                data.extend(chunk)
                if len(data) > max_bytes:
                    raise NotAFile(too_large)
            return _Response(
                status=response.status,
                reason=response.reason or "",
                url=str(response.url),
                headers=headers,
                data=bytes(data),
            )


def _resolve(url: str, base_url: str) -> str:
    """Return a URL from a page, made whole against the page's address."""
    return urljoin(base_url, url)


def page_redirect(page: str, base_url: str) -> str | None:
    """Return where an HTML page sends the browser on to, if anywhere.

    Look for a meta refresh, for a script that sets the location, and for
    a link whose text says to click, continue or download.
    """
    refresh = re.search(
        r'<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*'
        r'content=["\']?[^;"]*;?\s*url=([^"\'>\s]+)["\']?[^>]*>',
        page,
        re.IGNORECASE,
    )
    if refresh:
        return _resolve(refresh.group(1).strip(), base_url)
    for pattern in (
        r'window\.location\s*=\s*["\']([^"\']+)["\']',
        r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
        r'location\.href\s*=\s*["\']([^"\']+)["\']',
        r'document\.location\s*=\s*["\']([^"\']+)["\']',
    ):
        script = re.search(pattern, page, re.IGNORECASE)
        if script:
            return _resolve(script.group(1).strip(), base_url)
    for pattern in (
        r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>.*?(?:click|continue|redirect|download)',
        r'href=["\']([^"\']+)["\'][^>]*>.*?(?:here|download|continue)',
    ):
        link = re.search(pattern, page, re.IGNORECASE | re.DOTALL)
        if link:
            return _resolve(link.group(1).strip(), base_url)
    return None


def name_from(headers: dict[str, str], url: str) -> str:
    """Return the name a server gives a file, or the last part of its URL."""
    named = re.search(
        r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?",
        headers.get("content-disposition", ""),
        re.IGNORECASE,
    )
    if named:
        return named.group(1).strip()
    return unquote(urlparse(url).path.rsplit("/", 1)[-1]).split("?")[0]


def _is_page(response: _Response) -> bool:
    if not response.headers.get("content-type", "").startswith("text/html"):
        return False
    start = response.data[:200].decode("utf-8", errors="ignore").strip().lower()
    return start.startswith("<!doctype html") or start.startswith("<html")


def is_document(data: bytes, content_type: str) -> bool:
    """Return whether a download is a file and not a web page or a picture.

    A link that needs a login gives a page, and a page that sends the
    browser on can lead to the site's icon.
    """
    content_type = (content_type or "").lower()
    if content_type.startswith("text/html") or content_type.startswith("image/"):
        return False
    head = data[:32].lstrip()
    if head[:4] == b"\x00\x00\x01\x00":  # Windows .ico
        return False
    if head[:4] == b"\x89PNG" or head[:3] == b"GIF" or head[:2] == b"\xff\xd8":
        return False
    start = head[:14].lower()
    return not (start.startswith(b"<!doctype") or start.startswith(b"<html"))


async def download(url: str, max_size_mb: float) -> tuple[_Response, str]:
    """Fetch what a link leads to, and return the answer and its file name.

    Follow HTTP redirects, and pages that only send the browser on. Raise
    ``NotAFile`` if the link does not lead to a file of at most
    ``max_size_mb``. Any other error is one that may pass: the server did
    not answer, or failed.
    """
    max_bytes = int(max_size_mb * 1024 * 1024)
    http_hops = page_hops = 0
    while True:
        await _check_public(url)
        response = await _get(url, max_bytes)
        if response.status in (301, 302, 303, 307, 308):
            target = response.headers.get("location", "")
            http_hops += 1
            if not target or http_hops > MAX_HTTP_REDIRECTS:
                raise NotAFile("The link redirects too many times.")
            url = _resolve(target, response.url)
            continue
        if response.status >= 500:
            raise RuntimeError(f"HTTP {response.status}: {response.reason}")
        if response.status != 200:
            raise NotAFile(f"HTTP {response.status}: {response.reason}")
        if _is_page(response):
            onward = page_redirect(
                response.data.decode("utf-8", errors="ignore"), response.url
            )
            page_hops += 1
            if onward is None:
                raise NotAFile("The link gave a web page, not a file.")
            if page_hops > MAX_PAGE_REDIRECTS:
                raise NotAFile("The link redirects too many times.")
            url = onward
            continue
        return response, name_from(response.headers, response.url)


@dataclass
class DownloadFileInput:
    """Which link to download."""

    url: str
    max_size_mb: float = 10
    # The name of the file if the link gives none.
    name_if_none: str = "download"


@dataclass
class DownloadFileOutput(File):
    """The file, or why there is none."""

    # Whether the link led to a file. If not, the fields of the file are
    # empty and ``problem`` says why: a web page, a picture, too large.
    ok: bool
    problem: str


class DownloadFile(Action[DownloadFileInput, DownloadFileOutput]):
    """Download the file that a link leads to.

    A link that does not lead to a file does not fail the step: ``ok`` is
    no, and ``problem`` says why. The step fails, and the run is tried
    again, if the server does not answer or has an error of its own.
    """

    id = "core.download-file"
    title = "Download a file"

    async def run(
        self, ctx: StepContext, inputs: DownloadFileInput
    ) -> DownloadFileOutput:
        """Download the file, or say why the link does not lead to one."""
        try:
            response, name = await download(inputs.url, inputs.max_size_mb)
        except NotAFile as e:
            return _no_file(str(e))
        content_type = response.headers.get("content-type", "")
        if not is_document(response.data, content_type):
            # Its values stay out of the log: only what kind of thing it is.
            _logger.warning(f"A download gave {content_type!r}, not a file.")
            return _no_file(
                f"The link gave {content_type or 'something that is not a file'}, "
                "not a file."
            )
        name = name or inputs.name_if_none
        # A link often has no extension. Take it from the type of the file.
        if "." not in name and "pdf" in content_type.lower():
            name += ".pdf"
        file = file_of(name, content_type.split(";")[0].strip(), response.data)
        return DownloadFileOutput(**vars(file), ok=True, problem="")


def _no_file(problem: str) -> DownloadFileOutput:
    return DownloadFileOutput(
        name="",
        stem="",
        extension="",
        type="",
        data=b"",
        size=0,
        ok=False,
        problem=problem,
    )


@dataclass
class SendFileInput:
    """Which file to send, under which name."""

    name: str
    # The data of a file from another step, or a text.
    content: Any


class SendFile(Action[SendFileInput, NoOutput]):
    """Send the user a file, on the surfaces that reach them now."""

    id = "core.send-file"
    title = "Send file"
    kind = BlockKind.ASK

    async def run(self, ctx: StepContext, inputs: SendFileInput) -> NoOutput:
        """Send the file."""
        bot = await UserInteractionApi.for_user(ctx.user)
        await bot.send_document(inputs.name, as_bytes(inputs.content))
        return NoOutput()


def as_bytes(content: Any) -> bytes:
    """Return what a step gave as the content of a file, as bytes."""
    if isinstance(content, (bytes, bytearray)):
        return bytes(content)
    if content is None:
        return b""
    return str(content).encode("utf-8")


def file_blocks() -> list[Block]:
    """Return the blocks that Spanreed publishes for files."""
    return [DownloadFile(), SendFile()]
