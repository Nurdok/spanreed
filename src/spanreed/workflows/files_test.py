import asyncio
import socket
from typing import Any
from unittest.mock import AsyncMock, patch

import pytest

from spanreed.test_utils import mock_user_find_by_id, patch_user_interaction
from spanreed.workflows import files
from spanreed.workflows.blocks import BlockRegistry, RunInfo, StepContext
from spanreed.workflows.files import (
    DownloadFile,
    DownloadFileInput,
    NotAFile,
    SendFile,
    SendFileInput,
    _Response,
    as_bytes,
    download,
    file_blocks,
    file_of,
    is_document,
    name_from,
    page_redirect,
    safe_name,
)
from spanreed.workflows.testing import fake_plugin

PDF = b"%PDF-1.7 data"


def _context() -> StepContext:
    return StepContext(
        user=mock_user_find_by_id(4),
        workflow_id="wf_1",
        run=RunInfo(id="r1", manual=False),
    )


def _response(
    data: bytes = PDF,
    status: int = 200,
    url: str = "https://files.example/receipt.pdf",
    **headers: str,
) -> _Response:
    names = {name.replace("_", "-"): value for name, value in headers.items()}
    names.setdefault("content-type", "application/pdf")
    return _Response(status=status, reason="Reason", url=url, headers=names, data=data)


def _page(html: str, url: str = "https://shop.example/go") -> _Response:
    return _response(
        b"<!DOCTYPE html><html>" + html.encode() + b"</html>",
        url=url,
        content_type="text/html; charset=utf-8",
    )


def _download(*responses: _Response, url: str = "https://shop.example/go") -> Any:
    """Download ``url`` from a web that gives these answers, in order.

    Return what ``download`` returns, and the addresses that were asked.
    """
    asked: list[str] = []
    answers = list(responses)

    async def get(address: str, max_bytes: int) -> _Response:
        asked.append(address)
        return answers.pop(0)

    with (
        patch("spanreed.workflows.files._get", new=get),
        patch("spanreed.workflows.files._check_public", new=AsyncMock()),
    ):
        return asyncio.run(download(url, 10)), asked


def test_the_file_blocks_are_well_formed() -> None:
    registry = BlockRegistry()
    for block in file_blocks():
        registry.register(block, fake_plugin())
    assert [block.id for block in registry.all()] == [
        "core.download-file",
        "core.send-file",
    ]


# -- a file --------------------------------------------------------------------


def test_a_file_has_a_name_that_is_safe_to_write_it_under() -> None:
    file = file_of('Invoice: "May"/2026?.pdf', "application/pdf", PDF)

    assert (file.name, file.stem, file.extension) == (
        "Invoice May2026.pdf",
        "Invoice May2026",
        ".pdf",
    )
    assert (file.type, file.data, file.size) == ("application/pdf", PDF, len(PDF))
    assert file_of("README", "text/plain", b"").extension == ""
    assert safe_name("  a<b>|c  ") == "abc"


def test_the_content_of_a_file_is_bytes_whatever_a_step_gave() -> None:
    assert as_bytes(PDF) == PDF
    assert as_bytes("שלום") == "שלום".encode("utf-8")
    assert as_bytes(None) == b""
    assert as_bytes(5) == b"5"


# -- downloading ---------------------------------------------------------------


def test_a_link_to_a_file_gives_the_file_and_its_name() -> None:
    (response, name), asked = _download(_response())

    assert (response.data, name) == (PDF, "receipt.pdf")
    assert asked == ["https://shop.example/go"]


def test_http_redirects_are_followed() -> None:
    (response, _), asked = _download(
        _response(b"", status=302, location="/files/1"),
        _response(b"", status=301, location="https://files.example/receipt.pdf"),
        _response(),
    )

    assert response.data == PDF
    assert asked == [
        "https://shop.example/go",
        "https://files.example/files/1",
        "https://files.example/receipt.pdf",
    ]


def test_a_page_that_sends_the_browser_on_is_followed() -> None:
    (response, _), asked = _download(
        _page('<meta http-equiv="refresh" content="0;url=/download?id=7">'),
        _response(),
    )

    assert response.data == PDF
    assert asked[1] == "https://shop.example/download?id=7"


def test_a_page_that_leads_nowhere_is_not_a_file() -> None:
    with pytest.raises(NotAFile, match="a web page, not a file"):
        _download(_page("<p>Please log in.</p>"))


def test_a_link_that_goes_in_circles_is_given_up() -> None:
    forever = [_response(b"", status=302, location="/again")] * 20
    with pytest.raises(NotAFile, match="redirects too many times"):
        _download(*forever)

    pages = [_page('<script>window.location = "/next"</script>')] * 20
    with pytest.raises(NotAFile, match="redirects too many times"):
        _download(*pages)


def test_a_link_that_is_gone_is_not_a_file_and_a_server_error_may_pass() -> None:
    with pytest.raises(NotAFile, match="HTTP 404"):
        _download(_response(b"", status=404))
    # Trying again can help: the step fails, and the run is tried again.
    with pytest.raises(RuntimeError, match="HTTP 503"):
        _download(_response(b"", status=503))


def test_where_a_page_sends_the_browser() -> None:
    base = "https://shop.example/a/b"
    assert (
        page_redirect(
            '<meta http-equiv="refresh" content="0; url=//cdn.example/f">', base
        )
        == "https://cdn.example/f"
    )
    assert (
        page_redirect('<script>location.href = "file.pdf"</script>', base)
        == "https://shop.example/a/file.pdf"
    )
    assert (
        page_redirect('<a href="/dl/9">Click here to download</a>', base)
        == "https://shop.example/dl/9"
    )
    assert page_redirect("<p>Nothing here.</p>", base) is None


