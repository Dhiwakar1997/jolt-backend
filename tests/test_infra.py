"""Infra-as-code stays in step with the code (BE-02).

The Bicep in `infra/` provisions what the app expects: every Cosmos container
with the partition key and composite indexes from `jolt/data/cosmos.py` (the app
cannot create them itself under Managed Identity), and only environment
variables that `Settings` reads. `.env.example` documents every setting.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

from jolt.config import Settings
from jolt.data.cosmos import COMPOSITE_INDEXES, CONTAINER_PARTITION_KEYS

ROOT = Path(__file__).resolve().parent.parent
INFRA = ROOT / "infra"

# Read by the Azure SDK (DefaultAzureCredential), not by Settings.
SDK_ENV = {"AZURE_CLIENT_ID"}


def test_cosmos_containers_match_the_code():
    spec = json.loads((INFRA / "cosmos-containers.json").read_text())["containers"]
    assert [c["name"] for c in spec] == list(CONTAINER_PARTITION_KEYS)
    for c in spec:
        assert c["partitionKey"] == CONTAINER_PARTITION_KEYS[c["name"]], c["name"]
        assert c["compositeIndexes"] == COMPOSITE_INDEXES.get(c["name"], []), c["name"]


def test_lld_composite_indexes_are_declared():
    """LLD §6: (user_id, due_at) for due reviews; status + timestamp for claims."""
    paths = {
        name: [[p["path"] for p in idx] for idx in indexes]
        for name, indexes in COMPOSITE_INDEXES.items()
    }
    assert ["/user_id", "/due_at"] in paths["concept_states"]
    assert ["/processing_status", "/created_at"] in paths["sources"]
    assert ["/status", "/created_at"] in paths["reviews"]


def test_container_app_env_vars_are_settings():
    bicep = (INFRA / "modules" / "containerapp.bicep").read_text()
    names = set(re.findall(r"\{ name: '([A-Z0-9_]+)'", bicep))
    assert {"COSMOS_ENDPOINT", "TOKEN_HASH_PEPPER", "CORS_ORIGINS", "AZURE_CLIENT_ID"} <= names
    sign_in = {"GOOGLE_OAUTH_CLIENT_ID", "MCP_SERVER_URL", "DEV_AUTH_ENABLED", "DEV_AUTH_SECRET"}
    assert sign_in <= names  # BE-03
    fields = {f.upper() for f in Settings.model_fields}
    assert names - SDK_ENV <= fields, names - SDK_ENV - fields


def test_env_example_documents_every_setting():
    text = (ROOT / ".env.example").read_text()
    documented = set(re.findall(r"^#?\s*([A-Z0-9_]+)=", text, flags=re.M))
    missing = {f.upper() for f in Settings.model_fields} - documented
    assert not missing, f".env.example is missing {sorted(missing)}"
