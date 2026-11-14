"""Claves de API y tokens: siempre en el Credential Manager de Windows (keyring), nunca en los ajustes ni en el disco."""
import logging

import keyring

log = logging.getLogger(__name__)

SERVICE = "ZH3 Marea"
KEYS = {"spicy_lyrics": "Spicy Lyrics"}  # nombre interno -> etiqueta en Ajustes


class KeyringSecrets:
    def get(self, name: str) -> str:
        try:
            return keyring.get_password(SERVICE, name) or ""
        except keyring.errors.KeyringError as e:
            log.warning("Failed reading keyring secret %s: %s", name, e)
            return ""

    def set(self, name: str, value: str):
        value = value.strip()
        if not value:
            self.delete(name)
        else:
            try:
                keyring.set_password(SERVICE, name, value)
            except keyring.errors.KeyringError as e:
                log.warning("Failed saving keyring secret %s: %s", name, e)

    def delete(self, name: str):
        try:
            keyring.delete_password(SERVICE, name)
        except (keyring.errors.PasswordDeleteError, keyring.errors.KeyringError):
            pass  # ya no estaba o keyring no disponible
