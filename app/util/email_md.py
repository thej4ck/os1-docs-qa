"""Markdown → email/HTML-safe rendering.

Shared by the chat-conversation email ([routes/chat_routes.py]) and the public
shared-answer landing ([routes/public_routes.py]). Converts the answer markdown
(including `[Screenshot: desc | url]` markers) to HTML with inline styles and
absolute image URLs, so it renders in email clients and standalone pages alike.
"""

import re

import markdown as md


def escape_html(text: str) -> str:
    """HTML-escape text."""
    return (
        text.replace("&", "&amp;")
        .replace("<", "&lt;")
        .replace(">", "&gt;")
        .replace('"', "&quot;")
    )


def md_to_html(text: str, base_url: str) -> str:
    """Convert markdown content to email-safe HTML with absolute image URLs."""
    # Convert [Screenshot: desc | url] markers to markdown images
    text = re.sub(
        r'\[Screenshot:\s*(.+?)\s*\|\s*(.+?)\s*\]',
        r'![\1](\2)',
        text,
    )
    # Make relative image URLs absolute
    text = re.sub(
        r'!\[([^\]]*)\]\((/[^)]+)\)',
        lambda m: f'![{m.group(1)}]({base_url}{m.group(2)})',
        text,
    )
    html = md.markdown(text, extensions=["tables", "fenced_code", "nl2br"])
    # Style images inline for email
    html = html.replace(
        "<img ",
        '<img style="max-width:100%;height:auto;border-radius:8px;border:1px solid #E5E7EB;margin:8px 0;display:block;" ',
    )
    # Style tables inline
    html = html.replace("<table>", '<table style="border-collapse:collapse;width:100%;margin:8px 0;font-size:14px;">')
    html = html.replace("<th>", '<th style="border:1px solid #E5E7EB;padding:6px 10px;background:#F0F2F5;font-weight:600;text-align:left;">')
    html = html.replace("<td>", '<td style="border:1px solid #E5E7EB;padding:6px 10px;text-align:left;">')
    # Style code blocks
    html = html.replace("<pre>", '<pre style="background:#1E293B;color:#E2E8F0;padding:12px 16px;border-radius:6px;overflow-x:auto;margin:8px 0;font-size:13px;">')
    html = html.replace("<code>", '<code style="font-family:monospace;font-size:0.9em;">')
    return html
