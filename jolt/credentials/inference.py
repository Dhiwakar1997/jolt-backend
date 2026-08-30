"""Key-backed model calls — the dormant seam (LLD §4, §5.1, §11).

Ships with NO callers in MVP. It exists so the later key-backed features (on-demand
doc processing, question-answering) attach here without a structural change. When
they arrive they are the only paths that put inference on Jolt's own compute, and
they remain strictly opt-in and billed to the user's own key.

Custody rules (LLD §5.1): the plaintext is retrieved from Key Vault via Managed
Identity only at the moment of a call, held in memory for that call, and discarded
— never cached to disk, never attached to a log span.
"""

from __future__ import annotations

from jolt.credentials.vault import VaultClient


class InferenceNotEnabled(RuntimeError):
    """Raised if a key-backed call is attempted in MVP, where it is dormant."""


async def call_model(
    user_id: str,
    provider: str,
    *,
    vault: VaultClient,
    **_kwargs,
):
    """Placeholder for a key-backed model call. Dormant in MVP.

    The shape is intentionally fixed now: resolve the key at call time, use it,
    discard it. Wiring a real provider call is a later change contained entirely
    within this function.
    """
    raise InferenceNotEnabled(
        "Key-backed inference is not enabled in MVP. The key is captured and stored "
        "(LLD §5.1) but no feature consumes it yet."
    )
