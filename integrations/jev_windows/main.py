# -*- coding: utf-8 -*-
"""父进程：只管界面。截图 + OCR 在 app/worker.py 的子进程里跑，队列里收新消息 →
冒出新的对方消息才调 engine → 悬浮窗给 3 条候选 → 人点「填入」。发送永远手动。静默期零调用。
上下文、结果、聊天记录按应用和会话名分开存，切会话或应用时不混用。

    pip install rapidocr-onnxruntime numpy windows-capture PySide6-Fluent-Widgets
两个模型（判断 Jev / 起草语言模型）的来源和 key 在独立设置页填写，不用改代码。IDE 里直接 Run。
"""
import ctypes
import multiprocessing
import queue
import threading
import traceback
from collections import deque

from app import profiles, settings, update, worker
from app.capture import find_app_hwnd
from app.fill import fill
from app.overlay import Overlay
from app.review import ReviewDialog
from app.version import VERSION
from core.engine import analyze
from core.experience import details as generate_details, explain as explain_candidate, rewrite as rewrite_candidates
from core.transcript import merge_transcript, similar

# {(应用, 会话名): {history, result, rev, target, senders}}
# history 里是 [(who, text, name)]，engine 只认 her/me，name 是群里的发言人（单聊/自己说的是 None）；
# 只是缓冲区，实际喂模型几条由设置里的「参考上下文」决定
# senders：这个群里发过言的人，去重、最近的排最前；target：用户挑的回复对象（None = 跟着最近那个走）
chats = {}
state = {"area": None, "busy": False, "rerun": None, "hwnd": None, "chat": "", "reviewing": False,
         "app": None, "worker_stop": None, "epoch": 0}
capture_on = debug_on = None  # 采集开关/调试开关；由 spawn_worker 每次重启时换新，见那里的注释
results = queue.Queue()
extras = queue.Queue()
update_result = queue.Queue()  # 独立小队列，别跟 results 的 (kind, r, title, revision) 形状搅在一起
connection_identity = "direct"


def chat_of(title, app=None):
    key = (app or state["app"] or settings.target_app(), title)
    return chats.setdefault(key, {"history": deque(maxlen=60), "result": None, "rev": 0,
                                    "target": None, "senders": []})


def invalidate_capture():
    """Revoke fill permission and reject late model results from the old capture."""
    state["area"], state["chat"], state["rerun"] = None, "", None
    state["epoch"] += 1
    state["busy"] = False
    for chat in chats.values():
        chat["rev"] += 1
    ov.invalidate_replies()
    ov.set_busy(False)


def target_of(title):
    """这个会话现在的回复对象：用户挑过且人还在就用它，否则用最近说话的那个；单聊没有发言人 → None。"""
    chat = chat_of(title)
    if chat["target"] in chat["senders"]:
        return chat["target"]
    return chat["senders"][0] if chat["senders"] else None


def fill_reply(text):
    app = state["app"] or settings.target_app()
    name = profiles.app_name(app)
    if state["hwnd"] is None:  # 子进程重开过，hwnd 可能换了，用最新的
        raise RuntimeError(f"未找到{name}窗口，请确认{name}已打开")
    if state["area"] is None:
        raise RuntimeError(f"{name}输入区域尚不可用，请确认{name}聊天窗口可见（不要最小化）")
    if not state["chat"] or ov.current_chat() != state["chat"]:
        raise RuntimeError(f"会话已切换，先核对当前{name}会话再填入")
    if find_app_hwnd(app) != state["hwnd"]:
        raise RuntimeError(f"{name}窗口已变化，等待采集更新后重试")
    if settings.reply_target() and ov.at_prefix_enabled():
        target = target_of(ov.current_chat())  # 填进去的是界面上正看着的那个会话的对象
        if target:
            text = f"@{target} " + text  # 纯文本，不认成真正的 @，只是让群里看得出在跟谁说
    fill(state["hwnd"], state["area"], text, app)


