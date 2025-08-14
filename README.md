# T-Mobile Bill Automation

This project automates the process of downloading your monthly T-Mobile bill, parsing it to calculate amounts owed by different individuals, and sending a summary via WhatsApp.

The entire process is contained in a single Python script (`app.py`) designed to be deployed as a serverless function (e.g., on Vercel, AWS Lambda, or Google Cloud Functions), allowing it to run on a schedule without manual intervention.

## Features

- **Automated Login:** Securely logs into your T-Mobile account using credentials from environment variables.
- **PDF Download:** Navigates the T-Mobile website to download the latest summary bill PDF.
- **Robust Parsing:** Uses `pdfplumber` and regular expressions to reliably extract billing information.
- **Custom Calculations:** Implements the specific cost-sharing logic from the original script.
- **Official WhatsApp API:** Sends a formatted bill summary to multiple recipients using the official Meta Cloud API for WhatsApp.

---

## Setup and Configuration

Follow these steps to set up and run the application.

### 1. Prerequisites

- Python 3.8+
- A T-Mobile account with online access.
- A Meta Developer Account to use the WhatsApp Cloud API.

### 2. Installation

Clone the repository and install the required Python libraries:

```bash
pip install -r requirements.txt
```

You will also need to install the Playwright browser binaries:

```bash
playwright install
```

### 3. Environment Variables

This application requires you to set several environment variables for security. Create a `.env` file in the root of your project or set these variables in your deployment environment.

**T-Mobile Credentials:**
- `TMO_USER`: Your T-Mobile username (email or phone number).
- `TMO_PASS`: Your T-Mobile password.

**Meta WhatsApp API Credentials:**
Follow the official [Meta Get Started Guide](https://developers.facebook.com/docs/whatsapp/cloud-api/get-started) to set up your app and get your credentials.
- `META_API_TOKEN`: Your temporary or permanent User Access Token from the Meta App Dashboard.
- `SENDER_PHONE_NUMBER_ID`: The Phone Number ID associated with the number you are sending messages from.

**Recipient Phone Numbers:**
- `RECIPIENT_PHONE_NUMBERS`: A comma-separated list of phone numbers to send the notification to. Numbers must be in E.164 format (e.g., `+15551234567`).

**Example `.env` file:**
```
TMO_USER="your-tmobile-email@example.com"
TMO_PASS="your_tmobile_password"

META_API_TOKEN="EAAJB..."
SENDER_PHONE_NUMBER_ID="106540352242922"

RECIPIENT_PHONE_NUMBERS="+15551234567,+15559876543"
```
*(Note: You will need a library like `python-dotenv` to automatically load a `.env` file if running locally.)*

---

## Running Locally

To test the script locally, ensure your environment variables are set and run:

```bash
python app.py
```

The script will execute the full workflow: download the bill to `bill.pdf`, parse it, and send the WhatsApp notifications. A screenshot named `error_screenshot.png` will be saved if the web scraping process fails.

---

## Deployment as a Serverless Function (Vercel Example)

This script is ideal for automated monthly execution on a serverless platform. Here’s how to deploy it on Vercel:

1.  **Fork this repository** to your own GitHub account.
2.  **Create a new Vercel project** and connect it to your forked repository.
3.  **Configure the Vercel Project:**
    - **Framework Preset:** Select "Other".
    - **Build & Development Settings:**
        - **Build Command:** `pip install -r requirements.txt && playwright install --with-deps`
        - **Output Directory:** Leave as default.
        - **Install Command:** Leave as default.
4.  **Add Environment Variables:** In your Vercel project settings (under "Environment Variables"), add all the variables listed in step 3 above.
5.  **Create a `vercel.json` file** in your repository with the following content. This tells Vercel how to handle the Python serverless function and sets up a cron job.

    ```json
    {
      "functions": {
        "app.py": {
          "maxDuration": 60
        }
      },
      "crons": [
        {
          "path": "/api/app",
          "schedule": "0 10 5 * *"
        }
      ]
    }
    ```
    - The `"path"` should point to your Python script. Vercel automatically maps `app.py` to `/api/app`.
    - The `"schedule"` is a cron expression. `"0 10 5 * *"` means the script will run at 10:00 AM on the 5th day of every month. Adjust this to your needs.

6.  **Deploy:** Commit and push the `vercel.json` file to your repository. Vercel will automatically build and deploy the function. You can then monitor executions from the Vercel dashboard.
