"""Invite email delivery.

Two backends selected by `EMAIL_BACKEND` config, so the calling code never
branches on environment:

- `ses` — Amazon SES via boto3, for production. Consistent with the rest
  of the stack's AWS-native choices.
- `smtp` — stdlib `smtplib`, pointed at a local Mailpit container for
  development, so invite emails are visible in Mailpit's web UI without
  ever actually leaving the developer's machine.

Requirement 1.3 (project-invites spec): an invite email states the project
name, the inviting admin, the assigned role, and contains the accept link.
"""

from __future__ import annotations

import logging
import smtplib
from email.message import EmailMessage

import boto3

from app.config import get_settings

log = logging.getLogger("app.email")


def _render(project_name: str, inviter_email: str, role: str, accept_url: str) -> tuple[str, str]:
    subject = f"You've been invited to {project_name} on Deal Workspace"
    body = (
        f"{inviter_email} has invited you to join the project \"{project_name}\" "
        f"on Deal Workspace, as a {role}.\n\n"
        f"Open this link to accept:\n{accept_url}\n"
    )
    return subject, body


def _send_via_ses(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    client = boto3.client("ses", region_name=settings.aws_region)
    client.send_email(
        Source=settings.email_from_address,
        Destination={"ToAddresses": [to]},
        Message={"Subject": {"Data": subject}, "Body": {"Text": {"Data": body}}},
    )


def _send_via_smtp(to: str, subject: str, body: str) -> None:
    settings = get_settings()
    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = settings.email_from_address
    message["To"] = to
    message.set_content(body)

    with smtplib.SMTP(settings.smtp_host, settings.smtp_port) as smtp:
        smtp.send_message(message)


def send_invite_email(
    *,
    to: str,
    project_name: str,
    inviter_email: str,
    role: str,
    accept_url: str,
) -> None:
    """Send one invite email. Backend selected by `settings.email_backend`."""
    subject, body = _render(project_name, inviter_email, role, accept_url)
    settings = get_settings()

    if settings.email_backend == "ses":
        _send_via_ses(to, subject, body)
    else:
        _send_via_smtp(to, subject, body)
    log.info("invite email sent to=%s project=%r backend=%s", to, project_name, settings.email_backend)


__all__ = ["send_invite_email"]
