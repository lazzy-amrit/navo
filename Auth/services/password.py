from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerifyMismatchError

hasher = PasswordHasher()


def hash_password(password):
    return hasher.hash(password)


def verify_password(password, password_hash):
    if not password_hash:
        return False
    try:
        return hasher.verify(password_hash, password)
    except (VerifyMismatchError, InvalidHashError):
        return False
