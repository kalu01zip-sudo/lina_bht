import smtplib
import os

from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart


SMTP_HOST = os.getenv("SMTP_HOST")

SMTP_PORT = int(
    os.getenv("SMTP_PORT", 587)
)

SMTP_USER = os.getenv("SMTP_USER")

SMTP_PASSWORD = os.getenv("SMTP_PASSWORD")

FROM_EMAIL = os.getenv("FROM_EMAIL")


def send_support_email(

    to_email: str,

    subject: str,

    body: str
):

    msg = MIMEMultipart()

    msg["From"] = FROM_EMAIL

    msg["To"] = to_email

    msg["Subject"] = subject

    msg.attach(
        MIMEText(body, "plain")
    )

    server = smtplib.SMTP(
        SMTP_HOST,
        SMTP_PORT
    )

    server.starttls()

    server.login(
        SMTP_USER,
        SMTP_PASSWORD
    )

    server.sendmail(

        FROM_EMAIL,

        to_email,

        msg.as_string()
    )

    server.quit()