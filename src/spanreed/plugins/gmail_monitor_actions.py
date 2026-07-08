import abc
import re
import html
import logging
import pathlib
from typing import Any, Dict, List, Optional
from dataclasses import dataclass
import aiohttp

from spanreed.user import User
from spanreed.apis.gmail import EmailMessage, GmailApi
from spanreed.apis.obsidian import ObsidianApi
from spanreed.apis.telegram_bot import TelegramBotApi


@dataclass
class EmailMatch:
    email: EmailMessage
    rule_name: str


class EmailActionHandler(abc.ABC):
    @abc.abstractmethod
    async def execute(
        self, email_match: EmailMatch, config: Dict[str, Any], user: User
    ) -> None:
        pass


class TelegramNotificationAction(EmailActionHandler):
    async def execute(
        self, email_match: EmailMatch, config: Dict[str, Any], user: User
    ) -> None:
        bot = await TelegramBotApi.for_user(user)

        email = email_match.email
        rule_name = email_match.rule_name

        # Get configuration with defaults
        include_body = config.get("include_body", False)
        include_snippet = config.get("include_snippet", True)
        custom_message = config.get("custom_message", "")

        # Build notification message
        message_parts = []

        if custom_message:
            message_parts.append(html.escape(custom_message))
        else:
            message_parts.append(
                f"📧 Email matched rule: <b>{html.escape(rule_name)}</b>"
            )

        message_parts.append(f"<b>From:</b> {html.escape(email.sender)}")
        message_parts.append(f"<b>Subject:</b> {html.escape(email.subject)}")
        message_parts.append(f"<b>Date:</b> {email.date.strftime('%Y-%m-%d %H:%M:%S')}")

        if email.has_attachments:
            message_parts.append("📎 Has attachments")

        if include_snippet and email.snippet:
            message_parts.append(f"<b>Preview:</b> {html.escape(email.snippet)}")

        if include_body and email.body:
            # Limit body length to avoid telegram message limits
            body_preview = email.body[:500]
            if len(email.body) > 500:
                body_preview += "..."
            message_parts.append(f"<b>Body:</b> {html.escape(body_preview)}")

        message = "\n\n".join(message_parts)

        await bot.send_message(message)