def spawn_worker(active=True):
    """开一个采集子进程。每次重启都换一套全新的同步对象（capture_on/debug_on/stop）：
    被停掉的旧子进程就算死在信号量操作的中间，也污染不到新一代进程用的对象。
    这里踩过大坑：旧代码在运行时共享同一组 Event，强杀子进程后打坏了它的内部计数，
    下一次 capture_on.set() 会把主线程永久卡在 Condition 里（AppHang 实测，Windows 事件日志可见）。"""
    global capture_on, debug_on, q
    was_debug = settings.debug_view() or (debug_on is not None and debug_on.is_set())
    capture_on = multiprocessing.Event()
    debug_on = multiprocessing.Event()
    stop = multiprocessing.Event()
    q = multiprocessing.Queue()  # no queued frames/locks from the previous worker
    if active:
        capture_on.set()
    if was_debug:
        debug_on.set()
    p = multiprocessing.Process(target=worker.run,
                                args=(q, state["hwnd"], capture_on, debug_on, stop, state["app"]), daemon=True)
    p.start()
    state["worker_stop"] = stop
    return p


def stop_worker():
    """先礼后兵地停掉采集子进程：stop 事件让它在循环里自己收摊（锁都在可控点释放），3 秒还不走
    才 terminate。以前上来就 terminate，子进程可能死在共享信号量里把父进程的下一次 Event 操作卡死。"""
    global child
    if child is None:
        return
    stop = state.get("worker_stop")
    if stop is not None:
        stop.set()
    child.join(timeout=3)
    if child.is_alive():
        child.terminate()
        child.join(timeout=2)
    child = None
    state["worker_stop"] = None
    q.cancel_join_thread()
    q.close()


def set_debug(on):
    """调试视图开关：开 → 开窗 + 置位（子进程这才开始送帧，一帧 2~3MB）；关 → 清掉 + 收窗。"""
    global dbg
    if not on:
        if debug_on is not None:
            debug_on.clear()
        if dbg is not None:
            dbg.hide()
        return
    if dbg is None:
        from app.debugwin import DebugWindow

        dbg = DebugWindow(on_close=on_debug_closed)
    dbg.show()
    if debug_on is not None:
        debug_on.set()


def on_debug_closed():
    """用户直接关了调试窗 = 把开关也关了，否则设置页显示开着但没窗。"""
    if debug_on is not None:
        debug_on.clear()
    ov.set_debug_switch(False)
    settings.save(debug_view_on=False)


def on_toggle_capture(on):
    """标题栏开关。开着再点=重新找一次目标窗口：换了窗口就顺势接过去（重启采集子进程），
    窗口没变就只是恢复采集；没开过就找窗口后开起来。"""
    global child
    if not on:
        if capture_on is not None:
            capture_on.clear()
        return
    if child is None:
        try:
            state["hwnd"] = find_app_hwnd(state["app"])
        except RuntimeError:
            ov.set_capture(False, f"未找到{profiles.app_name(state['app'])}窗口，打开后再开启采集")
            return
        child = spawn_worker(True)
        return
    try:
        hwnd = find_app_hwnd(state["app"])
    except RuntimeError:
        hwnd = state["hwnd"]  # 找不到就维持现状（窗口可能只是暂时被挡住/收起来）
    if hwnd != state["hwnd"]:
        state["hwnd"] = hwnd
        invalidate_capture()
        stop_worker()
        child = spawn_worker(True)
    else:
        capture_on.set()


def on_settings_saved():
    """Refresh the child so newly saved OCR credentials are read immediately.
    目标应用换过（微信 ↔ QQ）：旧窗口句柄和会话状态全部作废，重找窗口再重启采集。"""
    global child, connection_identity
    identity = "direct"
    if settings.cloud_mode() is True:
        from app.cloud_runtime import current
        identity = current().identity
    if identity != connection_identity:
        chats.clear()
        ov.reset_chat_state()
        connection_identity = identity
    active = capture_on is not None and capture_on.is_set()
    invalidate_capture()
    if settings.target_app() != state["app"]:
        state["app"] = settings.target_app()
        ov.reset_chat_state()
        try:
            state["hwnd"] = find_app_hwnd(state["app"])
        except RuntimeError:
            state["hwnd"] = None
            if capture_on is not None:
                capture_on.clear()
            stop_worker()
            ov.set_capture(False, f"未找到{profiles.app_name(state['app'])}窗口，打开后再开启采集")
            return
    if child is None:
        if state["hwnd"] is not None and active:
            child = spawn_worker(True)
        return
    stop_worker()
    child = spawn_worker(active)
    ov.set_status("设置已保存；识图服务已重新载入", "success")


