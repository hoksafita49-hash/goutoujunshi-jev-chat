"""Preview first; import only explicitly confirmed old relationship profiles."""
import json
from pathlib import Path


def preview_profiles(path):
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    scoped = data.get("app_profiles", {})
    scoped = scoped if isinstance(scoped, dict) else {}
    rows = []
    for app in ("wechat", "qq"):
        profiles = dict(data.get("profiles", {})) if app == "wechat" and isinstance(data.get("profiles"), dict) else {}
        if isinstance(scoped.get(app), dict):
            profiles.update(scoped[app])
        for title, row in profiles.items():
            if isinstance(title, str) and isinstance(row, dict):
                rows.append(dict(app=app, title=title, stage=str(row.get("stage", "未填写")),
                                 goal=str(row.get("goal", "自然接话")), background=str(row.get("background", ""))))
    return rows


def import_profiles(db, rows, device):
    count = 0
    for row in rows:
        source = f"config.json:{device}:{row['app']}:{row['title']}"
        uid = db.stable_uid(source)
        if db.visible("profiles", uid):
            continue
        db.save("profiles", uid, dict(name=row["title"], stage=row["stage"], goal=row["goal"], background=row["background"], notes=""))
        identity = db.stable_uid(source + ":identity")
        db.save("identity_links", identity, dict(profile_uid=uid, platform=row["app"], legacy_id=row["title"], source_device=device))
        count += 1
    return count