class DownloadAttachmentAction(EmailActionHandler):
    """Download the email's file attachments and send them over Telegram.

    Unlike ``DownloadLinkAction`` (which scrapes URLs out of the body), this
    fetches the actual MIME attachments via the Gmail API.
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)

    async def execute(
        self, email_match: EmailMatch, config: Dict[str, Any], user: User
    ) -> None:
        bot = await TelegramBotApi.for_user(user)
        email = email_match.email
        rule_name = email_match.rule_name

        # `mime_types = None` means "all attachments". Default to PDFs only.
        mime_types = config.get("mime_types", ["application/pdf"])

        gmail = await GmailApi.for_user(user)
        try:
            attachments = await gmail.get_attachments(email.id, mime_types=mime_types)
        except Exception as e:
            self._logger.error(f"Failed to fetch attachments for {email.id}: {e}")
            await bot.send_message(
                f"❌ Failed to fetch attachments from "
                f"{html.escape(email.sender)}: {str(e)}"
            )
            return

        if not attachments:
            await bot.send_message(
                f"📎 No matching attachment found in email from "
                f"{html.escape(email.sender)}"
            )
            return

        for attachment in attachments:
            try:
                await bot.send_document(attachment.filename, attachment.data)
                await bot.send_message(
                    f"📄 {html.escape(attachment.filename)} from "
                    f"{html.escape(email.sender)}\n"
                    f"🏷️ Rule: {html.escape(rule_name)}"
                )
            except Exception as e:
                self._logger.error(
                    f"Failed to send attachment {attachment.filename}: {e}"
                )
                await bot.send_message(
                    f"❌ Failed to send {html.escape(attachment.filename)}: "
                    f"{str(e)}"
                )


class SaveAttachmentToVaultAction(EmailActionHandler):
    """Save the email's attachments into a folder in the Obsidian vault.

    Config parameters:
      - ``vault_dir``: destination folder in the vault (e.g. "Receipts").
        Empty means the vault root.
      - ``filename_template``: how to name the saved file. Placeholders:
        ``{original}`` (the attachment's own name), ``{sender}``, ``{subject}``,
        ``{date}``, ``{index}``. Defaults to ``{original}``. The original
        extension is appended if the rendered name lacks it.
      - ``mime_types``: which attachments to save (default ``["application/pdf"]``;
        ``None`` saves all).
      - ``overwrite``: overwrite an existing file (default False -> skip).
    """

    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)

    async def execute(
        self, email_match: EmailMatch, config: Dict[str, Any], user: User
    ) -> None:
        bot = await TelegramBotApi.for_user(user)
        email = email_match.email
        rule_name = email_match.rule_name

        vault_dir = str(config.get("vault_dir", "")).strip().strip("/")
        filename_template = config.get("filename_template") or "{original}"
        mime_types = config.get("mime_types", ["application/pdf"])
        overwrite = bool(config.get("overwrite", False))

        gmail = await GmailApi.for_user(user)
        try:
            attachments = await gmail.get_attachments(email.id, mime_types=mime_types)
        except Exception as e:
            self._logger.error(f"Failed to fetch attachments for {email.id}: {e}")
            await bot.send_message(
                f"❌ Failed to fetch attachments from "
                f"{html.escape(email.sender)}: {str(e)}"
            )
            return

        if not attachments:
            await bot.send_message(
                f"📎 No matching attachment to save from "
                f"{html.escape(email.sender)}"
            )
            return

        obsidian = await ObsidianApi.for_user(user)
        for index, attachment in enumerate(attachments, start=1):
            filename = self._build_filename(
                filename_template, attachment.filename, email, index
            )
            filepath = (
                str(pathlib.PurePosixPath(vault_dir) / filename)
                if vault_dir
                else filename
            )
            try:
                await obsidian.write_binary_file(
                    filepath, attachment.data, overwrite=overwrite
                )
            except FileExistsError:
                await bot.send_message(
                    f"⚠️ {html.escape(filepath)} already exists; skipped."
                )
                continue
            except Exception as e:
                self._logger.error(f"Failed to save {filepath}: {e}")
                await bot.send_message(
                    f"❌ Failed to save {html.escape(filename)}: {str(e)}"
                )
                continue

            await bot.send_message(
                f"💾 Saved {html.escape(filepath)} " f"(rule: {html.escape(rule_name)})"
            )

    @staticmethod
    def _build_filename(
        template: str, original: str, email: EmailMessage, index: int
    ) -> str:
        original = original or f"attachment-{index}"
        try:
            rendered = template.format(
                original=original,
                sender=(
                    email.sender.split("@")[0] if "@" in email.sender else email.sender
                ),
                subject=re.sub(r"[^\w\-. ]", "_", email.subject)[:50],
                date=email.date.strftime("%Y-%m-%d"),
                index=index,
            )
        except (KeyError, IndexError, ValueError):
            # A bad template shouldn't lose the file; fall back to the original.
            rendered = original

        # Strip path separators and characters illegal on common filesystems.
        rendered = re.sub(r'[\\/:*?"<>|]', "", rendered).strip()
        if not rendered:
            rendered = original

        # Preserve the original extension if the template dropped it.
        if "." in original:
            ext = original[original.rfind(".") :]
            if not rendered.lower().endswith(ext.lower()):
                rendered += ext
        return rendered


class DownloadLinkAction(EmailActionHandler):
    def __init__(self) -> None:
        self._logger = logging.getLogger(__name__)

    async def execute(
        self, email_match: EmailMatch, config: Dict[str, Any], user: User
    ) -> None:
        bot = await TelegramBotApi.for_user(user)
        email = email_match.email
        rule_name = email_match.rule_name

        # Get configuration
        url_regex = config.get("url_regex", r'https?://[^\s<>"\']+')
        text_regex = config.get("text_regex", None)
        custom_filename = config.get("custom_filename", None)
        max_file_size_mb = config.get("max_file_size_mb", 10)

        try:
            # Extract links from email body
            links = await self._extract_links_from_email(email, url_regex, text_regex)

            if not links:
                await bot.send_message(
                    f"🔗 No matching links found in email from {html.escape(email.sender)}"
                )
                return

            # Download and send each found link
            for i, link in enumerate(links[:3]):  # Limit to first 3 files
                try:
                    # Show the specific link being processed
                    link_preview = link[:100] + "..." if len(link) > 100 else link
                    await bot.send_message(
                        f"🔗 Processing link {i+1}: <code>{html.escape(link_preview)}</code>"
                    )

                    filename = await self._download_and_send_file(
                        bot,
                        link,
                        email,
                        rule_name,
                        custom_filename,
                        max_file_size_mb,
                        i,
                    )
                    if filename:
                        await bot.send_message(f"📄 Downloaded and sent: {filename}")
                except Exception as e:
                    self._logger.error(f"Failed to download file from {link}: {e}")
                    link_preview = link[:100] + "..." if len(link) > 100 else link
                    await bot.send_message(
                        f"❌ Failed to download from <code>{html.escape(link_preview)}</code>: {str(e)}"
                    )

        except Exception as e:
            self._logger.error(f"Download link action failed: {e}")
            await bot.send_message(f"❌ Download failed: {str(e)}")

    async def _extract_links_from_email(
        self, email: EmailMessage, url_regex: str, text_regex: Optional[str]
    ) -> List[str]:
        """Extract links from email body based on URL and text regex patterns"""

        # First, find ALL URLs in the email body using a simple pattern
        all_urls_pattern = re.compile(r'https?://[^\s<>"\']+', re.IGNORECASE)
        all_urls = all_urls_pattern.findall(email.body)

        # Now filter based on the configured url_regex
        url_pattern = re.compile(url_regex, re.IGNORECASE)
        matching_urls = [url for url in all_urls if url_pattern.search(url)]

        # If no text regex specified, return all matching URLs
        if not text_regex:
            return matching_urls

        # If text regex specified, filter URLs based on surrounding text
        text_pattern = re.compile(text_regex, re.IGNORECASE)
        filtered_urls = []

        for url in matching_urls:
            # Look for the URL in the email body and check surrounding text
            # Find the position of this URL in the email body
            url_pos = email.body.find(url)
            if url_pos != -1:
                # Get some context around the URL (100 chars before and after)
                start = max(0, url_pos - 100)
                end = min(len(email.body), url_pos + len(url) + 100)
                context = email.body[start:end]

                # Check if the text pattern matches anywhere in this context
                if text_pattern.search(context):
                    filtered_urls.append(url)

        return filtered_urls

    async def _download_and_send_file(
        self,
        bot: TelegramBotApi,
        url: str,
        email: EmailMessage,
        rule_name: str,
        custom_filename: Optional[str],
        max_file_size_mb: int,
        index: int,
        redirect_count: int = 0,
    ) -> Optional[str]:
        """Download file from URL and send via Telegram"""

        # Prevent infinite redirects
        if redirect_count > 5:
            raise Exception("Too many redirects (maximum 5)")

        # Generate filename
        if custom_filename:
            # Replace placeholders in custom filename
            filename = custom_filename.format(
                sender=(
                    email.sender.split("@")[0] if "@" in email.sender else email.sender
                ),
                subject=re.sub(r"[^\w\-_.]", "_", email.subject)[:50],
                date=email.date.strftime("%Y-%m-%d"),
                rule=rule_name,
                index=index,
            )
        else:
            # Extract filename from URL or use default
            url_filename = url.split("/")[-1].split("?")[0]
            if url_filename and "." in url_filename:
                filename = url_filename
            else:
                filename = f"document_{email.date.strftime('%Y-%m-%d')}_{index}"

        # Download the file with redirect following
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}: {response.reason}")

                # Debug info about the response
                content_type = response.headers.get("content-type", "unknown")
                content_length = response.headers.get("content-length")

                self._logger.info(
                    f"Downloading from {url}: Content-Type: {content_type}, Content-Length: {content_length}"
                )

                if (
                    content_length
                    and int(content_length) > max_file_size_mb * 1024 * 1024
                ):
                    raise Exception(
                        f"File too large: {int(content_length) / (1024*1024):.1f}MB > {max_file_size_mb}MB"
                    )

                # Download file content
                downloaded_size = 0
                max_size = max_file_size_mb * 1024 * 1024
                file_data = bytearray()

                async for chunk in response.content.iter_chunked(8192):
                    downloaded_size += len(chunk)
                    if downloaded_size > max_size:
                        raise Exception(
                            f"File too large: {downloaded_size / (1024*1024):.1f}MB > {max_file_size_mb}MB"
                        )
                    file_data.extend(chunk)

                # Check if we got HTML instead of the expected file (common redirect issue)
                if content_type.startswith("text/html") and downloaded_size > 0:
                    # Peek at first 200 chars to see if it's HTML
                    preview = file_data[:200].decode("utf-8", errors="ignore")
                    if preview.strip().lower().startswith(
                        "<!doctype html"
                    ) or preview.strip().lower().startswith("<html"):
                        self._logger.warning(
                            f"Got HTML response instead of file from {url}, checking for redirects"
                        )

                        # Try to parse the HTML for redirects
                        html_content = file_data.decode("utf-8", errors="ignore")
                        redirect_url = self._extract_redirect_from_html(
                            html_content, url
                        )

                        if redirect_url:
                            self._logger.info(f"Found redirect in HTML: {redirect_url}")
                            await bot.send_message(
                                f"🔄 Following redirect to: <code>{html.escape(redirect_url[:100])}{'...' if len(redirect_url) > 100 else ''}</code>"
                            )
                            # Follow the redirect (recursive call with redirect URL)
                            return await self._download_and_send_file(
                                bot,
                                redirect_url,
                                email,
                                rule_name,
                                custom_filename,
                                max_file_size_mb,
                                index,
                                redirect_count + 1,
                            )
                        else:
                            raise Exception(
                                f"URL returned HTML page instead of file (Content-Type: {content_type})"
                            )

        final_data = bytes(file_data)

        # Send debug info about the downloaded file
        debug_info = (
            f"📊 File info: {len(final_data)} bytes, Content-Type: {content_type}"
        )
        await bot.send_message(debug_info)

        # Send via Telegram
        await bot.send_document(filename, final_data)

        # Send additional info as separate message
        await bot.send_message(
            f"📄 File from {html.escape(email.sender)}\n📧 {html.escape(email.subject)}\n🏷️ Rule: {html.escape(rule_name)}"
        )

        return filename

    def _extract_redirect_from_html(
        self, html_content: str, base_url: str
    ) -> Optional[str]:
        """Extract redirect URL from HTML content"""
        try:
            # Case 1: Meta refresh redirect
            # <meta http-equiv="refresh" content="0;url=https://example.com">
            meta_refresh_match = re.search(
                r'<meta[^>]*http-equiv=["\']?refresh["\']?[^>]*content=["\']?[^;"]*;?\s*url=([^"\'>\s]+)["\']?[^>]*>',
                html_content,
                re.IGNORECASE,
            )
            if meta_refresh_match:
                redirect_url = meta_refresh_match.group(1).strip()
                return self._resolve_url(redirect_url, base_url)

            # Case 2: JavaScript window.location redirect
            # window.location = "https://example.com"
            # window.location.href = "https://example.com"
            js_location_patterns = [
                r'window\.location\s*=\s*["\']([^"\']+)["\']',
                r'window\.location\.href\s*=\s*["\']([^"\']+)["\']',
                r'location\.href\s*=\s*["\']([^"\']+)["\']',
                r'document\.location\s*=\s*["\']([^"\']+)["\']',
            ]

            for pattern in js_location_patterns:
                match = re.search(pattern, html_content, re.IGNORECASE)
                if match:
                    redirect_url = match.group(1).strip()
                    return self._resolve_url(redirect_url, base_url)

            # Case 3: HTML link with "click here" or auto-redirect text
            # Look for links that might be the redirect target
            link_patterns = [
                r'<a[^>]*href=["\']([^"\']+)["\'][^>]*>.*?(?:click|continue|redirect|download)',
                r'href=["\']([^"\']+)["\'][^>]*>.*?(?:here|download|continue)',
            ]

            for pattern in link_patterns:
                match = re.search(pattern, html_content, re.IGNORECASE | re.DOTALL)
                if match:
                    redirect_url = match.group(1).strip()
                    return self._resolve_url(redirect_url, base_url)

            return None
        except Exception as e:
            self._logger.warning(f"Error parsing HTML for redirects: {e}")
            return None

    def _resolve_url(self, url: str, base_url: str) -> str:
        """Resolve relative URLs against base URL"""
        if url.startswith("http://") or url.startswith("https://"):
            return url
        elif url.startswith("//"):
            # Protocol-relative URL
            base_protocol = "https:" if base_url.startswith("https:") else "http:"
            return base_protocol + url
        elif url.startswith("/"):
            # Absolute path
            from urllib.parse import urlparse

            parsed_base = urlparse(base_url)
            return f"{parsed_base.scheme}://{parsed_base.netloc}{url}"
        else:
            # Relative path
            from urllib.parse import urljoin

            return urljoin(base_url, url)


class SaveLinkToVaultAction(DownloadLinkAction):
    """Download the file behind a link in the email body and save it to the vault.

    The link-delivered counterpart to ``SaveAttachmentToVaultAction``: it
    reuses ``DownloadLinkAction``'s link extraction and HTML-redirect following
    (e.g. iCount links that bounce through a redirect page), but writes the
    downloaded bytes into the Obsidian vault instead of sending them to
    Telegram.

    Config parameters:
      - ``vault_dir``: destination folder in the vault.
      - ``url_regex`` / ``text_regex``: which body links to download (same
        semantics as ``download_link``).
      - ``filename_template``: naming, as in ``save_attachment_to_vault``.
      - ``max_file_size_mb``: download cap (default 10).
      - ``overwrite``: overwrite an existing file (default False -> skip).
    """

    async def execute(
        self, email_match: EmailMatch, config: Dict[str, Any], user: User
    ) -> None:
        bot = await TelegramBotApi.for_user(user)
        email = email_match.email
        rule_name = email_match.rule_name

        url_regex = config.get("url_regex", r'https?://[^\s<>"\']+')
        text_regex = config.get("text_regex", None)
        vault_dir = str(config.get("vault_dir", "")).strip().strip("/")
        filename_template = config.get("filename_template") or "{original}"
        max_file_size_mb = config.get("max_file_size_mb", 10)
        overwrite = bool(config.get("overwrite", False))

        links = await self._extract_links_from_email(email, url_regex, text_regex)
        if not links:
            await bot.send_message(
                f"🔗 No matching links to save from " f"{html.escape(email.sender)}"
            )
            return

        obsidian = await ObsidianApi.for_user(user)
        for index, link in enumerate(links[:3], start=1):  # cap like download
            try:
                data, content_type, suggested = await self._download_bytes(
                    link, max_file_size_mb
                )
            except Exception as e:
                self._logger.error(f"Failed to download {link}: {e}")
                await bot.send_message(
                    f"❌ Failed to download from "
                    f"{html.escape(link[:100])}: {str(e)}"
                )
                continue

            if not self._is_document(data, content_type):
                # e.g. a session-gated link that returns an HTML page, or the
                # redirect heuristic having grabbed a favicon/logo. Don't save
                # the garbage — tell the user to grab it manually.
                self._logger.warning(
                    f"Link returned non-document content "
                    f"({content_type!r}) for {link}"
                )
                await bot.send_message(
                    f"⚠️ The link for “{html.escape(rule_name)}” returned "
                    f"{html.escape(content_type or 'non-document content')}, "
                    f"not a file — not saved. Open it manually:\n"
                    f"{html.escape(link[:150])}"
                )
                continue

            original = suggested or f"download-{index}"
            # Links often lack a file extension; infer .pdf from the type.
            if "." not in original and "pdf" in content_type.lower():
                original += ".pdf"
            filename = SaveAttachmentToVaultAction._build_filename(
                filename_template, original, email, index
            )
            filepath = (
                str(pathlib.PurePosixPath(vault_dir) / filename)
                if vault_dir
                else filename
            )
            try:
                await obsidian.write_binary_file(filepath, data, overwrite=overwrite)
            except FileExistsError:
                await bot.send_message(
                    f"⚠️ {html.escape(filepath)} already exists; skipped."
                )
                continue
            except Exception as e:
                self._logger.error(f"Failed to save {filepath}: {e}")
                await bot.send_message(
                    f"❌ Failed to save {html.escape(filename)}: {str(e)}"
                )
                continue

            await bot.send_message(
                f"💾 Saved {html.escape(filepath)} " f"(rule: {html.escape(rule_name)})"
            )

    async def _download_bytes(
        self, url: str, max_file_size_mb: int, redirect_count: int = 0
    ) -> tuple[bytes, str, str]:
        """Download ``url``, following HTML redirects. Returns (data, type, name)."""
        if redirect_count > 5:
            raise Exception("Too many redirects (maximum 5)")

        max_size = max_file_size_mb * 1024 * 1024
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}: {response.reason}")
                content_type = response.headers.get("content-type", "")
                suggested = self._filename_from_headers(
                    response.headers, str(response.url)
                )
                content_length = response.headers.get("content-length")
                if content_length and int(content_length) > max_size:
                    raise Exception(
                        f"File too large: "
                        f"{int(content_length) / (1024 * 1024):.1f}MB > "
                        f"{max_file_size_mb}MB"
                    )
                downloaded = 0
                data = bytearray()
                async for chunk in response.content.iter_chunked(8192):
                    downloaded += len(chunk)
                    if downloaded > max_size:
                        raise Exception(
                            f"File too large: "
                            f"{downloaded / (1024 * 1024):.1f}MB > "
                            f"{max_file_size_mb}MB"
                        )
                    data.extend(chunk)

        # Some links return an HTML redirect page rather than the file itself.
        if content_type.startswith("text/html") and len(data) > 0:
            preview = data[:200].decode("utf-8", errors="ignore").strip().lower()
            if preview.startswith("<!doctype html") or preview.startswith("<html"):
                redirect_url = self._extract_redirect_from_html(
                    data.decode("utf-8", errors="ignore"), url
                )
                if redirect_url:
                    return await self._download_bytes(
                        redirect_url, max_file_size_mb, redirect_count + 1
                    )
                raise Exception("URL returned an HTML page instead of a file")

        return bytes(data), content_type, suggested

    @staticmethod
    def _filename_from_headers(headers: Any, final_url: str) -> str:
        """Best-effort filename: Content-Disposition, else the URL basename."""
        cd = headers.get("content-disposition", "") if headers else ""
        match = re.search(r"filename\*?=(?:UTF-8'')?\"?([^\";]+)\"?", cd, re.IGNORECASE)
        if match:
            return match.group(1).strip()
        from urllib.parse import urlparse, unquote

        basename = unquote(urlparse(final_url).path.rsplit("/", 1)[-1])
        return basename.split("?")[0]

    @staticmethod
    def _is_document(data: bytes, content_type: str) -> bool:
        """Reject obvious non-files: HTML pages and images (e.g. favicons)."""
        ct = (content_type or "").lower()
        if ct.startswith("text/html") or ct.startswith("image/"):
            return False
        head = data[:32].lstrip()
        if head[:4] == b"\x00\x00\x01\x00":  # Windows .ico
            return False
        if head[:4] == b"\x89PNG" or head[:3] == b"GIF" or head[:2] == b"\xff\xd8":
            return False  # PNG / GIF / JPEG
        low = head[:14].lower()
        if low.startswith(b"<!doctype") or low.startswith(b"<html"):
            return False
        return True
