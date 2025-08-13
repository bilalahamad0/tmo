import asyncio
import os
import sys
from playwright.async_api import async_playwright

# This is the main application file for the T-Mobile bill automation.
# It will contain the logic for web scraping, PDF parsing, and sending notifications.

async def download_bill() -> str:
    """
    Logs in to T-Mobile and downloads the latest PDF bill.

    Returns:
        The file path to the downloaded PDF bill.
    """
    tmo_user = os.getenv("TMO_USER")
    tmo_pass = os.getenv("TMO_PASS")

    if not tmo_user or not tmo_pass:
        print("Error: T-Mobile credentials (TMO_USER, TMO_PASS) are not set as environment variables.")
        sys.exit(1)

    print("Launching browser to download T-Mobile bill...")
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        page = await browser.new_page()

        try:
            # Log in
            login_url = "https://account.t-mobile.com/signin/v2/"
            print(f"Navigating to {login_url}")
            await page.goto(login_url, wait_until="networkidle")

            # Wait for the username field and fill it.
            # T-Mobile uses different IDs, so we try multiple common selectors.
            username_selector = 'input[name="username"], input[id="username"], input#username'
            await page.wait_for_selector(username_selector, timeout=15000)
            await page.fill(username_selector, tmo_user)
            await page.click('button#kc-login, button#login-button, button:has-text("Next")') # Click next after username

            # Wait for password field and fill it
            password_selector = 'input[type="password"], input[name="password"], input#password'
            await page.wait_for_selector(password_selector, timeout=10000)
            await page.fill(password_selector, tmo_pass)
            await page.click('button#kc-login, button#submitButton') # Click submit

            print("Login submitted. Waiting for dashboard...")
            await page.wait_for_url("https://www.t-mobile.com/account/dashboard", timeout=30000)
            print("Successfully logged in.")

            # Navigate to bill summary page
            bill_summary_url = "https://www.t-mobile.com/bill/summary"
            print(f"Navigating to {bill_summary_url}")
            await page.goto(bill_summary_url, wait_until="domcontentloaded")

            # Handle PDF download
            async with page.expect_download() as download_info:
                print("Looking for download button...")
                # The download link might be in a popup.
                # First, click the button to open the download options.
                await page.locator('button:has-text("Download my bill (PDF)"), a:has-text("Download bill")').first.click()

                # Then, click the final download button in the dialog/popup.
                print("Clicking final download link...")
                await page.locator('a:has-text("Download summary bill"), a:has-text("Download")').last.click()

            download = await download_info.value
            download_path = "bill.pdf"
            await download.save_as(download_path)
            print(f"Bill downloaded successfully to {download_path}")

            return download_path

        except Exception as e:
            print(f"An error occurred during web scraping: {e}")
            await page.screenshot(path="error_screenshot.png")
            print("A screenshot 'error_screenshot.png' has been saved for debugging.")
            return None
        finally:
            await browser.close()


import re
import pdfplumber

