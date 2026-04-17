#!/usr/bin/env python3
"""
Daily monitor for Apple's BatteryLife.mobileconfig profile.

Workflow:
1. Run once with --login to sign in to Apple in a real browser window.
2. Schedule the script to run daily.
3. Each run downloads the profile, computes MD5, compares it to the previous
   known MD5, stores state locally, and sends an email update.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import json
import os
import smtplib
import sys
from dataclasses import dataclass
from datetime import datetime, timezone
from email.message import EmailMessage
from pathlib import Path
from typing import Any

from playwright.sync_api import Error as PlaywrightError
from playwright.sync_api import TimeoutError as PlaywrightTimeoutError
from playwright.sync_api import sync_playwright


DOWNLOAD_URL = (
    "https://developer.apple.com/services-account/download"
    "?path=/iOS/iOS_Logs/BatteryLife.mobileconfig"
)
DEFAULT_APP_DIR = Path(__file__).resolve().parent / "monitor_data"
STATE_FILENAME = "state.json"
AUTH_STATE_FILENAME = "auth_state.json"
LATEST_FILENAME = "BatteryLife.mobileconfig"
DOWNLOADS_DIRNAME = "downloads"


class MonitorError(Exception):
    """Raised when the monitor cannot complete successfully."""


@dataclass
class Config:
    smtp_host: str
    smtp_port: int
    smtp_username: str
    smtp_password: str
    smtp_use_tls: bool
    email_from: str
    email_to: str
    apple_profile_dir: Path
    app_dir: Path
    headless: bool
    timeout_ms: int


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Download BatteryLife.mobileconfig, track MD5, and email status.",
    )
    parser.add_argument(
        "--login",
        action="store_true",
        help="Open a browser window so you can log in to Apple and save the session.",
    )
    parser.add_argument(
        "--headful",
        action="store_true",
        help="Force a visible browser window during the download check.",
    )
    parser.add_argument(
        "--app-dir",
        default=str(DEFAULT_APP_DIR),
        help="Directory used for browser session, downloads, and state.",
    )
    parser.add_argument(
        "--timeout-seconds",
        type=int,
        default=120,
        help="Timeout for Apple page interactions.",
    )
    parser.add_argument(
        "-test",
        "--test",
        action="store_true",
        help="Run in test mode so email subjects and bodies are clearly marked as tests.",
    )
    parser.add_argument(
        "-run",
        "--run",
        action="store_true",
        help="With test mode, run one real download check and send a test status email.",
    )
    parser.add_argument(
        "-loginexpire",
        "-loginexpie",
        "--loginexpire",
        action="store_true",
        help="With test mode, send a test Apple-login-expired email.",
    )
    return parser.parse_args()


def get_env(name: str, *, required: bool = True, default: str | None = None) -> str:
    value = os.getenv(name, default)
    if required and not value:
        raise MonitorError(f"Missing required environment variable: {name}")
    return value or ""


def getenv_bool(name: str, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    return raw.strip().lower() in {"1", "true", "yes", "on"}


def load_config(args: argparse.Namespace) -> Config:
    app_dir = Path(args.app_dir).expanduser().resolve()
    return Config(
        smtp_host=get_env("SMTP_HOST", required=False),
        smtp_port=int(get_env("SMTP_PORT", required=False, default="587")),
        smtp_username=get_env("SMTP_USERNAME", required=False),
        smtp_password=get_env("SMTP_PASSWORD", required=False),
        smtp_use_tls=getenv_bool("SMTP_USE_TLS", True),
        email_from=get_env("EMAIL_FROM", required=False),
        email_to=get_env("EMAIL_TO", required=False),
        apple_profile_dir=app_dir / "apple_profile",
        app_dir=app_dir,
        headless=not args.headful,
        timeout_ms=args.timeout_seconds * 1000,
    )


def ensure_dirs(config: Config) -> None:
    config.app_dir.mkdir(parents=True, exist_ok=True)
    config.apple_profile_dir.mkdir(parents=True, exist_ok=True)
    (config.app_dir / DOWNLOADS_DIRNAME).mkdir(parents=True, exist_ok=True)


def state_path(config: Config) -> Path:
    return config.app_dir / STATE_FILENAME


def latest_file_path(config: Config) -> Path:
    return config.app_dir / LATEST_FILENAME


def auth_state_path(config: Config) -> Path:
    return config.app_dir / AUTH_STATE_FILENAME


def ensure_auth_state_from_env(config: Config) -> None:
    encoded = os.getenv("APPLE_AUTH_STATE_BASE64", "").strip()
    if not encoded:
        return

    path = auth_state_path(config)
    try:
        decoded = base64.b64decode(encoded)
        json.loads(decoded.decode("utf-8"))
    except Exception as exc:
        raise MonitorError(
            "APPLE_AUTH_STATE_BASE64 is present but is not a valid base64-encoded JSON auth state."
        ) from exc

    path.write_bytes(decoded)


def load_state(config: Config) -> dict[str, Any]:
    path = state_path(config)
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except json.JSONDecodeError as exc:
        raise MonitorError(f"State file is not valid JSON: {path}") from exc


def save_state(config: Config, state: dict[str, Any]) -> None:
    path = state_path(config)
    path.write_text(json.dumps(state, indent=2, sort_keys=True), encoding="utf-8")


def md5_hex(data: bytes) -> str:
    return hashlib.md5(data).hexdigest()


def display_md5(value: str | None) -> str:
    return value if value is not None else "null"


def save_download_copy(config: Config, content: bytes, current_md5: str) -> Path:
    timestamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    archive_path = (
        config.app_dir / DOWNLOADS_DIRNAME / f"BatteryLife_{timestamp}_{current_md5}.mobileconfig"
    )
    archive_path.write_bytes(content)
    latest_file_path(config).write_bytes(content)
    return archive_path


def looks_like_sign_in_url(url: str) -> bool:
    lowered = url.lower()
    return any(
        marker in lowered
        for marker in (
            "signin",
            "login",
            "auth",
            "appleid.apple.com",
            "idmsa.apple.com",
        )
    )


def safe_navigate_to_download(page: Any) -> None:
    try:
        page.goto(DOWNLOAD_URL, wait_until="domcontentloaded")
    except PlaywrightError as exc:
        if "Download is starting" not in str(exc):
            raise


def open_context_from_auth_state(playwright: Any, config: Config, *, headless: bool) -> Any:
    state_file = auth_state_path(config)
    if not state_file.exists():
        raise MonitorError(
            "No saved Apple auth state found. Run the script with --login first."
        )
    browser = playwright.chromium.launch(headless=headless)
    context = browser.new_context(
        accept_downloads=True,
        storage_state=str(state_file),
    )
    return browser, context


def perform_login(config: Config) -> None:
    print("Opening browser for Apple login...")
    print("Please sign in fully and let the BatteryLife.mobileconfig download start once.")

    with sync_playwright() as playwright:
        browser_context = playwright.chromium.launch_persistent_context(
            user_data_dir=str(config.apple_profile_dir),
            headless=False,
            accept_downloads=True,
        )
        try:
            page = browser_context.new_page()
            try:
                with page.expect_download(timeout=config.timeout_ms) as download_info:
                    safe_navigate_to_download(page)
                download = download_info.value
                temp_path = download.path()
                if temp_path is None:
                    raise MonitorError("Apple login completed, but the download file could not be read.")
                browser_context.storage_state(path=str(auth_state_path(config)))
                print("Apple session saved successfully.")
            except PlaywrightTimeoutError as exc:
                current_url = page.url
                if looks_like_sign_in_url(current_url):
                    raise MonitorError(
                        "Apple login was not completed in time. Run with --login again, "
                        "finish signing in, and complete any 2FA prompts."
                    ) from exc
                raise MonitorError(
                    "Timed out waiting for the BatteryLife.mobileconfig download during login setup."
                ) from exc
        finally:
            browser_context.close()


def download_profile(config: Config) -> bytes:
    with sync_playwright() as playwright:
        browser = None
        browser_context = None
        try:
            browser, browser_context = open_context_from_auth_state(
                playwright,
                config,
                headless=config.headless,
            )
            page = browser_context.new_page()
            try:
                with page.expect_download(timeout=config.timeout_ms) as download_info:
                    safe_navigate_to_download(page)
                download = download_info.value
            except PlaywrightTimeoutError as exc:
                if looks_like_sign_in_url(page.url):
                    raise MonitorError(
                        "Saved Apple session is not authenticated. Run the script with --login first."
                    ) from exc
                raise

            temp_path_str = download.path()
            if temp_path_str is None:
                raise MonitorError("Downloaded file path was not available.")
            temp_path = Path(temp_path_str)
            content = temp_path.read_bytes()
            if not content:
                raise MonitorError("Downloaded file is empty.")
            return content
        except PlaywrightTimeoutError as exc:
            raise MonitorError(
                "Timed out while waiting for the BatteryLife.mobileconfig download."
            ) from exc
        finally:
            if browser_context is not None:
                browser_context.close()
            if browser is not None:
                browser.close()


def build_status_subject(changed: bool, first_run: bool) -> str:
    if first_run:
        return "New BatteryLife.mobileconfig detected"
    if changed:
        return "New BatteryLife.mobileconfig detected"
    return "No new BatteryLife.mobileconfig detected"


def test_label(is_test: bool) -> str:
    return "[TEST] " if is_test else ""


def build_status_body(
    *,
    current_md5: str | None,
    previous_md5: str | None,
    changed: bool,
    first_run: bool,
    archive_path: Path | None,
    is_test: bool = False,
) -> str:
    if first_run:
        headline = "New BatteryLife.mobileconfig detected"
    elif changed:
        headline = "New BatteryLife.mobileconfig detected"
    else:
        headline = "No new BatteryLife.mobileconfig detected"

    lines = [
        headline,
        "",
        f"Current MD5: {display_md5(current_md5)}",
        f"Previous MD5: {display_md5(previous_md5)}",
        f"Checked at (UTC): {datetime.now(timezone.utc).isoformat()}",
    ]

    if archive_path is not None:
        lines.append(f"Saved file: {archive_path}")

    if is_test:
        lines.extend(
            [
                "",
                "Test mode only: this email was triggered manually.",
            ]
        )

    return "\n".join(lines)


def build_auth_failure_subject(*, is_test: bool = False) -> str:
    return f"{test_label(is_test)}BatteryLife.mobileconfig Apple login expired"


def build_auth_failure_body(config: Config, reason: str, *, is_test: bool = False) -> str:
    lines = [
        "BatteryLife.mobileconfig check could not run because the saved Apple login is no longer valid.",
        "",
        f"Reason: {reason}",
        f"Checked at (UTC): {datetime.now(timezone.utc).isoformat()}",
        "",
        "Action required:",
        f"Run: python3 {Path(__file__).name} --login",
    ]

    state_file = auth_state_path(config)
    if state_file.exists():
        lines.append(f"Saved auth state file: {state_file}")

    if is_test:
        lines.extend(
            [
                "",
                "Test mode only: this email was triggered manually.",
            ]
        )

    return "\n".join(lines)


def send_email(config: Config, subject: str, body: str) -> None:
    required_env_vars = {
        "SMTP_HOST": config.smtp_host,
        "SMTP_USERNAME": config.smtp_username,
        "SMTP_PASSWORD": config.smtp_password,
        "EMAIL_FROM": config.email_from,
        "EMAIL_TO": config.email_to,
    }
    missing = [name for name, value in required_env_vars.items() if not value]
    if missing:
        raise MonitorError(
            "Missing required email settings: " + ", ".join(sorted(missing))
        )

    message = EmailMessage()
    message["Subject"] = subject
    message["From"] = config.email_from
    message["To"] = config.email_to
    message.set_content(body)

    with smtplib.SMTP(config.smtp_host, config.smtp_port, timeout=60) as smtp:
        if config.smtp_use_tls:
            smtp.starttls()
        smtp.login(config.smtp_username, config.smtp_password)
        smtp.send_message(message)


def notify_auth_failure(config: Config, reason: str, *, is_test: bool = False) -> None:
    subject = build_auth_failure_subject(is_test=is_test)
    body = build_auth_failure_body(config, reason, is_test=is_test)
    send_email(config, subject, body)


def send_test_run_email(config: Config) -> None:
    ensure_dirs(config)
    ensure_auth_state_from_env(config)
    previous_state = load_state(config)
    previous_md5 = previous_state.get("current_md5")

    content = download_profile(config)
    current_md5 = md5_hex(content)
    first_run = previous_md5 is None
    changed = previous_md5 != current_md5
    subject = f"{test_label(True)}{build_status_subject(changed=changed, first_run=first_run)}"
    body = build_status_body(
        current_md5=current_md5,
        previous_md5=previous_md5,
        changed=changed,
        first_run=first_run,
        archive_path=None,
        is_test=True,
    )
    send_email(config, subject, body)
    print(subject)
    print(body)


def run_test_mode(config: Config, args: argparse.Namespace) -> int:
    if args.run and args.loginexpire:
        raise MonitorError("Choose only one test action: -run or -loginexpire.")
    if not args.run and not args.loginexpire:
        raise MonitorError("Test mode requires one action: -run or -loginexpire.")

    if args.run:
        send_test_run_email(config)
        return 0

    notify_auth_failure(
        config,
        "Test mode requested a simulated Apple login expiration.",
        is_test=True,
    )
    print(build_auth_failure_subject(is_test=True))
    return 0


def run_monitor(config: Config) -> None:
    ensure_dirs(config)
    ensure_auth_state_from_env(config)
    previous_state = load_state(config)
    previous_md5 = previous_state.get("current_md5")

    try:
        content = download_profile(config)
    except MonitorError as exc:
        if "Run the script with --login first" in str(exc) or "No saved Apple auth state found" in str(exc):
            notify_auth_failure(config, str(exc))
        raise

    current_md5 = md5_hex(content)
    first_run = previous_md5 is None
    changed = previous_md5 != current_md5

    archive_path = save_download_copy(config, content, current_md5)
    new_state = {
        "current_md5": current_md5,
        "previous_md5": previous_md5,
        "last_checked_utc": datetime.now(timezone.utc).isoformat(),
        "latest_file": str(latest_file_path(config)),
        "last_archive_file": str(archive_path),
    }
    save_state(config, new_state)

    subject = f"{test_label(False)}{build_status_subject(changed=changed, first_run=first_run)}"
    body = build_status_body(
        current_md5=current_md5,
        previous_md5=previous_md5,
        changed=changed,
        first_run=first_run,
        archive_path=archive_path,
        is_test=False,
    )
    send_email(config, subject, body)

    print(subject)
    print(body)


def main() -> int:
    args = parse_args()
    try:
        config = load_config(args)
        ensure_dirs(config)

        if args.test:
            return run_test_mode(config, args)

        if args.login:
            perform_login(config)
            return 0

        run_monitor(config)
        return 0
    except MonitorError as exc:
        print(f"ERROR: {exc}", file=sys.stderr)
        return 1
    except Exception as exc:  # pragma: no cover - safety net for automation runs
        print(f"UNEXPECTED ERROR: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
