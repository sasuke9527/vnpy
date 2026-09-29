from __future__ import annotations

import os
import secrets
import warnings
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping

PROFILES = ("overseas", "cn")

# action -> (max events, window seconds). "*_new" applies to devices younger
# than Settings.new_device_hours.
DEFAULT_RATE_LIMITS: dict[str, tuple[int, int]] = {
    "register_ip": (20, 3600),
    "write_ip": (120, 3600),
    "thread": (3, 3600),
    "thread_new": (1, 3600),
    "comment": (30, 3600),
    "comment_new": (10, 3600),
    "report": (20, 3600),
    "block": (60, 3600),
    "otp_start": (5, 3600),
    "otp_verify": (10, 3600),
}


@dataclass
class Settings:
    # "overseas": no real-identity binding at all (App Store outside mainland China).
    # "cn": 后台实名 -- a phone number must be bound before the device may post;
    #       the front end stays pseudonymous in both profiles.
    profile: str = "overseas"
    db_path: str = "anonboard.db"
    # Server-held secret that keys the per-thread alias derivation. Losing it
    # changes every alias; leaking it lets whoever holds it recompute aliases
    # from device ids. Keep it in a KMS/secret manager in production.
    alias_pepper: bytes = b""
    # Separate secret for hashing phone numbers (cn profile).
    phone_pepper: bytes = b""
    # Fernet key that encrypts bound phone numbers at rest (cn profile).
    phone_enc_key: str = ""
    admin_token: str = ""
    contact_email: str = "abuse@example.com"
    terms_version: str = "2026-09"
    retention_days: int = 180          # 网络安全法 第21条: logs >= 6 months
    signature_max_skew: int = 300      # seconds
    report_hide_threshold: int = 3     # distinct reporters that auto-hide content
    new_device_hours: int = 24
    max_devices_per_phone: int = 3
    thread_body_max: int = 1000
    comment_body_max: int = 500
    blocklist_path: str | None = None
    rate_limits: dict[str, tuple[int, int]] = field(default_factory=lambda: dict(DEFAULT_RATE_LIMITS))

    def __post_init__(self) -> None:
        if self.profile not in PROFILES:
            raise ValueError(f"profile must be one of {PROFILES}, got {self.profile!r}")


def _secret_from_env_or_file(env: Mapping[str, str], var: str, path: Path, nbytes: int = 32) -> bytes:
    raw = env.get(var)
    if raw:
        return bytes.fromhex(raw)
    if path.exists():
        return bytes.fromhex(path.read_text().strip())
    value = secrets.token_bytes(nbytes)
    path.write_text(value.hex())
    try:
        path.chmod(0o600)
    except OSError:
        pass
    warnings.warn(f"{var} not set; generated a new secret at {path}", stacklevel=2)
    return value


def load_settings(env: Mapping[str, str] | None = None) -> Settings:
    env = os.environ if env is None else env
    db_path = env.get("ANONBOARD_DB", "anonboard.db")
    base = Path(db_path if db_path != ":memory:" else "anonboard")
    alias_pepper = _secret_from_env_or_file(env, "ANONBOARD_ALIAS_PEPPER", base.with_suffix(".alias.pepper"))
    phone_pepper = _secret_from_env_or_file(env, "ANONBOARD_PHONE_PEPPER", base.with_suffix(".phone.pepper"))
    phone_enc_key = env.get("ANONBOARD_PHONE_ENC_KEY") or ""
    if not phone_enc_key:
        key_path = base.with_suffix(".phone.key")
        if key_path.exists():
            phone_enc_key = key_path.read_text().strip()
        else:
            from cryptography.fernet import Fernet

            phone_enc_key = Fernet.generate_key().decode()
            key_path.write_text(phone_enc_key)
            try:
                key_path.chmod(0o600)
            except OSError:
                pass
    admin_token = env.get("ANONBOARD_ADMIN_TOKEN") or ""
    if not admin_token:
        admin_token = secrets.token_urlsafe(24)
        warnings.warn("ANONBOARD_ADMIN_TOKEN not set; generated one for this process only", stacklevel=2)
    return Settings(
        profile=env.get("ANONBOARD_PROFILE", "overseas"),
        db_path=db_path,
        alias_pepper=alias_pepper,
        phone_pepper=phone_pepper,
        phone_enc_key=phone_enc_key,
        admin_token=admin_token,
        contact_email=env.get("ANONBOARD_CONTACT_EMAIL", "abuse@example.com"),
        terms_version=env.get("ANONBOARD_TERMS_VERSION", "2026-09"),
        retention_days=int(env.get("ANONBOARD_RETENTION_DAYS", "180")),
        report_hide_threshold=int(env.get("ANONBOARD_REPORT_HIDE_THRESHOLD", "3")),
        blocklist_path=env.get("ANONBOARD_BLOCKLIST") or None,
    )
