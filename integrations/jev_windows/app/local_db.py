"""Account-isolated SQLite mirror, dirty overlay, immutable outbox and job ledger."""
from __future__ import annotations
import hashlib
from datetime import datetime, timezone
from difflib import SequenceMatcher
import json
from pathlib import Path
import sqlite3
import threading
import time
import uuid


def encode(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))


class LocalDatabase:
    def __init__(self, root, server, account):
        self.path = Path(root) / (hashlib.sha256((server + "\n" + account).encode()).hexdigest() + ".sqlite3")
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.lock = threading.RLock()
        self.conn = sqlite3.connect(self.path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript("""
        PRAGMA journal_mode=WAL;
        CREATE TABLE IF NOT EXISTS mirror(kind TEXT,uid TEXT,record TEXT,PRIMARY KEY(kind,uid));
        CREATE TABLE IF NOT EXISTS edits(kind TEXT,uid TEXT,payload TEXT,action TEXT,PRIMARY KEY(kind,uid));
        CREATE TABLE IF NOT EXISTS outbox(id TEXT PRIMARY KEY,kind TEXT,uid TEXT,body TEXT,status TEXT);
        CREATE TABLE IF NOT EXISTS conflicts(id TEXT PRIMARY KEY,kind TEXT,uid TEXT,local TEXT,remote TEXT);
        CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY,value TEXT);
        CREATE TABLE IF NOT EXISTS legacy(key TEXT PRIMARY KEY,uid TEXT);
        CREATE TABLE IF NOT EXISTS jobs(request_id TEXT PRIMARY KEY,fingerprint TEXT,body TEXT,job TEXT);
        """)
        # Upgrade caches that consumed a tombstone before task-copy cleanup existed;
        # the incremental cursor will not deliver those deletion events again.
        with self.lock, self.conn:
            for row in self.conn.execute("SELECT record FROM mirror WHERE kind='analyses'").fetchall():
                record = json.loads(row["record"])
                if record.get("deleted_at"):
                    self._mirror(record)

    def close(self):
        with self.lock:
            self.conn.close()

    def _meta(self, key, default=""):
        row = self.conn.execute("SELECT value FROM meta WHERE key=?", (key,)).fetchone()
        return row[0] if row else default

    def _set_meta(self, key, value):
        self.conn.execute("INSERT OR REPLACE INTO meta VALUES (?,?)", (key, value))

    @property
    def cursor(self):
        with self.lock:
            return self._meta("cursor")

    @property
    def epoch(self):
        with self.lock:
            return self._meta("epoch")

    @property
    def frozen(self):
        with self.lock:
            return self._meta("frozen") == "1"

    def check_epoch(self, epoch):
        with self.lock:
            old = self.epoch
            if old and old != epoch:
                if not self.frozen:
                    recovery = sqlite3.connect(self.path.with_suffix(f".recovery-{time.time_ns()}.sqlite3"))
                    try:
                        self.conn.backup(recovery)
                    finally:
                        recovery.close()
                    with self.conn:
                        self._set_meta("frozen", "1")
                raise RuntimeError("server_epoch_changed：已保留恢复副本，请重新登录并处理备份差异")
            with self.conn:
                self._set_meta("epoch", epoch)

    def adopt_epoch_after_review(self, epoch):
        """Only called after the user explicitly accepts starting from the restored server."""
        with self.lock, self.conn:
            if not self.frozen:
                raise ValueError("没有待处理的服务器恢复")
            for table in ("mirror", "edits", "outbox", "conflicts", "jobs", "meta"):
                self.conn.execute("DELETE FROM " + table)
            self._set_meta("epoch", epoch)
            self._set_meta("frozen", "0")

    def _remote(self, kind, uid):
        row = self.conn.execute("SELECT record FROM mirror WHERE kind=? AND uid=?", (kind, uid)).fetchone()
        return json.loads(row[0]) if row else None

    def _mirror(self, record):
        old = self._remote(record["entity_type"], record["uid"])
        if record["entity_type"] == "analyses" and (record.get("deleted_at") or (old or {}).get("deleted_at")):
            # Analysis deletion is terminal. Historical pages and late job receipts must
            # not expose a second copy through the durable task/recovery ledger.
            tombstone = record if record.get("deleted_at") else old
            record = {**tombstone, "version": max(record["version"], (old or {}).get("version", 0)), "payload": {}}
            for row in self.conn.execute("SELECT request_id,body,job FROM jobs").fetchall():
                job = json.loads(row["job"])
                body = json.loads(row["body"])
                if (job.get("analysis_id") == record["uid"] or (job.get("result") or {}).get("analysis_id") == record["uid"] or
                        body.get("parent_analysis_id") == record["uid"]):
                    body = self._deleted_job_body(body)
                    cancelled = self._deleted_job(job.get("analysis_id") or (job.get("result") or {}).get("analysis_id"), body)
                    self.conn.execute("UPDATE jobs SET body=?,job=? WHERE request_id=?", (encode(body), encode(cancelled), row["request_id"]))
        if not old or record["version"] >= old["version"]:
            self.conn.execute("INSERT OR REPLACE INTO mirror VALUES (?,?,?)",
                              (record["entity_type"], record["uid"], encode(record)))

    def visible(self, kind, uid):
        with self.lock:
            remote = self._remote(kind, uid)
            edit = self.conn.execute("SELECT * FROM edits WHERE kind=? AND uid=?", (kind, uid)).fetchone()
            if edit:
                return {**(remote or dict(entity_type=kind, uid=uid, version=0)),
                        "payload": json.loads(edit["payload"]), "deleted_at": "local" if edit["action"] == "delete" else None,
                        "dirty": True}
            return remote

    def list_records(self, kind, *, deleted=False):
        with self.lock:
            ids = self.conn.execute("SELECT uid FROM mirror WHERE kind=? UNION SELECT uid FROM edits WHERE kind=?", (kind, kind)).fetchall()
            rows = [self.visible(kind, row[0]) for row in ids]
            return [row for row in rows if deleted or not row.get("deleted_at")]

    def _queue(self, kind, uid, payload, action, replace_id=None, base_version=None):
        remote = self._remote(kind, uid)
        op = dict(op_id=str(uuid.uuid4()), entity_type=kind, entity_uid=uid,
                  base_version=(remote["version"] if remote else 0) if base_version is None else base_version,
                  action=action, payload=payload)
        if replace_id:
            # Keep dependency order while replacing a never-sent/rejected receipt ID.
            self.conn.execute("UPDATE outbox SET id=?,body=?,status='ready' WHERE id=?",
                              (op["op_id"], encode(op), replace_id))
        else:
            self.conn.execute("INSERT INTO outbox VALUES (?,?,?,?,?)", (op["op_id"], kind, uid, encode(op), "ready"))

    def save(self, kind, uid, payload, action="upsert", *, expected_version=None):
        if kind == "analyses":
            raise ValueError("分析结果由服务端写入")
        with self.lock, self.conn:
            if self.frozen:
                raise RuntimeError("server_epoch_changed：恢复副本待处理")
            remote = self._remote(kind, uid)
            if remote and remote.get("deleted_at"):
                raise ValueError("已删除的记录不能恢复，请另建记录")
            attempted = self.conn.execute("SELECT 1 FROM outbox WHERE kind=? AND uid=? AND status IN ('inflight','conflict')", (kind, uid)).fetchone()
            if action == "delete" and not remote and not attempted:
                self.conn.execute("DELETE FROM outbox WHERE kind=? AND uid=?", (kind, uid))
                self.conn.execute("DELETE FROM edits WHERE kind=? AND uid=?", (kind, uid))
                return
            self.conn.execute("INSERT OR REPLACE INTO edits VALUES (?,?,?,?)", (kind, uid, encode(payload), action))
            # A never-sent edit can be coalesced. An attempted op is immutable forever.
            replace = self.conn.execute("SELECT id FROM outbox WHERE kind=? AND uid=? AND status IN ('ready','rejected')", (kind, uid)).fetchone()
            if replace:
                self._queue(kind, uid, payload, action, replace[0], expected_version)
            elif not self.conn.execute("SELECT 1 FROM outbox WHERE kind=? AND uid=?", (kind, uid)).fetchone():
                self._queue(kind, uid, payload, action, base_version=expected_version)
            if expected_version is not None and remote and remote["version"] > expected_version:
                for row in self.conn.execute("SELECT body FROM outbox WHERE kind=? AND uid=?", (kind, uid)).fetchall():
                    self._conflict(json.loads(row[0]), remote)

    def operations(self, limit=50):
        with self.lock:
            return [json.loads(row[0]) for row in self.conn.execute(
                "SELECT body FROM outbox WHERE status IN ('ready','inflight') ORDER BY rowid LIMIT ?", (limit,))]

    def mark_attempted(self, ids):
        with self.lock, self.conn:
            self.conn.executemany("UPDATE outbox SET status='inflight' WHERE id=? AND status='ready'", [(i,) for i in ids])

    def _conflict(self, op, record):
        edit = self.conn.execute("SELECT payload FROM edits WHERE kind=? AND uid=?", (op["entity_type"], op["entity_uid"])).fetchone()
        self.conn.execute("INSERT OR REPLACE INTO conflicts VALUES (?,?,?,?,?)",
                          (op["op_id"], op["entity_type"], op["entity_uid"], edit[0] if edit else encode(op["payload"]), encode(record)))
        self.conn.execute("UPDATE outbox SET status='conflict' WHERE id=?", (op["op_id"],))

    def apply_push(self, results):
        with self.lock, self.conn:
            for result in results:
                row = self.conn.execute("SELECT body FROM outbox WHERE id=?", (result["op_id"],)).fetchone()
                if not row:
                    continue
                op = json.loads(row[0])
                record = result.get("record")
                if record:
                    self._mirror(record)
                if result["status"] == "applied":
                    self.conn.execute("DELETE FROM outbox WHERE id=?", (op["op_id"],))
                    edit = self.conn.execute("SELECT * FROM edits WHERE kind=? AND uid=?", (op["entity_type"], op["entity_uid"])).fetchone()
                    if edit and edit["payload"] == encode(op["payload"]) and edit["action"] == op["action"]:
                        self.conn.execute("DELETE FROM edits WHERE kind=? AND uid=?", (op["entity_type"], op["entity_uid"]))
                    elif edit:
                        self._queue(op["entity_type"], op["entity_uid"], json.loads(edit["payload"]), edit["action"])
                elif result["status"] == "conflict" and record:
                    self._conflict(op, record)
                else:
                    self.conn.execute("UPDATE outbox SET status='rejected' WHERE id=?", (op["op_id"],))

    def apply_pull(self, changes, cursor):
        with self.lock, self.conn:
            for change in changes:
                rec = change["record"]
                previous = self._remote(rec["entity_type"], rec["uid"])
                self._mirror(rec)
                for row in self.conn.execute("SELECT body FROM outbox WHERE kind=? AND uid=?", (rec["entity_type"], rec["uid"])).fetchall():
                    op = json.loads(row[0])
                    # A pull scan may trail a push receipt. Historical versions are not
                    # concurrent writes and cannot downgrade a conflict's remote copy.
                    if (rec["version"] > op["base_version"] and
                            (not previous or rec["version"] >= previous["version"])):
                        self._conflict(op, rec)
            self._set_meta("cursor", cursor)

    def conflicts(self):
        with self.lock:
            return [dict(id=r["id"], kind=r["kind"], uid=r["uid"], local=json.loads(r["local"]), remote=json.loads(r["remote"]))
                    for r in self.conn.execute("SELECT * FROM conflicts ORDER BY rowid")]

    def resolve(self, conflict_id, choice):
        with self.lock, self.conn:
            row = self.conn.execute("SELECT * FROM conflicts WHERE id=?", (conflict_id,)).fetchone()
            if not row:
                return
            remote = json.loads(row["remote"])
            if choice not in ("local", "remote") or (choice == "local" and remote.get("deleted_at")):
                raise ValueError("远端已删除；只能接受删除，另建人物后再整理本地副本")
            edit = self.conn.execute("SELECT * FROM edits WHERE kind=? AND uid=?", (row["kind"], row["uid"])).fetchone()
            self.conn.execute("DELETE FROM outbox WHERE kind=? AND uid=?", (row["kind"], row["uid"]))
            self.conn.execute("DELETE FROM conflicts WHERE kind=? AND uid=?", (row["kind"], row["uid"]))
            if choice == "remote":
                self.conn.execute("DELETE FROM edits WHERE kind=? AND uid=?", (row["kind"], row["uid"]))
            elif edit:
                self._queue(row["kind"], row["uid"], json.loads(edit["payload"]), edit["action"])

    def stable_uid(self, source):
        with self.lock, self.conn:
            row = self.conn.execute("SELECT uid FROM legacy WHERE key=?", (source,)).fetchone()
            if row:
                return row[0]
            value = str(uuid.uuid5(uuid.NAMESPACE_URL, self.path.stem + ":" + source))
            self.conn.execute("INSERT INTO legacy VALUES (?,?)", (source, value))
            return value

    def confirm_messages(self, conversation_uid, profile_uid, messages, source_messages=None):
        """Sequence alignment preserves repeated messages and rolling capture windows."""
        with self.lock:
            previous = json.loads(self._meta("confirmed:" + conversation_uid, "[]"))
            old_rows = [(r["role"], r["text"]) for r in previous]
            new_rows = [("me" if r[0] == "me" else "other", str(r[1])) for r in messages]
            mapping, removed = {}, []
            source_present = set()
            if source_messages is not None:
                source_rows = [("me" if r[0] == "me" else "other", str(r[1])) for r in source_messages]
                for tag, a, b, c, d in SequenceMatcher(a=old_rows, b=source_rows, autojunk=False).get_opcodes():
                    if tag == "equal":
                        source_present.update(range(a, b))
                    elif tag == "replace":
                        source_present.update(range(a, a + min(b - a, d - c)))
            matcher = SequenceMatcher(a=old_rows, b=new_rows, autojunk=False)
            for tag, a, b, c, d in matcher.get_opcodes():
                if tag == "equal":
                    mapping.update({c + k: previous[a + k]["uid"] for k in range(b - a)})
                elif tag == "replace":
                    mapping.update({c + k: previous[a + k]["uid"] for k in range(min(b - a, d - c))})
                    removed.extend(previous[a + min(b - a, d - c):b])
                elif tag == "delete":
                    # Missing prefix/suffix can be an offscreen portion of the 60-row capture.
                    removed.extend(previous[k] for k in range(a, b) if k in source_present or (a > 0 and b < len(previous)))
            now = datetime.now(timezone.utc)
            snapshot = []
            orders = []
            for index in range(len(new_rows)):
                old = self.visible("messages", mapping[index]) if index in mapping else None
                key = (old or {}).get("payload", {}).get("order_key")
                orders.append(int(key) if key else None)
            start = 0
            while start < len(orders):
                if orders[start] is not None:
                    start += 1
                    continue
                end = start
                while end < len(orders) and orders[end] is None:
                    end += 1
                left = orders[start - 1] if start else 10 ** 30
                right = orders[end] if end < len(orders) else left + (end - start + 1) * 10 ** 20
                if end < len(orders) and not start:
                    left = max(0, right - (end - start + 1) * 10 ** 20)
                step = (right - left) // (end - start + 1)
                if step < 1:
                    raise ValueError("此处回溯插入次数较多，请新建会话整理顺序")
                for offset in range(end - start):
                    orders[start + offset] = left + step * (offset + 1)
                start = end
            for index, (role, text) in enumerate(new_rows):
                uid = mapping.get(index) or str(uuid.uuid5(uuid.UUID(conversation_uid) if len(conversation_uid) == 36 else uuid.NAMESPACE_URL,
                    encode([conversation_uid, previous, new_rows, index])))
                old = self.visible("messages", uid)
                payload = dict(conversation_uid=conversation_uid, profile_uid=profile_uid, role=role,
                               text=text, confirmed=True, order_key=f"{orders[index]:040d}", observed_at=(old or {}).get("payload", {}).get(
                                   "observed_at", now.isoformat().replace("+00:00", "Z")))
                if not old or old["payload"] != payload:
                    self.save("messages", uid, payload)
                snapshot.append(dict(uid=uid, role=role, text=text))
            for removed_row in removed:
                old = self.visible("messages", removed_row["uid"])
                if old and not old.get("deleted_at"):
                    self.save("messages", removed_row["uid"], old["payload"], "delete")
            with self.conn:
                self._set_meta("confirmed:" + conversation_uid, encode(snapshot))

    def prepare_job(self, conversation_uid, revision, kind, parent=None, selected_text=None, preferences=None, *, new_request=False):
        body = dict(conversation_uid=conversation_uid, context_revision=revision, kind=kind)
        if parent:
            body["parent_analysis_id"] = parent
        if selected_text:
            body["selected_text"] = selected_text
        if preferences:
            body["preferences"] = preferences
        fingerprint = hashlib.sha256(encode(body).encode()).hexdigest()
        with self.lock, self.conn:
            row = self.conn.execute("SELECT * FROM jobs WHERE fingerprint=? ORDER BY rowid DESC LIMIT 1", (fingerprint,)).fetchone()
            if row and not new_request:
                return dict(request_id=row["request_id"], body=json.loads(row["body"]), job=json.loads(row["job"]))
            request_id = str(uuid.uuid4())
            body["request_id"] = request_id
            self.conn.execute("INSERT INTO jobs VALUES (?,?,?,?)", (request_id, fingerprint, encode(body), "{}"))
            return dict(request_id=request_id, body=body, job={})

    def update_job(self, request_id, job):
        with self.lock, self.conn:
            row = self.conn.execute("SELECT body,job FROM jobs WHERE request_id=?", (request_id,)).fetchone()
            if not row:
                return job
            previous = json.loads(row["job"])
            previously_deleted = (previous.get("error") or {}).get("code") == "analysis_deleted"
            uid = previous.get("analysis_id") if previously_deleted else (job.get("analysis_id") or previous.get("analysis_id"))
            analysis = self._remote("analyses", uid) if uid else None
            body = json.loads(row["body"])
            parent = self._remote("analyses", body["parent_analysis_id"]) if body.get("parent_analysis_id") else None
            if previously_deleted or (analysis and analysis.get("deleted_at")) or (parent and parent.get("deleted_at")):
                body = self._deleted_job_body(body)
                job = self._deleted_job(uid or job.get("analysis_id"), body)
            self.conn.execute("UPDATE jobs SET body=?,job=? WHERE request_id=?", (encode(body), encode(job), request_id))
            return job

    @staticmethod
    def _deleted_job_body(body):
        # Keep the existing SHA256 fingerprint and IDs as a terminal replay ledger,
        # but remove copies of selected replies and private preference overrides.
        return {key: body[key] for key in ("request_id", "conversation_uid", "context_revision", "kind", "parent_analysis_id") if key in body}

    @staticmethod
    def _deleted_job(analysis_id, body):
        return dict(analysis_id=analysis_id, status="cancelled", result=None,
                    conversation_uid=body["conversation_uid"], context_revision=body["context_revision"],
                    error=dict(code="analysis_deleted"))

    def latest_job(self, conversation_uid, revision, kind):
        with self.lock:
            for row in self.conn.execute("SELECT * FROM jobs ORDER BY rowid DESC"):
                body = json.loads(row["body"])
                if (body.get("conversation_uid") == conversation_uid and body.get("context_revision") == revision
                        and body.get("kind") == kind):
                    return dict(request_id=row["request_id"], body=body, job=json.loads(row["job"]))
        return None

    def has_pending_context(self, profile, conversation):
        with self.lock:
            for row in self.conn.execute("SELECT kind,uid,payload FROM edits"):
                payload = json.loads(row["payload"])
                if ((row["kind"] == "profiles" and row["uid"] == profile) or
                        (row["kind"] == "conversations" and row["uid"] == conversation) or
                        payload.get("conversation_uid") == conversation or payload.get("profile_uid") == profile):
                    return True
            return False

    def status_counts(self):
        with self.lock:
            return {r[0]: r[1] for r in self.conn.execute("SELECT status,COUNT(*) FROM outbox GROUP BY status")}

    def retry_rejected(self):
        """Explicit user action after correcting a rejected draft/dependency."""
        count = 0
        with self.lock, self.conn:
            if self.frozen:
                raise RuntimeError("server_epoch_changed：先处理恢复副本")
            for row in self.conn.execute("SELECT * FROM outbox WHERE status='rejected' ORDER BY rowid").fetchall():
                original = json.loads(row["body"])
                remote = self._remote(row["kind"], row["uid"])
                if remote and remote.get("deleted_at"):
                    self._conflict(original, remote)
                    continue
                edit = self.conn.execute("SELECT * FROM edits WHERE kind=? AND uid=?", (row["kind"], row["uid"])).fetchone()
                if edit:
                    self._queue(row["kind"], row["uid"], json.loads(edit["payload"]), edit["action"], row["id"])
                    count += 1
        return count
