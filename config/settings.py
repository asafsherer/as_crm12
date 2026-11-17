import os
from pathlib import Path
from django.core.management.utils import get_random_secret_key

BASE_DIR = Path(__file__).resolve().parent.parent


def _secret_key():
    env = os.environ.get("SECRET_KEY")
    if env:
        return env
    f = BASE_DIR / ".secret_key"
    if f.exists():
        return f.read_text().strip()
    key = get_random_secret_key()
    f.write_text(key)
    return key


SECRET_KEY = _secret_key()
# Safe by default: debug mode is OFF unless explicitly switched on (start.bat does this on a local computer).
DEBUG = os.environ.get("DEBUG", "0") == "1"
# Encryption keys may be auto-created as local files ONLY on a local computer (DEBUG=1); online they must come from the environment.
ALLOW_LOCAL_KEYS = DEBUG
ALLOWED_HOSTS = os.environ.get("ALLOWED_HOSTS", "localhost,127.0.0.1").split(",")
CSRF_TRUSTED_ORIGINS = [o for o in os.environ.get("CSRF_TRUSTED_ORIGINS", "").split(",") if o]

INSTALLED_APPS = [
    "django.contrib.admin",
    "django.contrib.auth",
    "django.contrib.contenttypes",
    "django.contrib.sessions",
    "django.contrib.messages",
    "django.contrib.staticfiles",
    "leads",
]

MIDDLEWARE = [
    "django.middleware.security.SecurityMiddleware",
    "whitenoise.middleware.WhiteNoiseMiddleware",
    "django.contrib.sessions.middleware.SessionMiddleware",
    "django.middleware.common.CommonMiddleware",
    "django.middleware.csrf.CsrfViewMiddleware",
    "django.contrib.auth.middleware.AuthenticationMiddleware",
    "leads.middleware.TwoFactorMiddleware",
    "leads.middleware.SecurityHeadersMiddleware",
    "django.contrib.messages.middleware.MessageMiddleware",
    "django.middleware.clickjacking.XFrameOptionsMiddleware",
]

ROOT_URLCONF = "config.urls"

TEMPLATES = [
    {
        "BACKEND": "django.template.backends.django.DjangoTemplates",
        "DIRS": [],
        "APP_DIRS": True,
        "OPTIONS": {
            "context_processors": [
                "django.template.context_processors.request",
                "django.contrib.auth.context_processors.auth",
                "django.contrib.messages.context_processors.messages",
                "leads.context.crm",
            ],
        },
    },
]

WSGI_APPLICATION = "config.wsgi.application"

import dj_database_url

DATABASES = {
    "default": dj_database_url.config(
        default=f"sqlite:///{BASE_DIR / 'db.sqlite3'}", conn_max_age=60, conn_health_checks=True
    )
}

AUTH_PASSWORD_VALIDATORS = [
    {"NAME": "django.contrib.auth.password_validation.MinimumLengthValidator", "OPTIONS": {"min_length": 10}},
    {"NAME": "django.contrib.auth.password_validation.CommonPasswordValidator"},
    {"NAME": "django.contrib.auth.password_validation.NumericPasswordValidator"},
]

LANGUAGE_CODE = "he"
TIME_ZONE = "Asia/Jerusalem"
USE_I18N = True
USE_TZ = True

STATIC_URL = "static/"
STATIC_ROOT = BASE_DIR / "staticfiles"
MEDIA_ROOT = Path(os.environ.get("MEDIA_ROOT", BASE_DIR / "media"))  # private: served only via a login-protected view
BACKUP_DIR = Path(os.environ.get("BACKUP_DIR", BASE_DIR / "backups"))
# Cloudflare R2 file storage (optional). When all four are set, files go to the private bucket (encrypted before upload).
R2_ENDPOINT = os.environ.get("R2_ENDPOINT", "")  # https://<account-id>.r2.cloudflarestorage.com
R2_BUCKET = os.environ.get("R2_BUCKET", "")
R2_ACCESS_KEY_ID = os.environ.get("R2_ACCESS_KEY_ID", "")
R2_SECRET_ACCESS_KEY = os.environ.get("R2_SECRET_ACCESS_KEY", "")
USE_R2 = all((R2_ENDPOINT, R2_BUCKET, R2_ACCESS_KEY_ID, R2_SECRET_ACCESS_KEY))
STORAGES = {
    "default": {"BACKEND": "leads.storage.R2EncryptedStorage" if USE_R2 else "leads.storage.EncryptedFileSystemStorage"},
    "staticfiles": {"BACKEND": "whitenoise.storage.CompressedStaticFilesStorage"},
}

