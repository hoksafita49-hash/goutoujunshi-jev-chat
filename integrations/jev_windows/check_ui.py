# -*- coding: utf-8 -*-
"""Headless construction smoke test for the packaged Qt views."""
import os
import json
from pathlib import Path
import tempfile
import time
from unittest.mock import patch

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

import main as windows_main  # smoke-import the actual Windows capture/fill orchestration
from app.overlay import Overlay
from app.review import ReviewDialog
from app.trend_ui import TrendWindow
from app import settings, cloud_runtime
from app.gateway import result_for_ui
from PySide6.QtWidgets import QApplication
from PySide6.QtGui import QFontDatabase


def main():
    overlay = Overlay(on_fill=lambda _text: None, result_of=lambda _title: None)
    overlay.open_settings()
    overlay._back_home()
    overlay.show({"candidates": ["好，周六见", "行，周六见"], "best_index": 0,
                  "scores": [.6, .4], "goutou": {"intent": "可能愿意继续聊",
                  "action": "先承接", "facts": ["对方说周六见"]}})
    overlay.set_chat("测试会话")
    overlay.log_message("her", "仅属于上一应用", chat="测试会话")
    overlay.reset_chat_state()
    assert not overlay.current_chat() and not overlay.feeds and not overlay.cands
    source = [("me", "在吗"), ("her", "在")]
    snapshot = {"rows": source, "revision": 1}
    review = ReviewDialog("测试会话", source, overlay.win,
                          history_of=lambda: snapshot["rows"],
                          revision_of=lambda: snapshot["revision"])
    assert review.editor.toPlainText().count("：") == 2
    review.editor.setPlainText("我：在吗\n对方：在的")
    snapshot.update(rows=source + [("her", "下午见")], revision=2)
    review._accumulate()
    assert review.reviewed_revision == 2
    assert review.editor.toPlainText() == "我：在吗\n对方：在的\n对方：下午见"
    review._confirm()
    assert review.messages == [("me", "在吗"), ("her", "在的"), ("her", "下午见")]
    trend = TrendWindow()
    assert trend.canvas.candles
    trend.close()
    review.close()
    overlay.win.close()
    print("windows ui ok")


