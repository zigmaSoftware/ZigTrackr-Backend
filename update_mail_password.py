#!/usr/bin/env python3
"""
Update the Gmail App Password used by ZigTrackr mail intake + outbound mail.

It verifies the new credential against Gmail BEFORE writing anything, so a bad
paste can never leave the app in a broken state.

Usage:
    python3 update_mail_password.py                 # prompts (hidden input)
    python3 update_mail_password.py --check-only    # test current .env only
"""
from __future__ import annotations

import argparse
import getpass
import imaplib
import re
import shutil
import smtplib
import sys
from datetime import datetime
from pathlib import Path

ENV = Path(__file__).resolve().parent / ".env"
# Both keys hold the same Google App Password in this project.
KEYS = ("MAIL_INTAKE_APP_PASSWORD", "EMAIL_HOST_PASSWORD")


def read_env() -> dict[str, str]:
    data: dict[str, str] = {}
    for line in ENV.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            key, value = line.split("=", 1)
            data[key.strip()] = value.strip()
    return data


def normalise(raw: str) -> str:
    """Google shows app passwords as 'abcd efgh ijkl mnop'; strip spaces/quotes."""
    return raw.strip().strip('"').strip("'").replace(" ", "")


def check_imap(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    try:
        conn = imaplib.IMAP4_SSL(host, port, timeout=25)
    except Exception as exc:  # network/TLS problem, not a credential problem
        return False, f"could not reach {host}:{port} — {type(exc).__name__}: {exc}"
    try:
        conn.login(user, password)
        conn.select("INBOX")
        conn.logout()
        return True, "IMAP login OK"
    except imaplib.IMAP4.error as exc:
        return False, f"IMAP login rejected — {exc}"


def check_smtp(host: str, port: int, user: str, password: str) -> tuple[bool, str]:
    try:
        server = smtplib.SMTP(host, port, timeout=25)
        server.starttls()
    except Exception as exc:
        return False, f"could not reach {host}:{port} — {type(exc).__name__}: {exc}"
    try:
        server.login(user, password)
        server.quit()
        return True, "SMTP login OK"
    except smtplib.SMTPAuthenticationError as exc:
        return False, f"SMTP login rejected — {exc}"


def write_password(password: str) -> Path:
    backup = ENV.with_suffix(f".env.bak-{datetime.now():%Y%m%d-%H%M%S}")
    shutil.copy2(ENV, backup)
    backup.chmod(0o600)

    text = ENV.read_text()
    for key in KEYS:
        pattern = rf"(?m)^{re.escape(key)}=.*$"
        replacement = f"{key}={password}"
        text, count = re.subn(pattern, replacement, text)
        if count == 0:
            text = text.rstrip("\n") + f"\n{replacement}\n"
    ENV.write_text(text)
    ENV.chmod(0o600)
    return backup


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--check-only", action="store_true", help="test the current password, change nothing")
    args = parser.parse_args()

    env = read_env()
    user = env.get("MAIL_INTAKE_USERNAME") or env.get("MAIL_INTAKE_EMAIL", "")
    imap_host = env.get("MAIL_INTAKE_IMAP_HOST", "imap.gmail.com")
    imap_port = int(env.get("MAIL_INTAKE_IMAP_PORT", 993))
    smtp_host = env.get("EMAIL_HOST", "smtp.gmail.com")
    smtp_port = int(env.get("EMAIL_PORT", 587))

    print(f"Mailbox : {user}")
    print(f"IMAP    : {imap_host}:{imap_port}")
    print(f"SMTP    : {smtp_host}:{smtp_port}")
    print(f".env    : {ENV}\n")

    if args.check_only:
        current = env.get("MAIL_INTAKE_APP_PASSWORD", "")
        if not current:
            print("No MAIL_INTAKE_APP_PASSWORD set.")
            return 1
        ok, msg = check_imap(imap_host, imap_port, user, current)
        print(("PASS  " if ok else "FAIL  ") + msg)
        return 0 if ok else 1

    print("Paste the 16-character Google App Password (spaces are fine, input hidden).")
    print("Generate one at: https://myaccount.google.com/apppasswords\n")
    raw = getpass.getpass("New app password: ")
    password = normalise(raw)

    if len(password) != 16:
        print(f"\nRefusing: expected 16 characters after removing spaces, got {len(password)}.")
        return 1
    if not password.isalpha():
        print("\nRefusing: Google app passwords are 16 letters; this contains other characters.")
        return 1

    print("\nVerifying against Gmail before writing...")
    imap_ok, imap_msg = check_imap(imap_host, imap_port, user, password)
    print(("  PASS  " if imap_ok else "  FAIL  ") + imap_msg)
    if not imap_ok:
        print("\nNothing was written — .env is unchanged.")
        return 1

    smtp_ok, smtp_msg = check_smtp(smtp_host, smtp_port, user, password)
    print(("  PASS  " if smtp_ok else "  WARN  ") + smtp_msg)

    backup = write_password(password)
    print(f"\nUpdated {ENV}")
    print(f"Backup  {backup}")
    print("\nNow restart Celery so workers pick up the new value:")
    print("  pkill -f 'celery -A config.celery.app'")
    print("  ./start-backend-stack.sh     # or however you normally start it")
    return 0


if __name__ == "__main__":
    sys.exit(main())