def analyze_bg(msgs, title, revision, reply_to=None, epoch=None, relationship=None, account_identity=None, captured_app=None, source_messages=None, expected_context=None):
    """后台线程只跑网络调用，结果丢队列；UI 只在主线程的 tick 里动（Qt 不能跨线程碰）。"""
    try:
        if account_identity is not None and settings.cloud_mode() is not True:
            raise RuntimeError("连接方式已变化，请重新核对；未调用本机模型")
        if captured_app is not None and account_identity is None and settings.cloud_mode() is True:
            raise RuntimeError("连接方式已变化，请重新核对后提交云端")
        if settings.cloud_mode() is True:
            from app.cloud_runtime import current
            service = current()
            identity = account_identity or service.identity
            result = service.analyze(captured_app or state["app"], title, msgs, None,
                                     expected_identity=identity, source_messages=source_messages, expected_context=expected_context)
            result["cloud_identity"] = identity
            result["reviewed_messages"] = msgs
            results.put(("ok", result, title, revision, epoch))
            return
        result = analyze(msgs, relationship, context=settings.context(),
                                   model=settings.draft_model() or None,
                                   provider=settings.draft_provider(),
                                   base_url=settings.draft_base_url() or None,
                                   reply_to=reply_to, style=settings.style(),
                                   thinking=settings.thinking(),
                                   jev_provider=settings.jev_provider(),
                                   jev_model=settings.jev_model() or None,
                                   strategy_provider=settings.strategy_provider(),
                                   strategy_model=settings.strategy_model(),
                                   strategy_key=settings.strategy_key())
        result["reviewed_messages"] = msgs
        results.put(("ok", result, title, revision, epoch))
    except Exception as e:
        results.put(("err", f"分析失败: {e}", title, revision, epoch))


def check_update_bg():
    """启动时后台查一次新版本，跟 analyze_bg 一个套路：网络调用在线程里，UI 只在 tick() 里动。"""
    r = update.check_latest(VERSION)
    if r:
        update_result.put(r)


def start_analyze(title, msgs):
    if state["reviewing"] or state["busy"]:
        return
    state["reviewing"] = True
    epoch, app = state["epoch"], state["app"]
    account_identity = None
    if settings.cloud_mode() is True:
        from app.cloud_runtime import current
        account_identity = current().identity
    revision_of = lambda: chat_of(title, app)["rev"]
    expected_context = None
    try:
        if settings.cloud_mode() is True:
            try:
                service = current()
                expected_context = service.require().conversation_for(app, title, service.source_device)
            except (RuntimeError, ValueError) as exc:
                ov.set_status(str(exc)[:220], "warning")
                return
        dialog = ReviewDialog(title, msgs, ov.win,
                              history_of=lambda: list(chat_of(title, app)["history"]),
                              revision_of=revision_of)
        if not dialog.exec():
            ov.set_status("已取消本轮分析")
            return
        if (epoch != state["epoch"] or dialog.reviewed_revision != revision_of()
                or state["chat"] != title):
            ov.set_status("会话内容已变化，请重新核对后分析", "warning")
            return
        original_rows = getattr(dialog, "_source_rows", None)
        original_rows = list(original_rows) if isinstance(original_rows, (list, tuple)) else list(msgs)
        msgs = dialog.messages
    finally:
        state["reviewing"] = False
    if settings.cloud_mode() is True:
        from app.cloud_runtime import current
        if not current().session or current().identity != account_identity:
            ov.set_status("请先在设置中登录云账号；聊天草稿仍保存在当前账号本地缓存", "warning")
            return
    elif not settings.has_strategy_key():
        ov.set_status("请先在设置中配置模型", "warning")
        return
    if settings.cloud_mode() is not True and not settings.has_llm_key():
        ov.set_status(f"起草来源 {settings.draft_provider_name()} 没填密钥，去设置里补上", "warning")
        return
    state["busy"] = True
    ov.set_busy(True)
    reply_to = target_of(title) if settings.reply_target() else None  # 开关关着就是今天的行为
    threading.Thread(target=analyze_bg, args=(msgs, title, revision_of(), reply_to,
                                             epoch, settings.profile_context(title), account_identity, app, original_rows, expected_context),
                     daemon=True).start()


