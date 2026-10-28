"""Claves de API y tokens: siempre en el Credential Manager de Windows (keyring), nunca en los ajustes ni en el disco."""
import keyring

SERVICE = "ZH3 Marea"
KEYS = {"spicy_lyrics": "Spicy Lyrics"}  # nombre interno -> etiqueta en Ajustes


class KeyringSecrets:
    def get(self, name: str) -> str:
        return keyring.get_password(SERVICE, name) or ""

    def set(self, name: str, value: str):
        value = value.strip()
        if not value:
            self.delete(name)
        else:
            keyring.set_password(SERVICE, name, value)

    def delete(self, name: str):
        try:
            keyring.delete_password(SERVICE, name)
        except keyring.errors.PasswordDeleteError:
            pass  # ya no estaba
