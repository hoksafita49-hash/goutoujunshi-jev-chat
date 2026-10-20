"""Cloud business operations over a durable local repository."""
import threading
import time
import uuid
from .gateway import CloudError, result_for_ui
from .sync_worker import SyncCoordinator


class CloudRepository:
    def __init__(self, db, gateway):
        self.db, self.gateway = db, gateway
        self.syncer = SyncCoordinator(db, gateway)

    def bind_chat(self, app, title, profile_uid, source_device):
        if not title or not self.db.visible("profiles", profile_uid):
            raise ValueError("请先选择人物和当前会话")
        identity = self.db.stable_uid(f"binding:{source_device}:{app}:{title}")
        self.db.save("identity_links", identity, dict(profile_uid=profile_uid, platform=app,
                                                      legacy_id=title, source_device=source_device))
        conv = self.db.stable_uid(f"conversation:{source_device}:{app}:{title}:{profile_uid}")
        if not self.db.visible("conversations", conv):
            self.db.save("conversations", conv, dict(profile_uid=profile_uid, title=title, platform=app, context_revision=0))
        return conv

    def conversation_for(self, app, title, source_device):
        binding = self.db.visible("identity_links", self.db.stable_uid(f"binding:{source_device}:{app}:{title}"))
        links = [binding] if binding and not binding.get("deleted_at") else [r for r in self.db.list_records("identity_links") if
                 r["payload"].get("platform") == app and r["payload"].get("legacy_id") == title and
                 r["payload"].get("source_device") == source_device]
        if not links:
            raise ValueError("请在云账号设置中选择人物，并明确关联当前会话")
        if len({r["payload"]["profile_uid"] for r in links}) > 1:
            raise ValueError("当前会话存在多个人物关联，请在设置中明确重新关联")
        profile = links[-1]["payload"]["profile_uid"]
        live = self.db.visible("profiles", profile)
        if not live or live.get("deleted_at"):
            raise ValueError("关联人物已删除，请重新确认人物")
        conv = self.db.stable_uid(f"conversation:{source_device}:{app}:{title}:{profile}")
        if not self.db.visible("conversations", conv):
            self.db.save("conversations", conv, dict(profile_uid=profile, title=title, platform=app, context_revision=0))
        return profile, conv

    def analyze_confirmed(self, app, title, messages, source_device, preferences=None, source_messages=None):
        profile, conv = self.conversation_for(app, title, source_device)
        self.db.confirm_messages(conv, profile, messages, source_messages=source_messages)
        self.syncer.sync()
        if self.db.has_pending_context(profile, conv):
            raise RuntimeError("当前人物或聊天有未同步/冲突内容，请先处理后再分析")
        record = self.db.visible("conversations", conv)
        if not record or record.get("deleted_at"):
            raise RuntimeError("conversation_deleted")
        revision = record["payload"]["context_revision"]
        result = self.run_job(conv, revision, preferences=preferences)
        mapped = result_for_ui(result)
        mapped.update(profile_uid=profile, reviewed_messages=messages)
        return mapped

    def run_job(self, conversation_uid, revision, kind="analysis", parent=None, selected_text=None, preferences=None, *, new_request=False):
        pending = None
        if not new_request and preferences is None and parent is None and selected_text is None:
            pending = self.db.latest_job(conversation_uid, revision, kind)
        pending = pending or self.db.prepare_job(conversation_uid, revision, kind, parent, selected_text, preferences, new_request=new_request)
        job = pending["job"]
        if not job.get("analysis_id"):
            # request_id is already on disk. Ambiguity leaves that exact request available.
            job = self.gateway.request("POST", "/v1/analyses", pending["body"])
            self.db.update_job(pending["request_id"], job)
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            job = self.gateway.request("GET", "/v1/analyses/" + job["analysis_id"])
            self.db.update_job(pending["request_id"], job)
            if job.get("conversation_uid") != conversation_uid or job.get("context_revision") != revision:
                raise RuntimeError("context_mismatch")
            if job["status"] == "succeeded":
                result = job["result"]
                if (result.get("conversation_uid") != conversation_uid or result.get("context_revision") != revision
                        or result.get("analysis_id") != job["analysis_id"]):
                    raise RuntimeError("context_mismatch")
                return result
            if job["status"] in ("failed", "indeterminate", "cancelled"):
                raise CloudError((job.get("error") or {}).get("code") or job["status"])
            threading.Event().wait(1)
        raise RuntimeError("任务仍在服务端运行；已保存任务 ID，稍后使用同一上下文继续查询")
