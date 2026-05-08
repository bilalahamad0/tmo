"""SMTP helpers for the three pipeline email types.

- send_bill_summary_email: monthly bill breakdown with HTML UI + PDF attached
- send_confirmation_email: post-Zelle confirmation with txn ID + screenshot
- send_failure_alert: any pipeline failure, with logs/screenshots attached
- send_2fa_alert: T-Mobile push-approval prompt (moved from download_bill.py)
"""
import os
import smtplib
from email.message import EmailMessage
from pathlib import Path


def _smtp_config():
    """Return (sender_email, sender_password, recipients_list) or (None, None, [])."""
    sender = os.getenv("SMTP_EMAIL")
    password = os.getenv("SMTP_PASSWORD")
    recipients_str = os.getenv("RECIPIENT_EMAILS", "")
    recipients = [e.strip() for e in recipients_str.split(",") if e.strip()]
    return sender, password, recipients


def _failure_alert_recipients():
    """Failure alerts go to FAILURE_ALERT_EMAIL if set, else first RECIPIENT_EMAILS."""
    explicit = os.getenv("FAILURE_ALERT_EMAIL", "").strip()
    if explicit:
        return [explicit]
    _, _, recipients = _smtp_config()
    return recipients[:1] if recipients else []


def _send(msg: EmailMessage, sender: str, password: str) -> bool:
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465) as server:
            server.login(sender, password)
            server.send_message(msg)
        return True
    except Exception as e:
        print(f"Failed to send email '{msg['Subject']}': {e}")
        return False


def _attach_files(msg: EmailMessage, paths) -> None:
    for path in paths or []:
        if not path:
            continue
        p = Path(path)
        if not p.exists():
            print(f"Skipping missing attachment: {path}")
            continue
        try:
            data = p.read_bytes()
            suffix = p.suffix.lower()
            if suffix == ".pdf":
                msg.add_attachment(
                    data, maintype="application", subtype="pdf", filename=p.name
                )
            elif suffix in (".png", ".jpg", ".jpeg"):
                subtype = "png" if suffix == ".png" else "jpeg"
                msg.add_attachment(
                    data, maintype="image", subtype=subtype, filename=p.name
                )
            else:
                msg.add_attachment(
                    data,
                    maintype="application",
                    subtype="octet-stream",
                    filename=p.name,
                )
        except Exception as e:
            print(f"Failed to attach {path}: {e}")