# Two-factor login (TOTP). On by default when running online (DEBUG=0).
REQUIRE_2FA = os.environ.get("REQUIRE_2FA", "0" if DEBUG else "1") == "1"
DEFAULT_AUTO_FIELD = "django.db.models.BigAutoField"

LOGIN_URL = "login"
LOGIN_REDIRECT_URL = "home"
LOGOUT_REDIRECT_URL = "login"

SESSION_COOKIE_AGE = 60 * 60 * 8
SESSION_COOKIE_HTTPONLY = True
SESSION_COOKIE_SAMESITE = "Lax"
SESSION_SAVE_EVERY_REQUEST = True       # idle sessions expire 8 hours after the LAST activity
CSRF_COOKIE_HTTPONLY = True
X_FRAME_OPTIONS = "DENY"
SECURE_REFERRER_POLICY = "same-origin"
SECURE_CROSS_ORIGIN_OPENER_POLICY = "same-origin"
DATA_UPLOAD_MAX_MEMORY_SIZE = 50 * 1024 * 1024
FILE_UPLOAD_MAX_MEMORY_SIZE = 20 * 1024 * 1024

# Antivirus (optional but recommended online): host/port of a ClamAV (clamd) service. If set and unreachable, uploads are blocked.
CLAMAV_HOST = os.environ.get("CLAMAV_HOST", "")
CLAMAV_PORT = int(os.environ.get("CLAMAV_PORT", "3310"))

# Shared counters for login/2FA lockouts. Needs: python manage.py createcachetable
CACHES = {"default": {"BACKEND": "django.core.cache.backends.db.DatabaseCache", "LOCATION": "crm_cache"}}

if not DEBUG:
    SECURE_SSL_REDIRECT = True
    SESSION_COOKIE_SECURE = True
    CSRF_COOKIE_SECURE = True
    SECURE_PROXY_SSL_HEADER = ("HTTP_X_FORWARDED_PROTO", "https")
    SECURE_HSTS_SECONDS = 60 * 60 * 24 * 365
    SECURE_HSTS_INCLUDE_SUBDOMAINS = True
    SECURE_CONTENT_TYPE_NOSNIFF = True

# AI features (optional). Needs an Anthropic API key; without it the AI buttons stay hidden.
def _api_key():
    env = os.environ.get("ANTHROPIC_API_KEY", "").strip()
    if env:
        return env
    f = BASE_DIR / "ai_key.txt"  # simplest option on a local computer: a text file with just the key
    return f.read_text(encoding="utf-8").strip() if f.exists() else ""


ANTHROPIC_API_KEY = _api_key()
CLAUDE_MODEL = os.environ.get("CLAUDE_MODEL", "claude-sonnet-5-5")

# --- v10: website lead intake and business mailbox ---
# The website (a Cloudflare Pages Function, see website_connect/) posts new leads here with this secret. Empty = intake switched off.
LEAD_INTAKE_TOKEN = os.environ.get("LEAD_INTAKE_TOKEN", "").strip()
# Business mailbox read over IMAP (Google Workspace: create an "App password"). Read-only: nothing is deleted or marked read there.
MAIL_IMAP_HOST = os.environ.get("MAIL_IMAP_HOST", "imap.gmail.com")
MAIL_IMAP_PORT = int(os.environ.get("MAIL_IMAP_PORT", "993"))
MAIL_IMAP_USER = os.environ.get("MAIL_IMAP_USER", "").strip()
MAIL_IMAP_PASSWORD = os.environ.get("MAIL_IMAP_PASSWORD", "").strip()
MAIL_IMAP_FOLDER = os.environ.get("MAIL_IMAP_FOLDER", "INBOX")
# Our own addresses (to tell sent mail from received): comma separated, e.g. office@as-finances.com
MAIL_OWN_ADDRESSES = [a.strip().lower() for a in os.environ.get("MAIL_OWN_ADDRESSES", MAIL_IMAP_USER).split(",") if a.strip()]

# --- free hosting: scheduled jobs are triggered by an external pinger, see leads/cron.py ---
CRON_TOKEN = os.environ.get("CRON_TOKEN", "").strip()