def cloud_checks(directory):
    settings.save(cloud_mode_on=True, cloud_server_text="http://127.0.0.1:18000")
    cloud_runtime._service = cloud_runtime.CloudSession(settings.cloud_server(), directory / "cloud")
    with patch.object(settings, "_get_key", side_effect=AssertionError("Cloud mode read a vendor key")):
        overlay = Overlay(on_fill=lambda _text: None, result_of=lambda _title: None)
        overlay.open_settings()
        assert overlay.modelsSurface.isHidden() and overlay.ocrSurface.isHidden()
        assert overlay.cloudPanel.mode.currentIndex() == 1
        assert settings.ocr_provider() == "local"
        fixture = Path(__file__).resolve().parents[2] / "contracts/fixtures/cloud-v1-result.json"
        mapped = result_for_ui(json.loads(fixture.read_text(encoding="utf-8")))
        overlay.show(mapped)
        assert [card.text.text() for card in overlay.cards] == mapped["candidates"]
        assert mapped["scores"] == [None, None]
        assert hasattr(overlay, "evidenceButton"), "The result screen needs a usable evidence entry"
        page = json.loads((fixture.parent / "cloud-v1-sync-page.json").read_text(encoding="utf-8"))
        class FakeGateway:
            def __init__(self, _server, session=None):
                self.session = session
            def login(self, email, _password, _device):
                self.session = dict(token="synthetic-ui-token", user=dict(id="synthetic-ui-user", email=email),
                                    device=dict(id="synthetic-device"), device_secret="synthetic-secret",
                                    server_epoch=page["server_epoch"])
                return self.session
            def refresh(self):
                return self.session
            def request(self, method, path, body=None):
                if path == "/v1/config":
                    return dict(server_epoch=page["server_epoch"], api_version=1, min_client_version=1, sync_ready=True)
                if path.startswith("/v1/sync/pull"):
                    return page
                if path == "/v1/sync/push":
                    return dict(server_epoch=page["server_epoch"], results=[dict(op_id=op["op_id"], status="applied",
                        record=dict(entity_type=op["entity_type"], uid=op["entity_uid"], version=op["base_version"]+1,
                                    payload=op["payload"], created_at="2026-10-21T00:00:00Z", updated_at="2026-10-21T00:00:00Z", deleted_at=None))
                        for op in body["operations"]])
                if path == "/v1/auth/logout":
                    return {}
                if path.endswith("/evidence"):
                    return dict(analysis_id=mapped["analysis_id"], conversation_uid=mapped["conversation_uid"],
                        profile_uid=page["changes"][0]["record"]["uid"], context_revision=mapped["context_revision"],
                        messages=[dict(uid="synthetic-source", version=1, role="other", text="今天有点忙", observed_at="2026-10-21T00:01:00Z")],
                        evidence=[dict(claim_id="synthetic-claim", kind="fact", text="对方说今天有点忙", source_message_uids=["synthetic-source"],
                            source_quotes=[dict(message_uid="synthetic-source", quote="今天有点忙")], citation_status="verified")])
                raise AssertionError("Unexpected UI network call: " + path)
        def finish():
            deadline = time.monotonic() + 5
            while overlay.cloudPanel.busy and time.monotonic() < deadline:
                overlay.app.processEvents()
                time.sleep(.01)
            assert not overlay.cloudPanel.busy, "UI background callback did not arrive"
        with patch.object(cloud_runtime, "GatewayClient", FakeGateway):
            overlay.cloudPanel.email.setText("ui-test@example.test")
            overlay.cloudPanel.password.setText("synthetic-password")
            overlay.cloudPanel.login()
            finish()
            assert not overlay.cloudPanel.password.text()
            assert overlay.cloudPanel.people.count() == 1
            overlay.cloudPanel.name.setText("离线新建人物")
            overlay.cloudPanel.create_profile()
            assert overlay.cloudPanel.people.count() == 2
            overlay.cloudPanel.sync()
            finish()
            assert not cloud_runtime.current().require().db.operations()
            mapped.update(profile_uid=page["changes"][0]["record"]["uid"], cloud_identity=cloud_runtime.current().identity)
            overlay.show(mapped)
            assert not overlay.evidenceButton.isHidden()
            overlay.evidenceButton.click()
            evidence = overlay.cloudPanel.account_dialogs[-1]
            deadline = time.monotonic() + 5
            while evidence.busy and time.monotonic() < deadline:
                overlay.app.processEvents()
                time.sleep(.01)
            assert not evidence.busy
            assert "对方" in evidence.view.toPlainText() and "分析当时" in evidence.view.toPlainText()
            overlay.cloudPanel.memory_button.click()
            memories = overlay.cloudPanel.account_dialogs[-1]
            assert memories.isVisible() and memories.chooser.count() >= 1
        if os.environ.get("GOUTOU_UI_SNAPSHOT_DIR"):
            out = Path(os.environ["GOUTOU_UI_SNAPSHOT_DIR"])
            out.mkdir(parents=True, exist_ok=True)
            overlay.win.resize(450, 800)
            overlay.app.processEvents()
            overlay.win.grab().save(str(out / "windows-cloud-settings.png"))
            evidence.grab().save(str(out / "windows-cloud-evidence.png"))
            memories.grab().save(str(out / "windows-cloud-memories.png"))
        overlay.cloudPanel.logout()
        finish()
        assert not cloud_runtime.current().session and overlay.cloudPanel.people.count() == 0
        assert not evidence.isVisible() and not evidence.view.toPlainText() and not memories.isVisible()
        overlay.cloudPanel.timer.stop()
        overlay.win.close()
    cloud_runtime._service.close()
    cloud_runtime._service = None
    print("windows cloud ui ok")


if __name__ == "__main__":
    qt_app = QApplication.instance() or QApplication([])
    # Windows' offscreen platform has no system font database in a sandbox.
    # Load installed fonts for visual QA only; never copy them into the package.
    if not QFontDatabase.families():
        for name in ("msyh.ttc", "segoeui.ttf"):
            font = Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name
            if font.exists():
                QFontDatabase.addApplicationFont(str(font))
    with tempfile.TemporaryDirectory() as directory:
        with patch.object(settings, "_CONFIG", str(Path(directory) / "config.json")), patch.object(settings, "_get_key", return_value=""):
            main()
            cloud_checks(Path(directory))
