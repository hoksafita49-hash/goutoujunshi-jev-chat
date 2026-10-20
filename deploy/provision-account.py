#!/usr/bin/env python3
"""Provision the owner's first app account. Never print its password or token."""
import json
import os
from pathlib import Path
import secrets
import sys
import urllib.request


def main():
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    path = Path("/etc/goutou/application-user.json")
    pending = path.with_suffix(".json.pending")
    if path.exists():
        print("Existing application account credentials preserved.")
        return
    if not pending.exists():
        descriptor = os.open(pending, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
        with os.fdopen(descriptor, "w") as file:
            json.dump({"email": "owner@zovoeh.cn", "password": secrets.token_urlsafe(32)}, file)
            file.flush()
            os.fsync(file.fileno())
    credentials = json.loads(pending.read_text())
    environment = dict(line.split("=", 1) for line in Path("/etc/goutou/pocketbase.env").read_text().splitlines() if "=" in line)
    request = urllib.request.Request("http://127.0.0.1:8090/internal/cloud/provision",
        data=json.dumps(credentials).encode(), headers={"Content-Type": "application/json",
        "X-Internal-Key": environment["GOUTOU_PB_INTERNAL_KEY"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=15) as response:
        data = json.load(response)
    if not data.get("id"):
        raise RuntimeError("provision_failed")
    os.replace(pending, path)
    print("Application account provisioned; credentials remain in root-only file.")


if __name__ == "__main__":
    try:
        main()
    except Exception:
        print("Application account provisioning failed; inspect root-only pending file privately.")
        sys.exit(1)