def manual_analyze():
    title = ov.current_chat() or state["chat"]
    messages = list(chat_of(title)["history"]) if title else []
    if not messages or title != state["chat"]:
        ov.set_status("请先打开并读取要分析的会话", "warning")
        return
    start_analyze(title, messages)


def _extra_bg(kind, title, revision, result, index=None, epoch=None, relationship=None):
    try:
        if result.get("cloud_identity") and settings.cloud_mode() is not True:
            raise RuntimeError("连接方式已变化，请重新分析；未调用本机模型")
        if settings.cloud_mode() is True:
            from app.cloud_runtime import current
            from app.gateway import result_for_ui
            service = current()
            if result.get("cloud_identity") != service.identity:
                raise RuntimeError("账号已变化，请重新分析")
            cloud = service.extra(result, kind, index)
            payload = {"cloud_identity": service.identity, "value": cloud.get("text", ""),
                       "context": {key: result[key] for key in ("cloud_identity", "profile_uid", "conversation_uid", "context_revision")},
                       "mapped": result_for_ui(cloud) if kind == "rewrite" and cloud.get("candidates") else None}
            extras.put(("cloud_" + kind, payload, title, revision, epoch))
            return
        kwargs = dict(provider=settings.draft_provider(), model=settings.draft_model(),
                      key=settings.llm_key(), base_url=settings.draft_base_url() or None)
        messages = result["reviewed_messages"]
        if kind == "details":
            payload = generate_details(messages, relationship, result, **kwargs)
        elif kind == "explain":
            payload = explain_candidate(messages, relationship, result, index, **kwargs)
        else:
            payload = rewrite_candidates(messages, result, **kwargs)
        extras.put((kind, payload, title, revision, epoch))
    except Exception as exc:
        extras.put(("error", str(exc)[:180], title, revision, epoch))


def _on_extra(kind, index=None):
    title = ov.current_chat()
    chat = chat_of(title) if title else None
    result = chat["result"] if chat else None
    if not result or title != state["chat"] or not result.get("reviewed_messages"):
        ov.set_status("请先核对并分析当前会话", "warning")
        return
    if kind == "rewrite" and not result.get("candidates"):
        ov.set_status("当前没有可改写的候选", "warning")
        return
    ov.set_status("正在整理分析理由…" if kind == "explain" else
                  "正在整理详细分析…" if kind == "details" else "正在按你的原话调整口吻…", "busy")
    threading.Thread(target=_extra_bg, args=(kind, title, chat["rev"], result, index,
                     state["epoch"], settings.profile_context(title)), daemon=True).start()


def on_target_change(title, name):
    """用户挑了回复对象：记下来，这个会话里有对方的话就照新对象重跑一次。"""
    chat = chat_of(title)
    chat["target"] = name
    msgs = list(chat["history"])
    if not any(m[0] == "her" for m in msgs):
        return
    if state["busy"]:
        state["rerun"] = (title, msgs)
        ov.set_busy(True)
    else:
        start_analyze(title, msgs)