def parse_bill(pdf_path: str) -> dict:
    """
    Parses the T-Mobile PDF bill to extract billing details.

    Args:
        pdf_path: The file path to the PDF bill.

    Returns:
        A dictionary containing the formatted bill summary.
    """
    print(f"Parsing bill: {pdf_path}")

    name_mapping = {
        '(847)443-5295': 'Bilal',
        '(703)479-8351': 'Bilal2',
        '(650)797-3800': 'Karan',
        '(408)677-1812': 'Mudit',
        '(408)784-6924': 'Utsav',
        '(408)896-8130': 'Sachin',
        '(408)898-8413': 'Sambit'
    }

    try:
        with pdfplumber.open(pdf_path) as pdf:
            # The original script used page 1, which is the second page
            page = pdf.pages[1]
            text = page.extract_text()

            lines = text.split('\n')

            # Find total bill and month using regex for robustness
            month_year, total_bill_amount = "", "0.00"
            for line in lines:
                if "Bill date" in line:
                    match = re.search(r"(\w+\s+\d{1,2},\s+\d{4})", line)
                    if match:
                        month_year = match.group(1)
                if "Total" in line or "Totals" in line:
                    match = re.search(r"\$\d+\.\d{2}", line)
                    if match:
                        total_bill_amount = match.group(0)

            # Find base charge
            base_charge = 0.0
            for line in lines:
                if 'Account charges & credits' in line:
                    match = re.search(r"\$(\d+\.\d{2})", line)
                    if match:
                        base_charge = float(match.group(1))
                        break

            # Find individual line charges
            final_dict = {}
            for line in lines:
                match = re.search(r"(\(\d{3}\)\d{3}-\d{4})\s+.*\$(\d+\.\d{2})", line)
                if match and match.group(1) in name_mapping:
                    phone_number = match.group(1)
                    bill = float(match.group(2))
                    final_dict[phone_number] = bill

            if not final_dict:
                print("Could not parse individual line details. The bill format may have changed.")
                return None

            members = len(final_dict.keys())
            member_base_charge = (base_charge + 10.0) / members

            # Calculate final amounts
            summary = {}
            total_check = 0
            send_to_sachin = 0
            ba_im = 0 # Bilal + Bilal2 amount

            for phone, bill in final_dict.items():
                name = name_mapping[phone]
                if name == 'Bilal2':
                    bill -= 10.0  # Special adjustment from original script

                final_bill = bill + member_base_charge
                total_check += final_bill
                summary[name] = f"${final_bill:.2f}"

                if name in ['Bilal', 'Karan', 'Bilal2']:
                    send_to_sachin += final_bill
                if name in ['Bilal', 'Bilal2']:
                    ba_im += final_bill

            # Prepare final output strings
            output = {
                "header": f"{month_year}: \t{total_bill_amount}",
                "individual_bills": [f"{name} ({phone}): \t{final_dict[phone]:.2f}" for phone, name in name_mapping.items() if phone in final_dict],
                "final_summary": [],
                "total_bill": f"Total Bill: \t${total_check:.2f}",
                "special_summary": []
            }

            final_bills_text = []
            for name, bill_str in summary.items():
                phone = [p for p, n in name_mapping.items() if n == name][0]
                final_bills_text.append(f"{name} {phone}: \t{bill_str}")
            output["final_summary"] = final_bills_text
            output["special_summary"].append(f"for Bilal+Bilal2: \t${ba_im:.2f}")
            output["special_summary"].append(f"Send to Sachin: \t${send_to_sachin:.2f}")

            print("Bill parsed successfully.")
            return output

    except Exception as e:
        print(f"An error occurred during PDF parsing: {e}")
        return None


from twilio.rest import Client

def send_whatsapp_notification(bill_data: dict):
    """
    Sends the bill summary to a list of recipients via WhatsApp using Twilio.

    Args:
        bill_data: A dictionary containing the formatted bill summary.
    """
    account_sid = os.getenv("TWILIO_ACCOUNT_SID")
    auth_token = os.getenv("TWILIO_AUTH_TOKEN")
    twilio_phone = os.getenv("TWILIO_PHONE_NUMBER")
    recipient_phones_str = os.getenv("RECIPIENT_PHONE_NUMBERS")

    if not all([account_sid, auth_token, twilio_phone, recipient_phones_str]):
        print("Error: Twilio credentials or recipient phone numbers are not fully set.")
        print("Please set TWILIO_ACCOUNT_SID, TWILIO_AUTH_TOKEN, TWILIO_PHONE_NUMBER, and RECIPIENT_PHONE_NUMBERS.")
        return

    try:
        client = Client(account_sid, auth_token)
        recipients = [phone.strip() for phone in recipient_phones_str.split(',')]

        # Construct the message body
        separator = "\n" + "#"*21 + "\n"
        message_body = separator.join([
            bill_data["header"],
            "\n".join(bill_data["final_summary"]),
            bill_data["total_bill"],
            "\n".join(bill_data["special_summary"])
        ])

        print(f"Constructed Message Body:\n{message_body}")

        for recipient_phone in recipients:
            if not re.match(r"\+\d+", recipient_phone):
                print(f"Invalid phone number format: {recipient_phone}. It must be in E.164 format (e.g., +14155238886).")
                continue

            print(f"Sending WhatsApp message to {recipient_phone}...")
            message = client.messages.create(
                from_=f'whatsapp:{twilio_phone}',
                body=message_body,
                to=f'whatsapp:{recipient_phone}'
            )
            print(f"Message sent to {recipient_phone} with SID: {message.sid}")

    except Exception as e:
        print(f"An error occurred while sending WhatsApp notification: {e}")


async def main():
    """
    Main function to orchestrate the bill processing workflow.
    """
    print("T-Mobile bill automation script starting...")
    # 1. Download the bill
    bill_path = await download_bill()
    if not bill_path:
        print("Failed to download bill. Exiting.")
        return

    # 2. Parse the bill
    bill_data = parse_bill(bill_path)
    if not bill_data:
        print("Failed to parse bill. Exiting.")
        return

    # 3. Send notifications
    send_whatsapp_notification(bill_data)

    print("Script finished.")

if __name__ == "__main__":
    asyncio.run(main())
