import hashlib
import secrets
import os
from cryptography.fernet import Fernet
import base64
import logging

logger = logging.getLogger(__name__)

def get_encryption_key():
    """Get or generate encryption key from environment"""
    key = os.getenv('ENCRYPTION_KEY')
    if not key:
        logger.warning("ENCRYPTION_KEY not set, using generated key (not persistent)")
        key = Fernet.generate_key().decode()
    return key.encode() if isinstance(key, str) else key

def hash_password(password: str) -> str:
    """Hash a password using SHA-256 with salt"""
    salt = secrets.token_hex(16)
    hashed = hashlib.sha256((password + salt).encode()).hexdigest()
    return f"{salt}:{hashed}"

def verify_password(password: str, stored_hash: str) -> bool:
    """Verify a password against stored hash"""
    try:
        salt, hashed = stored_hash.split(':')
        check_hash = hashlib.sha256((password + salt).encode()).hexdigest()
        return check_hash == hashed
    except (ValueError, AttributeError):
        return False

def generate_assignment_id() -> str:
    """Generate a unique assignment ID"""
    return f"ASN-{secrets.token_hex(8).upper()}"

def generate_submission_id() -> str:
    """Generate a unique submission ID"""
    return f"SUB-{secrets.token_hex(8).upper()}"

def generate_student_id() -> str:
    """Generate a unique student ID"""
    return f"S{secrets.token_hex(4).upper()}"

def generate_teacher_id() -> str:
    """Generate a unique teacher ID"""
    return f"T{secrets.token_hex(4).upper()}"

def encrypt_api_key(api_key: str) -> str:
    """Encrypt an API key for storage"""
    try:
        key = get_encryption_key()
        f = Fernet(key)
        encrypted = f.encrypt(api_key.encode())
        return base64.b64encode(encrypted).decode()
    except Exception as e:
        logger.error(f"Error encrypting API key: {e}")
        return None

def decrypt_api_key(encrypted_key: str) -> str:
    """Decrypt a stored API key"""
    try:
        key = get_encryption_key()
        f = Fernet(key)
        decoded = base64.b64decode(encrypted_key.encode())
        decrypted = f.decrypt(decoded)
        return decrypted.decode()
    except Exception as e:
        logger.error(f"Error decrypting API key: {e}")
        return None

def generate_token(length: int = 32) -> str:
    """Generate a secure random token"""
    return secrets.token_urlsafe(length)


def validate_password(password: str) -> tuple[bool, str]:
    """
    Validate password: min 6 characters, must contain both letters and numbers.
    Returns (ok, error_message). error_message is empty when ok is True.
    """
    if len(password) < 6:
        return False, "Password must be at least 6 characters"
    if not any(c.isalpha() for c in password):
        return False, "Password must contain at least one letter"
    if not any(c.isdigit() for c in password):
        return False, "Password must contain at least one number"
    return True, ""