def drain():
    """把子进程队列里攒的东西全收掉。"""
    global child
    while True:
        try:
            msg = q.get_nowait()
        except (queue.Empty, ValueError, OSError):
            return
        kind = msg[0]
        if kind == "area":  # 只是窗口挪了位置，坐标跟着更新，别的什么都不用动
            state["area"] = msg[1]
            continue
        if kind == "chat":  # 微信切了会话，界面跟过去（用户正浏览别的会话时也跟，微信是准的）
            if state["chat"] != msg[1]:
                invalidate_capture()
            state["chat"] = msg[1]
            ov.set_chat(msg[1])
            continue
        if kind == "debug":  # 调试视图的一帧；窗口不在就直接丢掉
            if dbg is not None:
                dbg.show_packet(msg[1])
            continue
        if kind == "status":  # 单帧识别失败/报错，提示一下就好，别把正在跑的分析和已知坐标清掉
            ov.set_status(msg[1], "warning")
            ov.log(msg[1])
            continue
        if kind == "blocked":
            invalidate_capture()
            ov.set_status(msg[1], "warning")
            ov.log(msg[1])
            continue
        if kind == "paused":  # 子进程确认已暂停
            ov.set_capture(False)
            continue
        if kind == "resumed":  # 子进程重新开始采集
            ov.set_capture(True)
            continue
        if kind == "dead":  # 采集彻底停了（目标应用关了之类），这才是真的要清状态
            invalidate_capture()
            ov.set_capture(False, msg[1])
            ov.log(msg[1])
            if child is not None:  # 子进程已经不干活了，收掉引用，下次打开开关重开一个
                stop_worker()
            continue
        if kind == "rescan":  # 开/恢复采集：子进程把当前窗口可见的记录全量发来 → 并进累计历史
            _, title, rows, area = msg[:4]
            direction = msg[4] if len(msg) > 4 else "older"
            state["area"] = area
            state["chat"] = title
            ov.set_chat(title)  # 点采集立刻显示当前会话，不等下一条消息
            chat = chat_of(title)
            old = list(chat["history"])
            merged = merge_transcript(old, rows, unmatched=direction)
            added = len(merged) - len(old)
            chat["history"] = deque(merged, maxlen=60)
            chat["rev"] += 1
            for who, _text, name in merged:  # 发言人名册跟着重建（最近的排最前）
                if who == "her" and name:
                    if name in chat["senders"]:
                        chat["senders"].remove(name)
                    chat["senders"].insert(0, name)
            if title == ov.current_chat():
                ov.invalidate_replies()
            ov.set_targets(title, chat["senders"], target_of(title))
            if rows:
                ov.set_status(f"已读取当前窗口 {len(rows)} 条记录" +
                              (f"，累计 {added} 条新内容" if added > 0 else "（与已有记录重合）"), "success")
            else:
                ov.set_status("已连接当前会话，等待对方的新消息", "idle")
            continue
        _, title, new, area = msg
        state["area"] = area
        chat = chat_of(title)
        chat["rev"] += 1  # 这个会话有新消息了，它在跑的分析作废
        if title == ov.current_chat():  # 看的是别的会话就别把人家的候选划掉
            ov.invalidate_replies()
        for who, name, text in new:
            if any(w == who and n == name and similar(t, text) for w, t, n in chat["history"]):
                continue  # 抖动重复上报 / 重扫后的增量兜底：同一条只进一次
            chat["history"].append((who, text, name))
            ov.log_message(who, text, name, chat=title)
            if who == "her" and name:  # 群里发过言的人，去重后最近的排最前
                if name in chat["senders"]:
                    chat["senders"].remove(name)
                chat["senders"].insert(0, name)
        ov.set_targets(title, chat["senders"], target_of(title))  # 显不显示这一行由悬浮窗按开关决定
        if new[-1][0] == "her":  # 只有对方最新说话才值得分析
            msgs = list(chat["history"])
            if not settings.auto_analyze():
                ov.set_status("已读取新消息，点“核对原文并分析”")
            elif state["busy"]:
                state["rerun"] = (title, msgs)
                ov.set_busy(True)
            else:
                start_analyze(title, msgs)
        else:
            state["rerun"] = None
            ov.set_busy(False)
            ov.set_status("你已回复，等待对方的新消息")


