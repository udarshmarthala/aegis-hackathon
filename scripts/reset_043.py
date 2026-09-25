"""Undo INC-043: checkout back to 1.4.1 and its fault cleared.

The redeploy goes through the runtime adapter and is recorded in
``deployment_attempts`` as an operator deploy, the same way ``inject_043.py``
records the bad one, so the history stays a true account of what ran. The fault
is then cleared in the workload itself: 1.4.1 carries no baked fault, but a
fault injected by hand through ``/admin/fault`` would otherwise survive.

Usage:  backend/.venv/Scripts/python.exe scripts/reset_043.py   # or: make reset-043
"""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

from inject_043 import GOOD_VERSION, SERVICE, redeploy, use_host_endpoints

# Run inside the workload container, against its own loopback: the recreated
# container publishes no host port, and the workload image has no curl.
_CLEAR_FAULT = (
    "import urllib.request;"
    "r=urllib.request.Request('http://127.0.0.1:8080/admin/fault',"
    "data=b'{\"mode\":\"none\"}',headers={'Content-Type':'application/json'});"
    "print(urllib.request.urlopen(r,timeout=5).read().decode())"
)


async def clear_fault() -> int:
    use_host_endpoints()
    from aegis.core.config import get_settings
    from aegis.integrations.runtime import get_adapter

    adapter = get_adapter(get_settings())
    try:
        cleared = 0
        for container in adapter._list_containers(SERVICE):
            if container.status != "running":
                continue
            code, output = container.exec_run(["python", "-c", _CLEAR_FAULT])
            text = output.decode(errors="replace").strip()
            if code != 0:
                print(f"  could not clear the fault on {container.name}: {text}", file=sys.stderr)
                return 1
            print(f"  {container.name}: fault {json.loads(text)['applied']['mode']}")
            cleared += 1
        return 0 if cleared else 1
    finally:
        await adapter.close()


def main() -> int:
    code = asyncio.run(
        redeploy(
            to_version=GOOD_VERSION,
            actor="operator:reset-043",
            scenario="reset-043",
            baseline=False,
        )
    )
    if code != 0:
        return code
    return asyncio.run(clear_fault())


if __name__ == "__main__":
    raise SystemExit(main())
