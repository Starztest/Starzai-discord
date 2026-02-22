"""
File processing utilities for analyzing uploaded documents.
"""

from __future__ import annotations

import io
import logging
from typing import Optional

import discord

from config.constants import MAX_FILE_CONTENT_CHARS

logger = logging.getLogger(__name__)


class FileHandler:
    """Process and extract text from Discord file attachments."""

    SUPPORTED_EXTENSIONS = {".txt", ".md", ".py", ".js", ".ts", ".json", ".csv", ".xml", ".html", ".css", ".yaml", ".yml", ".toml", ".cfg", ".ini", ".log", ".sh", ".bat"}
    BINARY_EXTENSIONS = {".pdf", ".docx"}

    @classmethod
    async def extract_text(
        cls,
        attachment: discord.Attachment,
        max_size_mb: int = 10,
    ) -> tuple[str, Optional[str]]:
        """
        Extract text from a Discord attachment.

        Returns
        -------
        (content, error)
            Content string and an optional error message.
        """
        # Size check
        size_mb = attachment.size / (1024 * 1024)
        if size_mb > max_size_mb:
            return "", f"File too large ({size_mb:.1f}MB > {max_size_mb}MB limit)"

        ext = cls._get_extension(attachment.filename)

        # Binary files (PDF, DOCX) - provide a message
        if ext in cls.BINARY_EXTENSIONS:
            return "", f"Binary file format `{ext}` detected. Text extraction for this format requires additional libraries. Please upload a text-based file."

        # Text-based files
        if ext in cls.SUPPORTED_EXTENSIONS or ext == "":
            try:
                data = await attachment.read()
                text = data.decode("utf-8", errors="replace")
                if len(text) > MAX_FILE_CONTENT_CHARS:
                    text = text[:MAX_FILE_CONTENT_CHARS] + f"\n\n... [Truncated: {len(data):,} chars total]"
                return text, None
            except Exception as exc:
                logger.error("Failed to read file %s: %s", attachment.filename, exc)
                return "", f"Failed to read file: {exc}"

        return "", f"Unsupported file type: `{ext}`. Supported: {', '.join(sorted(cls.SUPPORTED_EXTENSIONS))}"

    @staticmethod
    def _get_extension(filename: str) -> str:
        if "." in filename:
            return "." + filename.rsplit(".", 1)[-1].lower()
        return ""

    @staticmethod
    def make_text_file(content: str, filename: str = "export.txt") -> discord.File:
        """Create a Discord File from a text string."""
        buffer = io.BytesIO(content.encode("utf-8"))
        return discord.File(buffer, filename=filename)

