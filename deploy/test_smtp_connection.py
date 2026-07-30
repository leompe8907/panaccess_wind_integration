"""
Prueba rápida y no destructiva de la conexión SMTP configurada en .env.
No envía ningún correo real -- solo EHLO + STARTTLS + LOGIN.

Uso (en el servidor, con el venv activado, desde la raíz del proyecto):
    python deploy/test_smtp_connection.py
"""
import os
import smtplib
import ssl
import sys

env = {}
env_path = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))), ".env")
with open(env_path, "r", encoding="utf-8", errors="ignore") as f:
    for line in f:
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, _, v = line.partition("=")
        env[k.strip()] = v.strip().strip('"').strip("'")

host = env.get("EMAIL_HOST", "smtp.sendgrid.net")
port = int(env.get("EMAIL_PORT", "587"))
user = env.get("EMAIL_HOST_USER", "")
password = env.get("EMAIL_HOST_PASSWORD", "")

print(f"Conectando a {host}:{port} ...")
try:
    server = smtplib.SMTP(host, port, timeout=15)
    code, msg = server.ehlo()
    print(f"EHLO -> {code} {msg[:80]}")
    server.starttls(context=ssl.create_default_context())
    print("STARTTLS OK")
    code, msg = server.ehlo()
    print(f"EHLO (post-TLS) -> {code} {msg[:80]}")

    print(f"Probando AUTH con usuario tal cual está en .env: '{user}'")
    try:
        code, msg = server.login(user, password)
        print(f"LOGIN OK -> {code} {msg}")
    except smtplib.SMTPAuthenticationError as e:
        print(f"LOGIN RECHAZADO (error de credenciales real) -> {e}")
    except smtplib.SMTPServerDisconnected as e:
        print(f"CONEXIÓN CORTADA durante el auth (mismo error visto en los logs) -> {e}")
        sys.exit(0)

    server.quit()
    print("Conexión cerrada limpiamente.")
except Exception as e:
    print(f"FALLO DE CONEXIÓN: {type(e).__name__}: {e}")
