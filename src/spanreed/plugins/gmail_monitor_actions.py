import abc
import re
import logging
from typing import Any, Dict, List, Optional
from dataclasses import dataclass
import aiohttp

from spanreed.user import User
from spanreed.apis.gmail import EmailMessage
from spanreed.apis.telegram_bot import TelegramBotApi


@dataclass
class EmailMatch:
    email: EmailMessage
    rule_name: str


class EmailActionHandler(abc.ABC):
    @abc.abstractmethod
    async def execute(self, email_match: EmailMatch, config: Dict[str, Any], user: User) -> None:
        pass


class TelegramNotificationAction(EmailActionHandler):
    async def execute(self, email_match: EmailMatch, config: Dict[str, Any], user: User) -> None:
        bot = await TelegramBotApi.for_user(user)

        email = email_match.email
        rule_name = email_match.rule_name

        # Get configuration with defaults
        include_body = config.get('include_body', False)
        include_snippet = config.get('include_snippet', True)
        custom_message = config.get('custom_message', '')

        # Build notification message
        message_parts = []

        if custom_message:
            message_parts.append(custom_message)
        else:
            message_parts.append(f"📧 Email matched rule: **{rule_name}**")

        message_parts.append(f"**From:** {email.sender}")
        message_parts.append(f"**Subject:** {email.subject}")
        message_parts.append(f"**Date:** {email.date.strftime('%Y-%m-%d %H:%M:%S')}")

        if email.has_attachments:
            message_parts.append("📎 Has attachments")

        if include_snippet and email.snippet:
            message_parts.append(f"**Preview:** {email.snippet}")

        if include_body and email.body:
            # Limit body length to avoid telegram message limits
            body_preview = email.body[:500]
            if len(email.body) > 500:
                body_preview += "..."
            message_parts.append(f"**Body:** {body_preview}")

        message = "\n\n".join(message_parts)

        await bot.send_message(message)


class DownloadLinkAction(EmailActionHandler):
    def __init__(self):
        self._logger = logging.getLogger(__name__)

    async def execute(self, email_match: EmailMatch, config: Dict[str, Any], user: User) -> None:
        bot = await TelegramBotApi.for_user(user)
        email = email_match.email
        rule_name = email_match.rule_name

        # Get configuration
        url_regex = config.get('url_regex', r'https?://[^\s<>"\']+')
        text_regex = config.get('text_regex', None)
        custom_filename = config.get('custom_filename', None)
        max_file_size_mb = config.get('max_file_size_mb', 10)

        try:
            # Extract links from email body
            links = await self._extract_links_from_email(email, url_regex, text_regex)

            if not links:
                await bot.send_message(f"🔗 No matching links found in email from {email.sender}")
                return

            # Download and send each found link
            for i, link in enumerate(links[:3]):  # Limit to first 3 files
                try:
                    filename = await self._download_and_send_file(
                        bot, link, email, rule_name, custom_filename, max_file_size_mb, i
                    )
                    if filename:
                        await bot.send_message(f"📄 Downloaded and sent: {filename}")
                except Exception as e:
                    self._logger.error(f"Failed to download file from {link}: {e}")
                    await bot.send_message(f"❌ Failed to download file: {str(e)}")

        except Exception as e:
            self._logger.error(f"Download link action failed: {e}")
            await bot.send_message(f"❌ Download failed: {str(e)}")

    async def _extract_links_from_email(self, email: EmailMessage, url_regex: str, text_regex: Optional[str]) -> List[str]:
        """Extract links from email body based on URL and text regex patterns"""
        links = []

        # First, find all URLs matching the URL regex
        url_pattern = re.compile(url_regex, re.IGNORECASE)
        url_matches = url_pattern.findall(email.body)

        if not text_regex:
            # If no text regex specified, return all URL matches
            return url_matches

        # If text regex specified, find links where the link text matches
        # Look for HTML links: <a href="url">text</a>
        html_link_pattern = re.compile(
            r'<a[^>]+href=["\'](https?://[^\s<>"\']+)["\'][^>]*>([^<]+)</a>',
            re.IGNORECASE
        )
        html_matches = html_link_pattern.findall(email.body)

        text_pattern = re.compile(text_regex, re.IGNORECASE)
        for url, link_text in html_matches:
            if url_pattern.match(url) and text_pattern.search(link_text):
                links.append(url)

        # Also look for plain text patterns where URL follows text
        # This is less reliable but covers cases where emails have "Click here: http://..."
        if not links:
            text_url_pattern = re.compile(
                rf'({text_regex}).*?(https?://[^\s<>"\']+)',
                re.IGNORECASE | re.DOTALL
            )
            text_url_matches = text_url_pattern.findall(email.body)
            for text_match, url in text_url_matches:
                if url_pattern.match(url):
                    links.append(url)

        return links

    async def _download_and_send_file(
        self,
        bot: TelegramBotApi,
        url: str,
        email: EmailMessage,
        rule_name: str,
        custom_filename: Optional[str],
        max_file_size_mb: int,
        index: int
    ) -> Optional[str]:
        """Download file from URL and send via Telegram"""

        # Generate filename
        if custom_filename:
            # Replace placeholders in custom filename
            filename = custom_filename.format(
                sender=email.sender.split('@')[0] if '@' in email.sender else email.sender,
                subject=re.sub(r'[^\w\-_.]', '_', email.subject)[:50],
                date=email.date.strftime('%Y%m%d'),
                rule=rule_name,
                index=index
            )
        else:
            # Extract filename from URL or use default
            url_filename = url.split('/')[-1].split('?')[0]
            if url_filename and '.' in url_filename:
                filename = url_filename
            else:
                filename = f"document_{email.date.strftime('%Y%m%d')}_{index}"

        # Download the file with redirect following
        async with aiohttp.ClientSession() as session:
            async with session.get(url, allow_redirects=True) as response:
                if response.status != 200:
                    raise Exception(f"HTTP {response.status}: {response.reason}")

                content_length = response.headers.get('content-length')
                if content_length and int(content_length) > max_file_size_mb * 1024 * 1024:
                    raise Exception(f"File too large: {int(content_length) / (1024*1024):.1f}MB > {max_file_size_mb}MB")

                # Download file content
                downloaded_size = 0
                max_size = max_file_size_mb * 1024 * 1024
                file_data = bytearray()

                async for chunk in response.content.iter_chunked(8192):
                    downloaded_size += len(chunk)
                    if downloaded_size > max_size:
                        raise Exception(f"File too large: {downloaded_size / (1024*1024):.1f}MB > {max_file_size_mb}MB")
                    file_data.extend(chunk)

        # Send via Telegram
        await bot.send_document(filename, bytes(file_data))

        # Send additional info as separate message
        await bot.send_message(f"📄 File from {email.sender}\n📧 {email.subject}\n🏷️ Rule: {rule_name}")

        return filename