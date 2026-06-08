from cryptography.fernet import Fernet

from appConfig import PanaccessConfig

PanaccessConfig.validate()

ENCRYPTION_KEY = PanaccessConfig.KEY
fernet = Fernet(ENCRYPTION_KEY)

def encrypt_value(value: str) -> str:
    if not value:
        return ""
    return fernet.encrypt(value.encode()).decode()

def decrypt_value(encrypted: str) -> str:
    if not encrypted:
        return ""
    return fernet.decrypt(encrypted.encode()).decode()
