"""Send a Markdown report as a text + HTML e-mail over SMTP."""

from __future__ import annotations

import os
import smtplib
import ssl
from dataclasses import dataclass
from email.message import EmailMessage

CSS = """
body { font-family: -apple-system, Segoe UI, Roboto, sans-serif; line-height: 1.45; color: #222; max-width: 720px; }
h1 { font-size: 1.4em; } h2 { font-size: 1.15em; margin-top: 1.2em; border-bottom: 1px solid #ddd; }
table { border-collapse: collapse; } td, th { border: 1px solid #ccc; padding: 3px 8px; }
th { background: #f3f3f3; } small, .foot { color: #777; font-size: 0.85em; }
"""


@dataclass(frozen=True)
class MailConfig:
    host: str
    port: int
    user: str | None
    password: str | None
    sender: str
    recipients: list[str]


def mail_config_from_env() -> MailConfig | None:
    host = os.getenv("SMTP_HOST")
    to = [a.strip() for a in (os.getenv("MAIL_TO") or "").split(",") if a.strip()]
    if not host or not to:
        return None
    user = os.getenv("SMTP_USER") or None
    return MailConfig(
        host=host,
        port=int(os.getenv("SMTP_PORT") or 587),
        user=user,
        password=os.getenv("SMTP_PASSWORD") or None,
        sender=os.getenv("MAIL_FROM") or user or to[0],
        recipients=to,
    )


def render_html(markdown_text: str) -> str:
    import markdown

    body = markdown.markdown(markdown_text, extensions=["tables", "sane_lists"])
    return f"<html><head><meta charset='utf-8'><style>{CSS}</style></head><body>{body}</body></html>"


def build_message(cfg: MailConfig, subject: str, markdown_text: str) -> EmailMessage:
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = cfg.sender
    msg["To"] = ", ".join(cfg.recipients)
    msg.set_content(markdown_text)
    msg.add_alternative(render_html(markdown_text), subtype="html")
    return msg


def send(cfg: MailConfig, msg: EmailMessage) -> None:
    ctx = ssl.create_default_context()
    if cfg.port == 465:
        server: smtplib.SMTP = smtplib.SMTP_SSL(cfg.host, cfg.port, context=ctx, timeout=60)
    else:
        server = smtplib.SMTP(cfg.host, cfg.port, timeout=60)
        server.starttls(context=ctx)
    with server:
        if cfg.user and cfg.password:
            server.login(cfg.user, cfg.password)
        server.send_message(msg)
