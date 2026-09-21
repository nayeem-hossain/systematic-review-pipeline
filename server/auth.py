from __future__ import annotations

import hashlib
import json
import os
import secrets
from pathlib import Path
from typing import Optional, Dict, Any

AUTH_DB_FILE = Path("runs_web/users.json")
AUTH_DB_FILE.parent.mkdir(parents=True, exist_ok=True)

def _load_users() -> Dict[str, Dict[str, Any]]:
    if not AUTH_DB_FILE.exists():
        return {}
    try:
        return json.loads(AUTH_DB_FILE.read_text(encoding="utf-8"))
    except Exception:
        return {}

def _save_users(users: Dict[str, Dict[str, Any]]) -> None:
    AUTH_DB_FILE.write_text(json.dumps(users, indent=2), encoding="utf-8")

def hash_password(password: str, salt: Optional[str] = None) -> tuple[str, str]:
    if salt is None:
        salt = secrets.token_hex(16)
    key = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt.encode("utf-8"), 100000)
    return key.hex(), salt

def register_user(email: str, password: str) -> Dict[str, Any]:
    email_clean = email.strip().lower()
    if not email_clean or "@" not in email_clean:
        raise ValueError("Invalid email address")
    if len(password) < 6:
        raise ValueError("Password must be at least 6 characters")

    users = _load_users()
    if email_clean in users:
        raise ValueError("User with this email already exists")

    pwd_hash, salt = hash_password(password)
    user_id = f"usr_{secrets.token_hex(6)}"
    user_data = {
        "user_id": user_id,
        "email": email_clean,
        "hash": pwd_hash,
        "salt": salt,
        "api_keys": {},
    }
    users[email_clean] = user_data
    _save_users(users)
    return {"user_id": user_id, "email": email_clean, "api_keys": {}}

def authenticate_user(email: str, password: str) -> Optional[Dict[str, Any]]:
    email_clean = email.strip().lower()
    users = _load_users()
    user_data = users.get(email_clean)
    if not user_data:
        return None

    computed_hash, _ = hash_password(password, user_data["salt"])
    if secrets.compare_digest(computed_hash, user_data["hash"]):
        return {
            "user_id": user_data["user_id"],
            "email": user_data["email"],
            "api_keys": user_data.get("api_keys", {}),
        }
    return None

def update_user_profile(user_id: str, new_email: Optional[str] = None, new_password: Optional[str] = None, api_keys: Optional[Dict[str, str]] = None) -> Dict[str, Any]:
    users = _load_users()
    target_email = None
    target_user = None

    for email_k, u_data in users.items():
        if u_data.get("user_id") == user_id:
            target_email = email_k
            target_user = u_data
            break

    if not target_user or not target_email:
        raise ValueError("User not found")

    if new_email and new_email.strip().lower() != target_email:
        clean_new = new_email.strip().lower()
        if "@" not in clean_new:
            raise ValueError("Invalid email format")
        if clean_new in users:
            raise ValueError("Email already in use by another user")
        # Remove old key
        del users[target_email]
        target_user["email"] = clean_new
        target_email = clean_new

    if new_password:
        if len(new_password) < 6:
            raise ValueError("Password must be at least 6 characters")
        pwd_hash, salt = hash_password(new_password)
        target_user["hash"] = pwd_hash
        target_user["salt"] = salt

    if api_keys is not None:
        target_user["api_keys"] = api_keys

    users[target_email] = target_user
    _save_users(users)

    return {
        "user_id": target_user["user_id"],
        "email": target_user["email"],
        "api_keys": target_user.get("api_keys", {}),
    }

def delete_user_account(user_id: str) -> bool:
    users = _load_users()
    target_email = None

    for email_k, u_data in users.items():
        if u_data.get("user_id") == user_id:
            target_email = email_k
            break

    if target_email and target_email in users:
        del users[target_email]
        _save_users(users)
        return True
    return False
