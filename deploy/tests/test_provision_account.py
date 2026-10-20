"""Provisioning survives interrupted delivery without rotating the owner's password."""
import importlib.util
import io
import json
from pathlib import Path
import tempfile
from unittest.mock import patch

import pytest

SPEC = importlib.util.spec_from_file_location("provision_account", Path(__file__).parents[1] / "provision-account.py")
MODULE = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(MODULE)


def test_pending_credentials_are_reused_after_network_failure():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "pocketbase.env").write_text("GOUTOU_PB_INTERNAL_KEY=fixture-internal-key\n")
        requests = []
        class Opener:
            def open(self, request, timeout):
                requests.append(json.loads(request.data))
                if len(requests) == 1:
                    raise OSError("offline")
                return io.BytesIO(b'{"id":"fixture-owner"}')
        with patch.object(MODULE, "Path", side_effect=lambda value: root / Path(value).name), \
                patch.object(MODULE.os, "geteuid", return_value=0, create=True), \
                patch.object(MODULE.urllib.request, "build_opener", return_value=Opener()):
            with pytest.raises(OSError):
                MODULE.main()
            pending = json.loads((root / "application-user.json.pending").read_text())
            assert not (root / "application-user.json").exists()
            MODULE.main()
            assert requests == [pending, pending]
            assert json.loads((root / "application-user.json").read_text()) == pending
            assert not (root / "application-user.json.pending").exists()
            MODULE.main()
            assert len(requests) == 2


def test_missing_provision_ack_keeps_pending_file():
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        (root / "pocketbase.env").write_text("GOUTOU_PB_INTERNAL_KEY=fixture-internal-key\n")
        class Opener:
            def open(self, request, timeout):
                return io.BytesIO(b'{}')
        with patch.object(MODULE, "Path", side_effect=lambda value: root / Path(value).name), \
                patch.object(MODULE.os, "geteuid", return_value=0, create=True), \
                patch.object(MODULE.urllib.request, "build_opener", return_value=Opener()):
            with pytest.raises(RuntimeError, match="provision_failed"):
                MODULE.main()
            assert (root / "application-user.json.pending").exists()
            assert not (root / "application-user.json").exists()
