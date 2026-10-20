#!/usr/bin/env python3
"""After an intentional DB restore: revoke sessions and invalidate sync cursors.

Run with the gateway stopped and PocketBase running. This does not restore data.
"""
import json
import os
from pathlib import Path
import subprocess
import sys
import urllib.request


def main():
    if os.geteuid() != 0:
        raise RuntimeError("root_required")
    if subprocess.run(["systemctl", "is-active", "--quiet", "goutou-gateway"]).returncode == 0:
        raise RuntimeError("stop_gateway_first")
    environment = dict(line.split("=", 1) for line in Path("/etc/goutou/pocketbase.env").read_text().splitlines() if "=" in line)
    request = urllib.request.Request("http://127.0.0.1:8090/internal/cloud/reset-epoch",
        data=b"{}", headers={"Content-Type": "application/json",
        "X-Internal-Key": environment["GOUTOU_PB_INTERNAL_KEY"]})
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    with opener.open(request, timeout=30) as response:
        data = json.load(response)
    if not data.get("server_epoch"):
        raise RuntimeError("epoch_reset_failed")
    print("Sync epoch rotated; all devices must log in again and review local recovery data.")


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        code = str(error) if str(error) in {"root_required", "stop_gateway_first"} else "epoch_reset_failed"
        print(code, file=sys.stderr)
        sys.exit(1)