def test_the_name_comes_from_the_server_or_from_the_address() -> None:
    disposition = {"content-disposition": 'attachment; filename="Invoice 7.pdf"'}
    assert name_from(disposition, "https://x.example/dl?id=7") == "Invoice 7.pdf"
    assert name_from({}, "https://x.example/files/May%20receipt.pdf?x=1") == (
        "May receipt.pdf"
    )
    assert name_from({}, "https://x.example/") == ""


def test_a_page_or_a_picture_is_not_a_document() -> None:
    assert is_document(PDF, "application/pdf")
    assert is_document(b"PK\x03\x04", "application/octet-stream")
    assert not is_document(b"<html>", "text/html")
    assert not is_document(b"  <!DOCTYPE html>", "application/octet-stream")
    assert not is_document(b"\x89PNG....", "application/octet-stream")
    assert not is_document(b"\x00\x00\x01\x00", "")
    assert not is_document(PDF, "image/png")


def _addresses(*addresses: str) -> Any:
    found = [(socket.AF_INET, socket.SOCK_STREAM, 6, "", (a, 443)) for a in addresses]

    async def check(url: str) -> None:
        loop = asyncio.get_running_loop()
        with patch.object(loop, "getaddrinfo", new=AsyncMock(return_value=found)):
            await files._check_public(url)

    return check


def test_only_public_addresses_are_fetched_from() -> None:
    asyncio.run(_addresses("93.184.216.34")("https://example.com/file.pdf"))
    for address in ("127.0.0.1", "10.0.0.5", "192.168.1.1", "169.254.169.254"):
        with pytest.raises(NotAFile, match="not public"):
            asyncio.run(_addresses(address)("https://internal.example/file.pdf"))
    # One address that is not public is enough.
    with pytest.raises(NotAFile, match="not public"):
        asyncio.run(_addresses("93.184.216.34", "10.0.0.5")("https://x.example/"))
    for url in ("ftp://example.com/file", "file:///etc/passwd", "https:///nothing"):
        with pytest.raises(NotAFile, match="Only http and https"):
            asyncio.run(_addresses("93.184.216.34")(url))


def test_every_address_on_the_way_is_checked() -> None:
    checked: list[str] = []
    answers = [
        _response(b"", status=302, location="http://10.0.0.5/secret"),
        _response(),
    ]

    async def get(address: str, max_bytes: int) -> _Response:
        return answers.pop(0)

    async def check(address: str) -> None:
        checked.append(address)
        if "10.0.0.5" in address:
            raise NotAFile("The link leads to an address that is not public.")

    with (
        patch("spanreed.workflows.files._get", new=get),
        patch("spanreed.workflows.files._check_public", new=check),
        pytest.raises(NotAFile, match="not public"),
    ):
        asyncio.run(download("https://shop.example/go", 10))

    assert checked == ["https://shop.example/go", "http://10.0.0.5/secret"]


# -- the blocks ----------------------------------------------------------------


def _run_download(result: Any, **inputs: Any) -> Any:
    values: dict[str, Any] = {"url": "https://shop.example/go"}
    values.update(inputs)
    fake = AsyncMock(side_effect=result if isinstance(result, Exception) else None)
    if not isinstance(result, Exception):
        fake.return_value = result
    with patch("spanreed.workflows.files.download", new=fake):
        output = asyncio.run(
            DownloadFile().run(_context(), DownloadFileInput(**values))
        )
    fake.assert_awaited_once_with(values["url"], values.get("max_size_mb", 10))
    return output


def test_download_gives_the_file() -> None:
    output = _run_download((_response(content_type="application/pdf; x=y"), "May.pdf"))

    assert (output.ok, output.problem) == (True, "")
    assert (output.name, output.stem, output.extension) == ("May.pdf", "May", ".pdf")
    assert (output.type, output.data, output.size) == ("application/pdf", PDF, len(PDF))


def test_download_names_a_file_that_has_no_name() -> None:
    output = _run_download((_response(), ""), name_if_none="download-2")
    # A link often has no extension: the type says that it is a PDF.
    assert output.name == "download-2.pdf"

    output = _run_download((_response(content_type="application/zip"), "archive"))
    assert output.name == "archive"


def test_a_link_that_is_no_file_does_not_fail_the_step() -> None:
    output = _run_download(NotAFile("The link gave a web page, not a file."))

    assert (output.ok, output.problem) == (
        False,
        "The link gave a web page, not a file.",
    )
    assert (output.name, output.data, output.size) == ("", b"", 0)


def test_a_picture_in_place_of_a_file_is_not_taken() -> None:
    icon = _response(b"\x89PNG....", content_type="image/png")

    output = _run_download((icon, "favicon.png"))

    assert (output.ok, output.problem) == (
        False,
        "The link gave image/png, not a file.",
    )
    assert output.data == b""


def test_a_server_that_fails_fails_the_step() -> None:
    with pytest.raises(RuntimeError, match="HTTP 503"):
        _run_download(RuntimeError("HTTP 503: Service Unavailable"))


@patch_user_interaction("spanreed.workflows.blocks")
def test_send_file_sends_the_data_under_the_name(mock_bot: AsyncMock) -> None:
    asyncio.run(SendFile().run(_context(), SendFileInput(name="May.pdf", content=PDF)))
    asyncio.run(SendFile().run(_context(), SendFileInput(name="a.txt", content="hi")))

    assert [call.args for call in mock_bot.send_document.await_args_list] == [
        ("May.pdf", PDF),
        ("a.txt", b"hi"),
    ]
