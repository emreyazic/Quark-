"""Centralized logging configuration with sensitive data masking and rotating file handler."""

import os
import re
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
from typing import Optional


# Patterns for sensitive data that must be masked in logs
_SENSITIVE_PATTERNS = [
    (re.compile(r'(access_key|secret_key|client_secret|client_id|authorization|token)["\']?\s*[:=]\s*["\']?([^"\'\s&,;]{6,})', re.IGNORECASE), 2),
    (re.compile(r'(Bearer\s+)([A-Za-z0-9\-_.~+/=]{10,})', re.IGNORECASE), 2),
]


class SensitiveDataFilter(logging.Filter):
    """Filter that masks API keys, secrets, and auth tokens in log records."""

    def filter(self, record: logging.LogRecord) -> bool:
        if isinstance(record.msg, str):
            record.msg = self.mask_sensitive_text(record.msg)
        if record.args:
            if isinstance(record.args, dict):
                record.args = {k: self.mask_sensitive_value(v) for k, v in record.args.items()}
            elif isinstance(record.args, (list, tuple)):
                record.args = tuple(self.mask_sensitive_value(v) for v in record.args)
        return True

    @classmethod
    def mask_sensitive_text(cls, text: str) -> str:
        masked = text
        for pattern, group_idx in _SENSITIVE_PATTERNS:
            def replace_match(match):
                val = match.group(group_idx)
                if len(val) <= 6:
                    replacement = "***"
                else:
                    replacement = f"{val[:3]}***{val[-3:]}"
                full = match.group(0)
                return full[:match.start(group_idx) - match.start(0)] + replacement + full[match.end(group_idx) - match.start(0):]
            masked = pattern.sub(replace_match, masked)
        return masked

    @classmethod
    def mask_sensitive_value(cls, value):
        if isinstance(value, str):
            return cls.mask_sensitive_text(value)
        return value


def mask_secret(secret: Optional[str]) -> str:
    """Helper to safely format a secret for display or logging."""
    if not secret:
        return "(empty)"
    if len(secret) <= 6:
        return "***"
    return f"{secret[:3]}***{secret[-3:]}"


_configured = False


def setup_logging(log_dir: Optional[str] = None, log_level: int = logging.INFO) -> logging.Logger:
    """Initialize application-wide logging with console and rotating file output."""
    global _configured
    root_logger = logging.getLogger("jlcpcb_bom_tool")
    if _configured:
        return root_logger

    root_logger.setLevel(log_level)
    formatter = logging.Formatter(
        "[%(asctime)s] [%(levelname)s] [%(name)s]: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S"
    )
    sensitive_filter = SensitiveDataFilter()

    # Console Handler
    console_handler = logging.StreamHandler()
    console_handler.setFormatter(formatter)
    console_handler.addFilter(sensitive_filter)
    console_handler.setLevel(log_level)
    root_logger.addHandler(console_handler)

    # File Handler
    if log_dir is None:
        user_home = Path(os.getenv("LOCALAPPDATA") or Path.home() / ".jlcpcb_bom_tool")
        target_dir = user_home / "logs"
    else:
        target_dir = Path(log_dir)

    try:
        target_dir.mkdir(parents=True, exist_ok=True)
        log_file = target_dir / "app.log"
        file_handler = RotatingFileHandler(
            str(log_file),
            maxBytes=5 * 1024 * 1024,  # 5 MB
            backupCount=3,
            encoding="utf-8"
        )
        file_handler.setFormatter(formatter)
        file_handler.addFilter(sensitive_filter)
        file_handler.setLevel(log_level)
        root_logger.addHandler(file_handler)
    except Exception:
        # Fallback to console only if file access fails
        pass

    _configured = True
    return root_logger


def get_logger(name: str) -> logging.Logger:
    """Get a child logger under the jlcpcb_bom_tool namespace."""
    if not _configured:
        setup_logging()
    return logging.getLogger(f"jlcpcb_bom_tool.{name}")
