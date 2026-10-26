"""Maintenance entry point (BE-10): `python -m jolt.maintenance [--dry-run]`.

Runs one sweep (jolt/services/maintenance.py: abandoned uploads and data
retention) against the configured stores and exits: 0 on success, 1 on error.
In Azure it runs daily as the `jolt-maintenance` Container Apps Job, from the
same image and with the same Managed Identity and settings as `jolt-api`
(infra/modules/containerapp.bicep). By hand:

    az containerapp job start -g jolt-prod -n jolt-maintenance
    python -m jolt.maintenance --dry-run       # locally, against .env settings

There is no HTTP route for this on purpose: nothing to guard, nothing exposed.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import sys
from typing import Optional

from jolt import telemetry
from jolt.config import Settings, get_settings
from jolt.runtime import Runtime
from jolt.services.maintenance import MaintenanceService, SweepReport

logger = logging.getLogger("jolt.maintenance")


async def run(settings: Settings, *, dry_run: bool = False) -> SweepReport:
    runtime = await Runtime.create(settings)
    try:
        return await MaintenanceService(runtime).sweep(dry_run=dry_run)
    finally:
        await runtime.close()


def main(argv: Optional[list[str]] = None, settings: Optional[Settings] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m jolt.maintenance", description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="count, delete nothing")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    settings = settings or get_settings()
    telemetry.configure_telemetry(settings, service_name="jolt-maintenance")
    try:
        report = asyncio.run(run(settings, dry_run=args.dry_run))
    except Exception:
        logger.exception("maintenance sweep failed")
        return 1
    finally:
        telemetry.flush()
    print(json.dumps(report.as_dict()))
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
