from spanreed.apis.gmail import GmailApi


def test_collect_attachment_parts_finds_nested_attachment() -> None:
    # multipart/mixed wrapping a multipart/alternative body plus a PDF part.
    payload = {
        "mimeType": "multipart/mixed",
        "parts": [
            {
                "mimeType": "multipart/alternative",
                "parts": [
                    {"mimeType": "text/plain", "body": {"data": "x"}},
                    {"mimeType": "text/html", "body": {"data": "y"}},
                ],
            },
            {
                "mimeType": "application/pdf",
                "filename": "receipt.pdf",
                "body": {"attachmentId": "a1"},
            },
        ],
    }

    parts = GmailApi._collect_attachment_parts(payload)

    assert [p["filename"] for p in parts] == ["receipt.pdf"]


def test_collect_attachment_parts_none_when_no_filename() -> None:
    payload = {"mimeType": "text/plain", "body": {"data": "x"}}
    assert GmailApi._collect_attachment_parts(payload) == []
