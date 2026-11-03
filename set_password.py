#!/usr/bin/env python3
"""Crea o cambia la contraseña del panel. Uso: sudo venv/bin/python3 set_password.py"""
import getpass
import json
import os
import secrets
import sys

from werkzeug.security import generate_password_hash

CONFIG_PATH = os.environ.get("WEBCUPS_CONFIG", "/etc/webcups.conf")
MIN_LENGTH = 8


def main():
    password = getpass.getpass("Nueva contraseña del panel: ")
    if len(password) < MIN_LENGTH:
        sys.exit(f"Debe tener al menos {MIN_LENGTH} caracteres.")
    if password != getpass.getpass("Repite la contraseña: "):
        sys.exit("Las contraseñas no coinciden.")

    # Una clave de sesión nueva cierra las sesiones abiertas con la contraseña anterior
    config = {
        "password_hash": generate_password_hash(password),
        "secret_key": secrets.token_hex(32)
    }

    tmp = CONFIG_PATH + ".tmp"
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w") as f:
        json.dump(config, f)
    os.replace(tmp, CONFIG_PATH)

    print(f"Contraseña guardada en {CONFIG_PATH}. Reinicia el servicio: systemctl restart webcups")


if __name__ == "__main__":
    main()
