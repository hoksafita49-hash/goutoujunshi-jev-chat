"""Cloud business operations over a durable local repository."""
import threading
import time
import uuid
import urllib.parse
from .gateway import CloudError, evidence_for_ui, result_for_ui
from .sync_worker import SyncCoordinator


class CloudRepository:
    def __init__(self, db, gateway):
        self.db, self.gateway = db, gateway
        self.syncer = SyncCoordinator(db, gateway)

    def preferences(self):
        row = self.db.visible("preferences", "00000000-0000-4000-8000-000000000001")
        return {**dict(tone="natural", length="short", count=3, style=""),
                **((row or {}).get("payload", {}) if not (row or {}).get("deleted_at") else {})}

    def _live(self, kind, uid):
        row = self.db.visible(kind, uid)
        if not row or row.get("deleted_at"):
            raise ValueError(kind + "_deleted：记录不存在或已删除")
        return row

    def _memory_payload(self, profile_uid, text, kind, status, source_uids, change_reason):
        self._live("profiles", profile_uid)
        if not isinstance(text, str) or not text.strip() or len(text) > 4000:
            raise ValueError("memory_text：记忆内容需要1至4000字")
        if not isinstance(change_reason, str) or not change_reason.strip() or len(change_reason) > 500:
            raise ValueError("change_reason：请填写不超过500字的修改原因")
        if kind not in ("fact", "hypothesis") or status not in ("pending", "confirmed"):
            raise ValueError("invalid_memory_kind_status")
        if kind == "hypothesis" and status == "confirmed":
            raise ValueError("hypothesis：推测需要先明确转为事实，再单独确认")
        if not isinstance(source_uids, (list, tuple)) or any(not isinstance(uid, str) or not uid for uid in source_uids):
            raise ValueError("invalid_source：请重新核对消息来源")
        sources = list(dict.fromkeys(source_uids))
        if len(sources) > 16:
            raise ValueError("source_limit：最多选择16条来源")
        for uid in sources:
            row = self.db.visible("messages", uid)
            if (not row or row.get("deleted_at") or row["payload"].get("profile_uid") != profile_uid or
                    row["payload"].get("confirmed") is not True):
                raise ValueError("invalid_source：来源已删除、未经核对或不属于当前人物；请同步后重试")
        # source_refs are exclusively server generated. Never send historical versions as proof.
        return dict(profile_uid=profile_uid, text=text.strip(), kind=kind, status=status,
                    source_uids=sources, source_type="messages" if sources else "user", change_reason=change_reason.strip())

    def create_memory(self, profile_uid, text, *, kind="fact", source_uids=(), change_reason="手动创建"):
        payload = self._memory_payload(profile_uid, text, kind, "pending", source_uids, change_reason)
        uid = str(uuid.uuid4())
        self.db.save("memories", uid, payload)
        return uid

    def save_memory(self, uid, *, text=None, kind=None, status=None, change_reason, expected_version):
        row = self._live("memories", uid)
        if status == "confirmed" and (row.get("dirty") or row["version"] <= 0):
            raise ValueError("sync_required：请先同步新增或更正，再单独确认事实")
        old = row["payload"]
        next_kind = old["kind"] if kind is None else kind
        if status == "confirmed" and old["kind"] != "fact" and next_kind == "fact":
            raise ValueError("separate_confirmation：先保存转换为事实，再单独确认")
        # All corrections/conversions return to pending unless a distinct confirmation was requested.
        changed = (text is not None and text.strip() != old["text"]) or next_kind != old["kind"]
        payload = self._memory_payload(old["profile_uid"], old["text"] if text is None else text,
                                       next_kind, "pending" if changed else (status or "pending"), old.get("source_uids", []), change_reason)
        self.db.save("memories", uid, payload, expected_version=expected_version)
        return self.db.visible("memories", uid)

    def delete_memory(self, uid, *, confirmed=False, expected_version):
        if confirmed is not True:
            raise ValueError("delete_confirmation：删除前需要明确确认")
        row = self._live("memories", uid)
        self.db.save("memories", uid, row["payload"], "delete", expected_version=expected_version)

    def restore_memory(self, uid, revision):
        current = self._live("memories", uid)
        old = revision.get("payload", {})
        if (revision.get("entity_type") != "memories" or revision.get("uid") != uid or revision.get("deleted_at") or
                old.get("profile_uid") != current["payload"]["profile_uid"]):
            raise ValueError("history_profile_mismatch")
        version = revision.get("version")
        if not isinstance(version, int) or isinstance(version, bool) or version < 1:
            raise ValueError("invalid_history_version")
        payload = self._memory_payload(old["profile_uid"], old.get("text"), old.get("kind"), "pending",
                                       old.get("source_uids", []), "恢复版本 " + str(version))
        self.db.save("memories", uid, payload, expected_version=current["version"])
        return self.db.visible("memories", uid)

    def memory_history(self, uid, *, before_version=0, limit=50):
        row = self._live("memories", uid)
        if (not isinstance(before_version, int) or isinstance(before_version, bool) or before_version < 0 or
                not isinstance(limit, int) or isinstance(limit, bool) or not 1 <= limit <= 50):
            raise ValueError("invalid_history_page")
        query = urllib.parse.urlencode(dict(before_version=before_version, limit=limit))
        page = self.gateway.request("GET", "/v1/memories/" + urllib.parse.quote(uid, safe="") + "/history?" + query)
        if page.get("memory_uid") != uid or page.get("profile_uid") != row["payload"]["profile_uid"]:
            raise RuntimeError("context_mismatch")
        versions = page.get("versions")
        if not isinstance(versions, list) or any(
                rev.get("entity_type") != "memories" or rev.get("uid") != uid or rev.get("deleted_at") or
                rev.get("payload", {}).get("profile_uid") != row["payload"]["profile_uid"] for rev in versions):
            raise RuntimeError("context_mismatch")
        return page

    def evidence(self, analysis_id, profile_uid, *, conversation_uid=None, context_revision=None):
        self._live("profiles", profile_uid)
        view = self.gateway.request("GET", "/v1/analyses/" + urllib.parse.quote(analysis_id, safe="") + "/evidence")
        if (view.get("analysis_id") != analysis_id or view.get("profile_uid") != profile_uid or
                (conversation_uid is not None and view.get("conversation_uid") != conversation_uid) or
                (context_revision is not None and view.get("context_revision") != context_revision)):
            raise RuntimeError("context_mismatch")
        return {**view, "evidence": evidence_for_ui(view)}

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
        pending = pending or self.db.prepare_job(conversation_uid, revision, kind, parent, selected_text,
                                                 {**self.preferences(), **(preferences or {})}, new_request=new_request)
        job = pending["job"]
        if (job.get("error") or {}).get("code") == "analysis_deleted":
            raise CloudError("analysis_deleted")
        if not job.get("analysis_id"):
            # request_id is already on disk. Ambiguity leaves that exact request available.
            job = self.gateway.request("POST", "/v1/analyses", pending["body"])
            job = self.db.update_job(pending["request_id"], job)
            if (job.get("error") or {}).get("code") == "analysis_deleted":
                raise CloudError("analysis_deleted")
        deadline = time.monotonic() + 180
        while time.monotonic() < deadline:
            job = self.gateway.request("GET", "/v1/analyses/" + job["analysis_id"])
            job = self.db.update_job(pending["request_id"], job)
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
