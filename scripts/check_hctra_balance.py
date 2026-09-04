#!/usr/bin/env python3
"""
Logs into the HCTRA (Harris County Toll Road Authority) account, reads the
"Available Balance" figure from the account overview page, and emails it
daily. Designed to run unattended (e.g. from a GitHub Actions cron job).

HCTRA's login page is an Angular SPA protected by Google reCAPTCHA
Enterprise. reCAPTCHA risk-scores traffic partly by IP reputation, and
GitHub-hosted runners use shared datacenter IPs, so this script may start
failing if HCTRA's reCAPTCHA begins flagging automated runs -- that is not
a bug in this script, it's a risk inherent to running browser automation
from CI infrastructure. On any failure this script captures a screenshot
and emails an error report (and exits non-zero, which also surfaces as a
failed GitHub Actions run) instead of failing silently.

Required environment variables:
  HCTRA_USERNAME       Login username for hctra.org
  HCTRA_PASSWORD       Login password for hctra.org
  SMTP_HOST            SMTP server host (e.g. smtp.zoho.com)
  SMTP_PORT            SMTP server port (e.g. 587)
  SMTP_USERNAME        SMTP auth username (usually your full Zoho email)
  SMTP_PASSWORD        SMTP auth password (Zoho app-specific password)
  EMAIL_FROM           From address for the notification email
  EMAIL_TO             Recipient address (e.g. user@example.com)
"""
import os
import re
import smtplib
import sys
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

from playwright.sync_api import sync_playwright

LOGIN_URL = "https://www.hctra.org/Login"
ACCOUNT_URL = "https://www.hctra.org/AccountOverview"
BALANCE_LABEL_RE = re.compile(r"available\s*balance", re.I)
MONEY_RE = re.compile(r"-?\$?\s?[\d,]+\.\d{2}")
SCREENSHOT_PATH = "/tmp/hctra_failure.png"
NAV_TIMEOUT_MS = 45_000


def find_username_field(page):
    password_input = page.locator('input[type="password"]').first
    password_input.wait_for(state="visible", timeout=NAV_TIMEOUT_MS)

    container = password_input.locator("xpath=ancestor::form[1]")
    if container.count() == 0:
        container = password_input.locator(
            "xpath=ancestor::*[self::div or self::section][1]"
        )

    candidates = container.locator(
        'input[type="text"], input[type="email"], input:not([type])'
    )
    if candidates.count() == 0:
        # Widen the search if the container guess was too narrow.
        candidates = page.locator('input[type="text"], input[type="email"]')

    return candidates.first, password_input, container


def find_submit_button(page, container):
    button = container.get_by_role("button", name=re.compile(r"log\s?in|sign\s?in", re.I))
    if button.count() == 0:
        button = container.locator('button[type="submit"], input[type="submit"]')
    if button.count() == 0:
        button = page.get_by_role("button", name=re.compile(r"log\s?in|sign\s?in", re.I))
    return button.first


def extract_balance(page):
    body_text = page.locator("body").inner_text()

    match = re.search(
        r"available\s*balance[^\d\-\$]{0,40}(-?\$?\s?[\d,]+\.\d{2})",
        body_text,
        re.I,
    )
    if match:
        return match.group(1)

    label = page.get_by_text(BALANCE_LABEL_RE).first
    if label.count() > 0:
        nearby = label.locator(
            "xpath=ancestor::*[self::div or self::li or self::section][1]"
        )
        nearby_text = nearby.inner_text()
        money_match = MONEY_RE.search(nearby_text)
        if money_match:
            return money_match.group(0)

    return None


def run():
    username = os.environ["HCTRA_USERNAME"]
    password = os.environ["HCTRA_PASSWORD"]

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        page.set_default_timeout(NAV_TIMEOUT_MS)

        try:
            page.goto(LOGIN_URL, wait_until="networkidle")

            username_field, password_field, container = find_username_field(page)
            username_field.fill(username)
            password_field.fill(password)

            submit = find_submit_button(page, container)
            submit.click()

            # Wait for navigation away from the login page. A stuck
            # reCAPTCHA challenge or bad credentials will time out here.
            page.wait_for_url(lambda url: "/Login" not in url, timeout=NAV_TIMEOUT_MS)

            page.goto(ACCOUNT_URL, wait_until="networkidle")
            page.wait_for_selector(f"text=/{BALANCE_LABEL_RE.pattern}/i", timeout=NAV_TIMEOUT_MS)

            balance = extract_balance(page)
            if balance is None:
                raise RuntimeError(
                    "Logged in and reached the account overview page, but "
                    "could not find a dollar figure near 'Available Balance'. "
                    "The page layout may have changed."
                )

            browser.close()
            return balance, None

        except Exception as exc:
            try:
                page.screenshot(path=SCREENSHOT_PATH, full_page=True)
            except Exception:
                pass
            browser.close()
            return None, str(exc)


def send_email(subject, body, attach_screenshot=False):
    smtp_host = os.environ["SMTP_HOST"]
    smtp_port_raw = os.environ.get("SMTP_PORT", "").strip()
    if not smtp_port_raw:
        raise RuntimeError(
            "SMTP_PORT environment variable is empty or not set (check the "
            "GitHub secret has a value, e.g. 587)"
        )
    smtp_port = int(smtp_port_raw)
    smtp_username = os.environ["SMTP_USERNAME"]
    smtp_password = os.environ["SMTP_PASSWORD"]
    email_from = os.environ["EMAIL_FROM"]
    email_to = os.environ["EMAIL_TO"]

    msg = MIMEMultipart()
    msg["Subject"] = subject
    msg["From"] = email_from
    msg["To"] = email_to
    msg.attach(MIMEText(body, "plain"))

    if attach_screenshot and os.path.exists(SCREENSHOT_PATH):
        with open(SCREENSHOT_PATH, "rb") as f:
            image = MIMEImage(f.read())
            image.add_header(
                "Content-Disposition", "attachment", filename="failure.png"
            )
            msg.attach(image)

    with smtplib.SMTP(smtp_host, smtp_port) as server:
        server.starttls()
        server.login(smtp_username, smtp_password)
        server.sendmail(email_from, [email_to], msg.as_string())


def main():
    balance, error = run()

    if error is not None:
        # Print the real failure before attempting to email it, so it's
        # visible in the Actions log even if send_email() itself fails
        # (e.g. a missing/misconfigured SMTP secret).
        print(f"FAILED: {error}", file=sys.stderr)
        try:
            send_email(
                subject="HCTRA balance check FAILED",
                body=(
                    "The daily HCTRA balance check failed with the following "
                    f"error:\n\n{error}\n\n"
                    "A screenshot at the point of failure is attached if one "
                    "could be captured. This is often caused by reCAPTCHA "
                    "flagging the automated run, a changed page layout, or "
                    "expired/invalid credentials."
                ),
                attach_screenshot=True,
            )
        except Exception as email_exc:
            print(
                f"Additionally failed to send the failure notification email: {email_exc}",
                file=sys.stderr,
            )
        sys.exit(1)

    try:
        send_email(
            subject=f"HCTRA Available Balance: {balance}",
            body=f"Your HCTRA Available Balance today is: {balance}",
        )
    except Exception as email_exc:
        print(
            f"Balance check succeeded (balance={balance}) but failed to email it: {email_exc}",
            file=sys.stderr,
        )
        sys.exit(1)

    print(f"OK: {balance}")


if __name__ == "__main__":
    main()
