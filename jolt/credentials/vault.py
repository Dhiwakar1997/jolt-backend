"""Key Vault access for per-user provider keys (LLD §5.1).

The raw key lives only here, named by user_id+provider. Managed Identity to Key
Vault (no stored key material anywhere). The plaintext never lands in Cosmos, in a
log, or in a response body.
"""

from __future__ import annotations

import re

from jolt.config import Settings, get_settings


def secret_name(user_id: str, provider: str) -> str:
    """Key Vault secret names allow only [0-9a-zA-Z-]. Sanitise both parts."""
    safe_user = re.sub(r"[^0-9a-zA-Z-]", "-", user_id)
    safe_provider = re.sub(r"[^0-9a-zA-Z-]", "-", provider)
    return f"apikey-{safe_user}-{safe_provider}"


class VaultClient:
    def __init__(self, settings: Settings | None = None) -> None:
        self._settings = settings or get_settings()
        self._client = None  # lazily built

    def _ensure(self):
        if self._client is not None:
            return self._client
        if not self._settings.keyvault_url:
            raise RuntimeError(
                "KEYVAULT_URL is not set. Fill it in .env after deploying Key Vault."
            )
        from azure.identity.aio import DefaultAzureCredential
        from azure.keyvault.secrets.aio import SecretClient

        self._client = SecretClient(
            vault_url=self._settings.keyvault_url, credential=DefaultAzureCredential()
        )
        return self._client

    async def set_key(self, user_id: str, provider: str, plaintext: str) -> str:
        client = self._ensure()
        name = secret_name(user_id, provider)
        await client.set_secret(name, plaintext)
        return name

    async def get_key(self, user_id: str, provider: str) -> str:
        """Read the plaintext — only ever called from inference.py at call time."""
        client = self._ensure()
        secret = await client.get_secret(secret_name(user_id, provider))
        return secret.value

    async def delete_key(self, user_id: str, provider: str) -> None:
        client = self._ensure()
        poller = await client.begin_delete_secret(secret_name(user_id, provider))
        try:
            await poller.wait()
        except Exception:
            pass  # already gone / soft-delete in progress

    async def close(self) -> None:
        if self._client is not None:
            await self._client.close()