def _build_summary_html(bill_data: dict) -> str:  # noqa: E501
    """HTML body for the bill summary email (preserved from app.py).

    Long lines below are intentional inline-CSS for email-client compatibility;
    splitting them harms readability without functional benefit.
    """
    month_name = bill_data.get("month_name", "Month")
    tmo_total = bill_data.get("total_bill", "0.00")
    plan_total = bill_data.get("plan_total", tmo_total)
    total_calc = bill_data.get("total_amount", 0.0)
    special_amount_val = bill_data.get("special_amount", 0.0)
    special_title = bill_data.get("special_title", "Special Pool")
    special_desc = bill_data.get("special_desc", "Included custom coverage")

    rows_html = ""
    for item in bill_data["structured_summary"]:
        rows_html += f"""
        <tr>
            <td style="padding: 12px 15px; border-bottom: 1px solid #eef2f6; color: #1e293b; font-weight: 500;">{item['name']}</td>
            <td style="padding: 12px 15px; border-bottom: 1px solid #eef2f6; color: #64748b; font-size: 13px;">{item['phone']}</td>
            <td style="padding: 12px 15px; border-bottom: 1px solid #eef2f6; color: #0f172a; font-weight: 600; text-align: right;">${item['amount']:.2f}</td>
        </tr>
        """

    b_style = (
        "margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "
        "'Segoe UI', Roboto, Helvetica, Arial, sans-serif; "
        "background-color: #f1f5f9; color: #334155; -webkit-font-smoothing: antialiased;"
    )
    t_style = "background-color: #f1f5f9; padding: 40px 20px;"
    return f"""
    <!DOCTYPE html>
    <html>
    <head>
        <meta charset="utf-8">
        <meta name="viewport" content="width=device-width, initial-scale=1.0">
    </head>
    <body style="{b_style}">
        <table width="100%" border="0" cellspacing="0" cellpadding="0" style="{t_style}">
            <tr>
                <td align="center">
                    <table width="100%" max-width="600" border="0" cellspacing="0" cellpadding="0" style="background-color: #ffffff; border-radius: 16px; overflow: hidden; box-shadow: 0 20px 40px rgba(0,0,0,0.06); max-width: 600px; width: 100%;">
                        <tr>
                            <td style="background: linear-gradient(135deg, #1e293b 0%, #0f172a 100%); padding: 45px 40px 35px 40px; text-align: center; border-bottom: 4px solid #E20074;">
                                <h1 style="margin: 0; color: #ffffff; font-size: 26px; font-weight: 700; letter-spacing: -0.5px;">T-Mobile Statement</h1>
                                <p style="margin: 8px 0 0 0; color: #94a3b8; font-size: 15px; font-weight: 500; text-transform: uppercase; letter-spacing: 1px;">{month_name}</p>
                            </td>
                        </tr>
                        <tr>
                            <td style="padding: 40px;">
                                <div style="text-align: center; margin-bottom: 40px; background-color: #f8fafc; padding: 30px; border-radius: 12px; border: 1px solid #e2e8f0;">
                                    <p style="margin: 0; font-size: 13px; color: #64748b; text-transform: uppercase; letter-spacing: 1.5px; font-weight: 600;">Calculated Split Total</p>
                                    <h2 style="margin: 10px 0; font-size: 48px; color: #0f172a; font-weight: 800; letter-spacing: -1px;">${total_calc:.2f}</h2>
                                    <p style="margin: 0; font-size: 14px; color: #94a3b8;">Original Carrier Plan Bill: ${plan_total}</p>
                                </div>
                                <h3 style="margin: 0 0 15px 0; font-size: 15px; color: #475569; text-transform: uppercase; letter-spacing: 1px; font-weight: 700; border-bottom: 2px solid #f1f5f9; padding-bottom: 12px;">Line Breakdown</h3>
                                <table width="100%" border="0" cellspacing="0" cellpadding="0" style="margin-bottom: 35px; border-collapse: collapse;">
                                    {rows_html}
                                </table>
                                <table width="100%" border="0" cellspacing="0" cellpadding="0">
                                    <tr>
                                        <td style="background: linear-gradient(to right, #fdf2f8, #fbcfe8); border-left: 4px solid #E20074; padding: 25px; border-radius: 0 12px 12px 0;">
                                            <p style="margin: 0 0 8px 0; font-size: 13px; color: #be185d; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;">Action Required</p>
                                            <h4 style="margin: 0; font-size: 20px; color: #831843; font-weight: 700;">{special_title} <span style="float: right;">${special_amount_val:.2f}</span></h4>
                                            <p style="margin: 6px 0 0 0; font-size: 14px; color: #db2777; font-weight: 500;">{special_desc}</p>
                                        </td>
                                    </tr>
                                </table>
                            </td>
                        </tr>
                        <tr>
                            <td style="background-color: #f8fafc; padding: 25px 40px; text-align: center; border-top: 1px solid #e2e8f0;">
                                <p style="margin: 0; font-size: 13px; color: #94a3b8; font-weight: 500;">Auto-generated Report &bull; Original PDF Attached</p>
                            </td>
                        </tr>
                    </table>
                </td>
            </tr>
        </table>
    </body>
    </html>
    """


def send_bill_summary_email(bill_data: dict, attachment_path: str) -> bool:
    """Sends the monthly bill breakdown email. Returns True on success."""
    sender, password, recipients = _smtp_config()
    if not all([sender, password, recipients]):
        print("SMTP credentials not fully set. Skipping bill summary email.")
        return False

    separator = "\n" + "#" * 30 + "\n"
    plain_body = separator.join(
        [
            bill_data["header"],
            "\n".join(bill_data["summary"]),
            bill_data["total_calc"],
            bill_data["special"],
        ]
    )

    msg = EmailMessage()
    h_split = bill_data["header"].split(":")[0]
    msg["Subject"] = f"T-Mobile Bill Summary - {h_split}"
    msg["From"] = f"T-Mobile Automations <{sender}>"
    msg["To"] = ", ".join(recipients)
    msg.set_content(plain_body)
    msg.add_alternative(_build_summary_html(bill_data), subtype="html")
    _attach_files(msg, [attachment_path])

    print(f"Sending bill summary email to {len(recipients)} recipients...")
    return _send(msg, sender, password)


