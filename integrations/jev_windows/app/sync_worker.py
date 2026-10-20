"""One foreground sync at a time. Receipts never become pull cursors."""
import threading
import urllib.parse


class SyncCoordinator:
    def __init__(self, db, gateway):
        self.db, self.gateway = db, gateway
        self.lock = threading.Lock()

    def sync(self):
        with self.lock:
            config = self.gateway.request("GET", "/v1/config")
            if config.get("api_version", 1) != 1 or config.get("min_client_version", 1) > 1:
                raise RuntimeError("client_upgrade_required")
            if config.get("sync_ready") is False:
                raise RuntimeError("sync_not_ready")
            self.db.check_epoch(config["server_epoch"])
            if self.db.frozen:
                raise RuntimeError("server_epoch_changed：请处理恢复副本")
            for _ in range(200):
                ops = self.db.operations(50)
                if not ops:
                    break
                self.db.mark_attempted([op["op_id"] for op in ops])
                result = self.gateway.request("POST", "/v1/sync/push", dict(server_epoch=self.db.epoch, operations=ops))
                self.db.check_epoch(result["server_epoch"])
                receipts = result["results"]
                if {r["op_id"] for r in receipts} != {o["op_id"] for o in ops}:
                    raise RuntimeError("invalid_push_receipts")
                self.db.apply_push(receipts)
            else:
                raise RuntimeError("同步队列较长，请继续同步")
            for _ in range(1000):
                query = urllib.parse.urlencode(dict(cursor=self.db.cursor, limit=200))
                result = self.gateway.request("GET", "/v1/sync/pull?" + query)
                self.db.check_epoch(result["server_epoch"])
                if result.get("has_more") and result["cursor"] == self.db.cursor:
                    raise RuntimeError("invalid_pull_cursor")
                self.db.apply_pull(result["changes"], result["cursor"])
                if not result["has_more"]:
                    return config
            raise RuntimeError("同步分页较多，请继续同步")