def tick():
    try:
        drain()
        while not update_result.empty():
            latest, url = update_result.get()
            ov.set_update(latest, url)
        while not extras.empty():
            kind, payload, title, revision, epoch = extras.get()
            if epoch != state["epoch"]:
                continue
            if title != state["chat"] or title != ov.current_chat() or revision != chat_of(title)["rev"]:
                continue
            if kind.startswith("cloud_"):
                from app.cloud_runtime import current
                if not settings.cloud_mode() or not current().accept_result(payload["context"]):
                    continue
                if kind == "cloud_rewrite" and payload["mapped"]:
                    result = chat_of(title)["result"]
                    if result:
                        updated = {**result, **payload["mapped"]}
                        chat_of(title)["result"] = updated
                        ov.show(updated)
                else:
                    ov.show_details(payload["value"] or "未返回解释内容", "云端详细分析")
                continue
            if kind == "error":
                ov.set_status("操作失败：" + payload, "warning")
            elif kind == "details":
                ov.show_details(payload)
                ov.set_status("详细分析已生成", "success")
            elif kind == "explain":
                ov.show_details(payload, "回复理由与代价")
                ov.set_status("回复理由已生成", "success")
            else:
                result = chat_of(title)["result"]
                if result:
                    candidates, scores = payload
                    updated = {**result, "candidates": candidates, "scores": scores,
                               "best_index": max(range(len(scores)), key=scores.__getitem__) if scores else 0}
                    chat_of(title)["result"] = updated
                    ov.show(updated)
        while not results.empty():
            kind, r, title, revision, epoch = results.get()
            if epoch != state["epoch"]:
                continue
            state["busy"] = False
            if state["rerun"]:  # 分析期间又来了新消息，接着跑最新的
                (t, msgs), state["rerun"] = state["rerun"], None
                start_analyze(t, msgs)
                continue
            if revision != chat_of(title)["rev"]:  # 这个会话后来又说话了，这份结果过期了
                ov.set_busy(False)
                continue
            if kind == "ok":
                if r.get("cloud_identity"):
                    from app.cloud_runtime import current
                    if not settings.cloud_mode() or not current().accept_result(r):
                        ov.set_busy(False)
                        continue
                chat_of(title)["result"] = r  # 先存着；正看着这个会话才立刻贴上去
                if title == ov.current_chat():
                    ov.show(r)
                else:
                    ov.set_busy(False)
            else:
                ov.set_busy(False)
                ov.set_status(str(r)[:220] if settings.cloud_mode() is True else
                              "生成失败，请检查网络和服务设置；新消息到来后会重试。", "error")
                ov.log(r)
    except Exception:
        traceback.print_exc()  # 一帧出错不退出
    ov.after(50, tick)


if __name__ == "__main__":  # Windows 的 spawn 会让子进程重新执行本文件，没这行就无限套娃开进程
    multiprocessing.freeze_support()  # 打包成 exe 后 spawn 出来的子进程会重跑一遍 exe，没这行就无限弹界面
    ctypes.windll.user32.SetProcessDPIAware()
    q = multiprocessing.Queue()
    ov = Overlay(on_fill=fill_reply, on_toggle_capture=on_toggle_capture,
                 on_target_change=on_target_change, on_toggle_debug=set_debug,
                 on_manual_analyze=manual_analyze,
                 on_details=lambda: _on_extra("details"),
                 on_rewrite=lambda: _on_extra("rewrite"),
                 on_settings_saved=on_settings_saved,
                 on_explain=lambda index: _on_extra("explain", index),
                 result_of=lambda t: chat_of(t).get("result"))
    child = dbg = None
    state["app"] = settings.target_app()
    try:
        state["hwnd"] = find_app_hwnd(state["app"])
    except RuntimeError:
        ov.set_capture(False, f"未找到{profiles.app_name(state['app'])}窗口，打开后再开启采集")
    else:
        child = spawn_worker(True)
    if settings.debug_view():  # 上次开着就直接开回来
        set_debug(True)
    if not settings.has_key():
        ov.set_status("请先在设置中登录云账号" if settings.cloud_mode() else "请先在设置中配置模型", "warning")
        ov.after(0, ov.open_settings)
    if settings.check_update() and update.parse_version(VERSION):  # 开发版没有版本号，不查也不烦源码用户
        threading.Thread(target=check_update_bg, daemon=True).start()
    ov.after(50, tick)
    try:
        ov.run()
    finally:
        if child is not None:
            if state.get("worker_stop") is not None:
                state["worker_stop"].set()  # 先请子进程自己退，退不掉就 terminate
            child.join(timeout=1.5)
            if child.is_alive():
                child.terminate()