def send_confirmation_email(
    amount: float,
    recipient_name: str,
    confirmation_id: str,
    month_name: str,
    screenshot_path: str = None,
) -> bool:
    """Sends Zelle payment confirmation email."""
    sender, password, recipients = _smtp_config()
    if not all([sender, password, recipients]):
        print("SMTP credentials not fully set. Skipping confirmation email.")
        return False

    confirmation_recipients_str = os.getenv("CONFIRMATION_RECIPIENTS", "")
    if confirmation_recipients_str.strip():
        recipients = [
            e.strip() for e in confirmation_recipients_str.split(",") if e.strip()
        ]

    plain_body = (
        f"Zelle payment automation completed.\n\n"
        f"Amount:           ${amount:.2f}\n"
        f"Recipient:        {recipient_name}\n"
        f"Confirmation ID:  {confirmation_id or '(not captured)'}\n"
        f"For bill month:   {month_name}\n\n"
        f"Verify the transaction in your Bank of America app."
    )

    html_body = f"""
    <html><body style="font-family: -apple-system, sans-serif; background: #f1f5f9; padding: 40px;">
      <table style="max-width: 560px; margin: 0 auto; background: #fff; border-radius: 12px; padding: 32px; box-shadow: 0 8px 24px rgba(0,0,0,0.06);">
        <tr><td>
          <div style="border-left: 4px solid #16a34a; padding-left: 16px; margin-bottom: 24px;">
            <p style="margin: 0; font-size: 12px; color: #16a34a; font-weight: 700; text-transform: uppercase; letter-spacing: 1px;">Payment Sent</p>
            <h2 style="margin: 4px 0 0 0; font-size: 28px; color: #0f172a;">${amount:.2f} → {recipient_name}</h2>
          </div>
          <table style="width: 100%; border-collapse: collapse;">
            <tr><td style="padding: 8px 0; color: #64748b; font-size: 13px;">Bill Month</td><td style="text-align: right; color: #0f172a; font-weight: 500;">{month_name}</td></tr>
            <tr><td style="padding: 8px 0; color: #64748b; font-size: 13px;">Confirmation #</td><td style="text-align: right; color: #0f172a; font-weight: 500; font-family: monospace;">{confirmation_id or '(not captured)'}</td></tr>
          </table>
          <p style="margin-top: 24px; font-size: 13px; color: #94a3b8;">Verify in Bank of America.</p>
        </td></tr>
      </table>
    </body></html>
    """

    msg = EmailMessage()
    msg["Subject"] = (
        f"T-Mobile Auto-Pay Confirmed - ${amount:.2f} -> {recipient_name}"
    )
    msg["From"] = f"T-Mobile Automations <{sender}>"
    msg["To"] = ", ".join(recipients)
    msg.set_content(plain_body)
    msg.add_alternative(html_body, subtype="html")
    # NOTE: BoA confirmation screenshot is NOT attached - keeps the email
    # clean and avoids leaking account UI fragments. Screenshot is still
    # saved locally to zelle_confirmation_*.png for audit.

    print(f"Sending confirmation email to {len(recipients)} recipients...")
    return _send(msg, sender, password)


def send_failure_alert(stage: str, error: str, attachments=None) -> bool:
    """Sends a failure alert email with logs/screenshots attached."""
    sender, password, _ = _smtp_config()
    recipients = _failure_alert_recipients()
    if not all([sender, password, recipients]):
        print("SMTP credentials not fully set. Cannot send failure alert.")
        return False

    body = (
        f"T-Mobile automation failed at stage: {stage}\n\n"
        f"Error:\n{error}\n\n"
        f"Attachments (if any) contain screenshots and the failing PDF.\n"
        f"Check ~/.tmo_state/ and automation.log for full context."
    )

    msg = EmailMessage()
    msg["Subject"] = f"[ALERT] T-Mobile Automation Failed: {stage}"
    msg["From"] = f"T-Mobile Automations <{sender}>"
    msg["To"] = ", ".join(recipients)
    msg.set_content(body)
    _attach_files(msg, attachments or [])

    print(f"Sending failure alert to {recipients} (stage={stage})...")
    return _send(msg, sender, password)


def send_2fa_alert() -> bool:
    """Sends email alert for T-Mobile MFA push-approval prompt."""
    sender, password, _ = _smtp_config()
    recipient = os.getenv("MFA_ALERT_EMAIL", "").strip()
    if not recipient:
        recipients = _failure_alert_recipients()
        recipient = recipients[0] if recipients else None
    if not all([sender, password, recipient]):
        print("SMTP credentials missing. Cannot send 2FA alert email.")
        return False

    msg = EmailMessage()
    msg["Subject"] = "ACTION REQUIRED: T-Mobile Login Notification"
    msg["From"] = f"T-Mobile Automation <{sender}>"
    msg["To"] = recipient
    msg.set_content(
        "T-Mobile automation is waiting for approval on your phone.\n\n"
        "Please open the T-Life app and approve the request."
    )
    return _send(msg, sender, password)
