"""Account lifetime and protected sessions, separate from Qt and capture."""
import os
from pathlib import Path
import threading
import uuid
from .auth_store import SessionStore
from .gateway import CloudError, GatewayClient, validate_server
from .local_db import LocalDatabase
from .repositories import CloudRepository


class CloudSession:
    def __init__(self, server, root, *, vault=None):
        self.server, self.root = validate_server(server), Path(root)
        self.vault = vault or SessionStore(self.root / "sessions")
        self.session = self.vault.load(self.server)
        self.gateway = GatewayClient(self.server, self.session)
        self.repository = None
        self.identity = str(uuid.uuid4())
        self._databases = []
        self._lock = threading.RLock()
        if self.session:
            self._open_account()

    def _open_account(self):
        db = LocalDatabase(self.root / "accounts", self.server, self.session["user"]["id"])
        self._databases.append(db)
        self.repository = CloudRepository(db, self.gateway)

    @property
    def source_device(self):
        path = self.root / "installation-id"
        try:
            return str(uuid.UUID(path.read_text(encoding="ascii").strip()))
        except (OSError, ValueError):
            self.root.mkdir(parents=True, exist_ok=True)
            value = str(uuid.uuid4())
            try:
                with path.open("x", encoding="ascii") as handle:
                    handle.write(value)
                return value
            except FileExistsError:
                return str(uuid.UUID(path.read_text(encoding="ascii").strip()))

    def require(self):
        if not self.session or not self.repository:
            raise CloudError("login_required", 401)
        return self.repository

    def _invalid(self):
        self.session = self.gateway.session = None
        self.repository = None
        self.identity = str(uuid.uuid4())
        self.vault.clear(self.server)

    def _guard(self, fn):
        with self._lock:
            try:
                return fn()
            except CloudError as exc:
                if exc.status == 401:
                    self._invalid()
                raise
            except RuntimeError as exc:
                if "server_epoch_changed" in str(exc):
                    self._invalid()
                raise

    def login(self, email, password, device_name=None):
        with self._lock:
            gateway = GatewayClient(self.server)
            session = gateway.login(email.strip(), password, device_name or os.environ.get("COMPUTERNAME", "Windows"))
            self.vault.save(self.server, session)
            self.session, self.gateway = session, gateway
            self.identity = str(uuid.uuid4())
            self._open_account()
            self.repository.db.check_epoch(session["server_epoch"])
            return self.sync()

    def sync(self):
        def action():
            repo = self.require()
            self.gateway.refresh()
            self.vault.save(self.server, self.session)
            return repo.syncer.sync()
        return self._guard(action)

    def logout(self):
        with self._lock:
            try:
                if self.session:
                    self.gateway.request("POST", "/v1/auth/logout")
            finally:
                self._invalid()

    def recover_after_review(self):
        def action():
            repo = self.require()
            config = self.gateway.request("GET", "/v1/config")
            repo.db.adopt_epoch_after_review(config["server_epoch"])
            self.identity = str(uuid.uuid4())
            return repo.syncer.sync()
        return self._guard(action)

    def analyze(self, app, title, messages, preferences, *, expected_identity=None, source_messages=None, expected_context=None,
                selected_profile_uid=None, new_profile=None, current_intent=None):
        def action():
            if expected_identity is not None and self.identity != expected_identity:
                raise RuntimeError("account_changed：账号已变化，请重新核对原文")
            repo = self.require()
            if expected_context is not None and repo.review_context(app, title, self.source_device) != tuple(expected_context):
                raise RuntimeError("context_changed：人物关联已变化，请重新核对原文")
            return repo.analyze_confirmed(app, title, messages, self.source_device, preferences, source_messages=source_messages,
                                          selected_profile_uid=selected_profile_uid, new_profile=new_profile, current_intent=current_intent)
        return self._guard(action)

    def extra(self, result, kind, index=None):
        def action():
            repo = self.require()
            if result.get("cloud_identity") != self.identity:
                raise RuntimeError("account_changed")
            profile = repo.db.visible("profiles", result["profile_uid"])
            conv = repo.db.visible("conversations", result["conversation_uid"])
            if (not profile or profile.get("deleted_at") or not conv or conv.get("deleted_at") or
                    conv["payload"].get("context_revision") != result["context_revision"]):
                raise RuntimeError("context_mismatch：请刷新后重新分析")
            selected = result["candidates"][index] if index is not None else None
            return repo.run_job(result["conversation_uid"], result["context_revision"],
                                "rewrite" if kind == "rewrite" else "explanation",
                                result["analysis_id"], selected)
        return self._guard(action)

    def accept_result(self, result):
        if result.get("cloud_identity") != self.identity or not self.repository or self.repository.db.frozen:
            return False
        profile = self.repository.db.visible("profiles", result["profile_uid"])
        conv = self.repository.db.visible("conversations", result["conversation_uid"])
        return bool(profile and not profile.get("deleted_at") and conv and not conv.get("deleted_at") and
                    conv["payload"].get("context_revision") == result["context_revision"] and
                    not self.repository.db.has_pending_context(result["profile_uid"], result["conversation_uid"]))

    def _account_operation(self, expected_identity, operation):
        def action():
            identity = self.identity if expected_identity is None else expected_identity
            if identity != self.identity:
                raise RuntimeError("account_changed")
            repo = self.require()
            if repo.db.frozen:
                raise RuntimeError("server_epoch_changed")
            value = operation(repo)
            if identity != self.identity or repo is not self.repository or not self.session:
                raise RuntimeError("account_changed")
            return value
        return self._guard(action)

    def evidence(self, analysis_id, profile_uid, *, expected_identity, **context):
        return self._account_operation(expected_identity, lambda repo: repo.evidence(analysis_id, profile_uid, **context))

    def memory_history(self, uid, *, expected_identity, **page):
        return self._account_operation(expected_identity, lambda repo: repo.memory_history(uid, **page))

    def create_memory(self, profile_uid, text, *, expected_identity, **fields):
        return self._account_operation(expected_identity, lambda repo: repo.create_memory(profile_uid, text, **fields))

    def save_memory(self, uid, *, expected_identity, **fields):
        return self._account_operation(expected_identity, lambda repo: repo.save_memory(uid, **fields))

    def delete_memory(self, uid, *, expected_identity, **fields):
        return self._account_operation(expected_identity, lambda repo: repo.delete_memory(uid, **fields))

    def restore_memory(self, uid, revision, *, expected_identity):
        return self._account_operation(expected_identity, lambda repo: repo.restore_memory(uid, revision))

    def close(self):
        for db in self._databases:
            db.close()
        self._databases.clear()


_service = None
_service_lock = threading.Lock()


def current():
    global _service
    from . import settings
    with _service_lock:
        server = settings.cloud_server()
        if _service is None or _service.server != server:
            root = Path(os.environ.get("LOCALAPPDATA", settings._ROOT)) / "GoutouJunshi" / "cloud"
            _service = CloudSession(server, root)
        return _service
