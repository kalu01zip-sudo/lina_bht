# auth_utils.py
"""
Security utilities:
  - bcrypt password hashing
  - JWT access + refresh token
  - 6-digit OTP generation
  - Email sender (SMTP / dev-mode console)
"""

import os, random, string, smtplib
from datetime import datetime, timedelta
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from typing import Optional

from jose import JWTError, jwt
from passlib.context import CryptContext

# ── Config (set in .env) ──────────────────────────────────────
SECRET_KEY           = os.environ.get("SECRET_KEY",           "change-me-in-production")
REFRESH_SECRET_KEY   = os.environ.get("REFRESH_SECRET_KEY",   "change-refresh-secret")
ALGORITHM            = "HS256"
ACCESS_EXPIRE_MIN    = int(os.environ.get("ACCESS_TOKEN_EXPIRE_MINUTES", 60))
REFRESH_EXPIRE_DAYS  = int(os.environ.get("REFRESH_TOKEN_EXPIRE_DAYS",   30))

SMTP_HOST     = os.environ.get("SMTP_HOST",     "smtp.gmail.com")
SMTP_PORT     = int(os.environ.get("SMTP_PORT", "587"))
SMTP_USER     = os.environ.get("SMTP_USER",     "")
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "")
FROM_EMAIL    = os.environ.get("FROM_EMAIL",    "noreply@skinsense.app")
APP_NAME      = "SkinSense"
OTP_EXPIRE_MINUTES = 10

# ── Password hashing ─────────────────────────────────────────
_pwd = CryptContext(schemes=["bcrypt"], deprecated="auto")

def hash_password(plain: str) -> str:     return _pwd.hash(plain)
def verify_password(plain: str, h: str) -> bool: return _pwd.verify(plain, h)

# ── JWT ───────────────────────────────────────────────────────
def create_access_token(user_id: str, email: str) -> str:
    return jwt.encode({
        "sub":   user_id, "email": email, "type": "access",
        "exp":   datetime.utcnow() + timedelta(minutes=ACCESS_EXPIRE_MIN),
        "iat":   datetime.utcnow(),
    }, SECRET_KEY, algorithm=ALGORITHM)

def create_refresh_token(user_id: str) -> str:
    return jwt.encode({
        "sub":  user_id, "type": "refresh",
        "exp":  datetime.utcnow() + timedelta(days=REFRESH_EXPIRE_DAYS),
        "iat":  datetime.utcnow(),
    }, REFRESH_SECRET_KEY, algorithm=ALGORITHM)

def decode_access_token(token: str) -> Optional[dict]:
    try:
        p = jwt.decode(token, SECRET_KEY, algorithms=[ALGORITHM])
        return p if p.get("type") == "access" else None
    except JWTError:
        return None

def decode_refresh_token(token: str) -> Optional[dict]:
    try:
        p = jwt.decode(token, REFRESH_SECRET_KEY, algorithms=[ALGORITHM])
        return p if p.get("type") == "refresh" else None
    except JWTError:
        return None

def access_expiry()  -> datetime: return datetime.utcnow() + timedelta(minutes=ACCESS_EXPIRE_MIN)
def refresh_expiry() -> datetime: return datetime.utcnow() + timedelta(days=REFRESH_EXPIRE_DAYS)
def otp_expiry()     -> datetime: return datetime.utcnow() + timedelta(minutes=OTP_EXPIRE_MINUTES)

# ── OTP ───────────────────────────────────────────────────────
def generate_otp() -> str:
    return "".join(random.choices(string.digits, k=6))

# ── Email ─────────────────────────────────────────────────────
def _send_email(to: str, subject: str, html: str) -> bool:
    if not SMTP_USER or not SMTP_PASSWORD:
        print(f"\n📧 [EMAIL MOCK] To: {to}\nSubject: {subject}\n{html}\n")
        return True
    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"]    = f"{APP_NAME} <{FROM_EMAIL}>"
        msg["To"]      = to
        msg.attach(MIMEText(html, "html"))
        with smtplib.SMTP(SMTP_HOST, SMTP_PORT) as s:
            s.ehlo(); s.starttls()
            s.login(SMTP_USER, SMTP_PASSWORD)
            s.sendmail(FROM_EMAIL, to, msg.as_string())
        return True
    except Exception as e:
        print(f"❌ Email error: {e}")
        return False

def send_verification_email(to: str, name: str, otp: str) -> bool:
    return _send_email(to, f"Verify your {APP_NAME} account", f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:32px;">
      <h2 style="color:#1A4A7A;">Welcome to {APP_NAME} 🌿</h2>
      <p>Hi <strong>{name or 'there'}</strong>, your verification code is:</p>
      <div style="background:#F0F7FF;border-radius:12px;padding:24px;text-align:center;margin:24px 0;">
        <span style="font-size:40px;font-weight:bold;letter-spacing:8px;color:#1A4A7A;">{otp}</span>
      </div>
      <p style="color:#888;font-size:13px;">Expires in <strong>10 minutes</strong>. Do not share this code.</p>
    </div>""")

def send_reset_email(to: str, otp: str) -> bool:
    return _send_email(to, f"Reset your {APP_NAME} password", f"""
    <div style="font-family:Arial,sans-serif;max-width:480px;margin:auto;padding:32px;">
      <h2 style="color:#1A4A7A;">{APP_NAME} — Password Reset</h2>
      <p>Use this code to reset your password:</p>
      <div style="background:#FFF0F0;border-radius:12px;padding:24px;text-align:center;margin:24px 0;">
        <span style="font-size:40px;font-weight:bold;letter-spacing:8px;color:#D85A30;">{otp}</span>
      </div>
      <p style="color:#888;font-size:13px;">Expires in <strong>10 minutes</strong>. If you didn't request this, ignore this email.</p>
    </div>""")
