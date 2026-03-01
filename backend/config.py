"""
Configuration module for Huddle.

Reads from .env file and environment variables with sensible defaults.
Supports environment-based defaults (development/staging/production).

Resolution order for each value: env var → .env file → environment-specific default.
The .env parser populates os.environ (without overriding), so os.environ.get()
handles the full chain.
"""

import json
import logging
import os
from pathlib import Path

logger = logging.getLogger("huddle")

# Load .env file if it exists (simple parser, no dependencies)
_env_file = Path(__file__).parent / ".env"
if _env_file.exists():
    with open(_env_file) as f:
        for line in f:
            line = line.strip()
            if not line or line.startswith("#"):
                continue
            if "=" in line:
                key, _, value = line.partition("=")
                key = key.strip()
                value = value.strip()
                # Don't override existing environment variables
                if key not in os.environ:
                    os.environ[key] = value

# Environment-specific defaults
_ENV_DEFAULTS = {
    "development": {
        "DEBUG": True,
        "CORS_ORIGINS": ["*"],
        "SESSION_MAX_AGE": 2592000,
        "LOG_LEVEL": "DEBUG",
        "BACKUP_RETENTION_DAYS": 30,
        "RATE_LIMIT_AUTH": 5,
        "PI_SCREEN_CONTROL": True,
        "SENTRY_DSN": "",
    },
    "staging": {
        "DEBUG": True,
        "CORS_ORIGINS": ["*"],
        "SESSION_MAX_AGE": 2592000,
        "LOG_LEVEL": "INFO",
        "BACKUP_RETENTION_DAYS": 30,
        "RATE_LIMIT_AUTH": 5,
        "PI_SCREEN_CONTROL": True,
        "SENTRY_DSN": "",
    },
    "production": {
        "DEBUG": False,
        "CORS_ORIGINS": ["https://huddle.rodgersgroup.au"],
        "SESSION_MAX_AGE": 2592000,
        "LOG_LEVEL": "INFO",
        "BACKUP_RETENTION_DAYS": 30,
        "RATE_LIMIT_AUTH": 5,
        "PI_SCREEN_CONTROL": True,
        "SENTRY_DSN": "",
    },
}


def _parse_value(raw, default):
    """Convert a string env var to the same type as the default value."""
    if raw is None:
        return default
    if isinstance(default, bool):
        return raw.lower() in ("true", "1", "yes")
    if isinstance(default, int):
        try:
            return int(raw)
        except ValueError:
            return default
    if isinstance(default, list):
        try:
            return json.loads(raw)
        except (json.JSONDecodeError, TypeError):
            return default
    return raw


def _get(key, env_defaults):
    """Resolve a config value: env var → .env → environment default."""
    default = env_defaults.get(key)
    raw = os.environ.get(key)
    if raw is None:
        return default
    return _parse_value(raw, default)


# Core configuration (no environment-specific defaults)
SECRET_KEY = os.environ.get("SECRET_KEY", "huddle-chores-secret-change-in-production")
RESEND_API_KEY = os.environ.get("RESEND_API_KEY", "")
FUEL_API_KEY = os.environ.get("FUEL_API_KEY", "")
DATABASE_URL = os.environ.get("DATABASE_URL", "sqlite:///chores.db")
ENVIRONMENT = os.environ.get("ENVIRONMENT", "development")
GA4_MEASUREMENT_ID = os.environ.get("GA4_MEASUREMENT_ID", "")

# Superadmin emails (comma-separated in env var)
SUPERADMIN_EMAILS = [e.strip() for e in os.getenv("SUPERADMIN_EMAILS", "").split(",") if e.strip()]

# ntfy push notification config
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "")
NTFY_SERVER = os.environ.get("NTFY_SERVER", "https://ntfy.sh")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")

# Feedback email control
FEEDBACK_EMAIL_ENABLED = os.environ.get("FEEDBACK_EMAIL_ENABLED", "").lower() in ("true", "1", "yes") if os.environ.get("FEEDBACK_EMAIL_ENABLED") else (ENVIRONMENT != "development")

# Resolve environment-specific defaults
_defaults = _ENV_DEFAULTS.get(ENVIRONMENT, _ENV_DEFAULTS["development"])

DEBUG = _get("DEBUG", _defaults)
CORS_ORIGINS = _get("CORS_ORIGINS", _defaults)
SESSION_MAX_AGE = _get("SESSION_MAX_AGE", _defaults)
LOG_LEVEL = _get("LOG_LEVEL", _defaults)
BACKUP_RETENTION_DAYS = _get("BACKUP_RETENTION_DAYS", _defaults)
RATE_LIMIT_AUTH = _get("RATE_LIMIT_AUTH", _defaults)
PI_SCREEN_CONTROL = _get("PI_SCREEN_CONTROL", _defaults)
SENTRY_DSN = _get("SENTRY_DSN", _defaults)

# Token auth settings (AnyList-style access/refresh tokens)
ACCESS_TOKEN_MAX_AGE = int(os.environ.get("ACCESS_TOKEN_MAX_AGE", "900"))  # 15 minutes
REFRESH_TOKEN_MAX_AGE = int(os.environ.get("REFRESH_TOKEN_MAX_AGE", "2592000"))  # 30 days

_DEFAULT_SECRET = "huddle-chores-secret-change-in-production"
_RECOGNIZED_ENVIRONMENTS = set(_ENV_DEFAULTS.keys())


def check_config():
    """Log warnings for insecure or missing configuration. Raises in production if SECRET_KEY is default."""
    if SECRET_KEY == _DEFAULT_SECRET:
        if ENVIRONMENT == "production":
            raise RuntimeError(
                "FATAL: SECRET_KEY is still the default value in production! "
                "Generate a random key with: python3 -c \"import secrets; print(secrets.token_urlsafe(48))\" "
                "and set it in .env"
            )
        logger.warning(
            "SECRET_KEY is still the default value! "
            "Set a random key in .env for production use."
        )
    if ENVIRONMENT == "production" and not RESEND_API_KEY:
        logger.warning("RESEND_API_KEY is not set — magic link emails will print to console.")
    if ENVIRONMENT not in _RECOGNIZED_ENVIRONMENTS:
        logger.warning(
            "ENVIRONMENT=%r is not recognized (expected one of %s). "
            "Falling back to development defaults.",
            ENVIRONMENT,
            ", ".join(sorted(_RECOGNIZED_ENVIRONMENTS)),
        )


def log_config():
    """Log the active configuration at INFO level, masking secrets."""
    def _mask(val):
        if not val:
            return "(not set)"
        return val[:4] + "****"

    logger.info(
        "Huddle config: ENVIRONMENT=%s DEBUG=%s LOG_LEVEL=%s\n"
        "  CORS_ORIGINS=%s SESSION_MAX_AGE=%s RATE_LIMIT_AUTH=%s\n"
        "  PI_SCREEN_CONTROL=%s BACKUP_RETENTION_DAYS=%s SENTRY_DSN=%s\n"
        "  SECRET_KEY=%s RESEND_API_KEY=%s FUEL_API_KEY=%s",
        ENVIRONMENT, DEBUG, LOG_LEVEL,
        json.dumps(CORS_ORIGINS), SESSION_MAX_AGE, RATE_LIMIT_AUTH,
        PI_SCREEN_CONTROL, BACKUP_RETENTION_DAYS,
        _mask(SENTRY_DSN),
        _mask(SECRET_KEY), _mask(RESEND_API_KEY), _mask(FUEL_API_KEY),
    )
