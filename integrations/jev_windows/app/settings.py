# -*- coding: utf-8 -*-
"""设置持久化。key 硬约束（docs/KICKOFF.md #6）：只进环境变量，绝不落文件；其余设置落 config.json。

key 的持久化走 Windows 用户环境变量（注册表 HKCU\\Environment，跟 setx 写的是同一个地方）。
全程只有两把本项目专用环境变量：GOUTOU_JEV_API_KEY、GOUTOU_LLM_API_KEY。
读的时候先看进程环境，没有就直接读注册表——IDE 启动时把环境快照拿走了，之后再 Run 继承的还是旧环境，
只靠 os.environ 会「保存了下次打开还是没有」。"""
from __future__ import annotations

import ctypes
import json
import os
import sys  # 只为下面这一处：打包后 __file__ 指向临时解包目录，config.json 得放在 exe 旁边才存得住

from core.providers import CUSTOM, DRAFT_PROVIDERS, JEV_ENV, JEV_PROVIDERS, LEGACY, LLM_ENV, DEEPSEEK_STRATEGY_ENV, OCR_ENV
from app import profiles

_ROOT = (os.path.dirname(sys.executable) if getattr(sys, "frozen", False)
         else os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
_CONFIG = os.path.join(_ROOT, "config.json")
_DEFAULT_RELATIONSHIP = "romantic partners"
_DEFAULT_CONTEXT = 10
_DEFAULT_JEV = "openrouter"
_DEFAULT_DRAFT = "deepseek"
STAGES = ("未填写", "初识", "了解中", "暧昧", "约会中", "伴侣", "关系结束")
GOALS = ("自然接话", "主动邀约", "澄清关系", "修复冲突", "减少投入", "结束联系")


def _read(name: str, default=None):
    """读 config.json 里的一个字段；每次都重新读文件，改设置不用重启进程。"""
    try:
        with open(_CONFIG, encoding="utf-8") as f:
            value = json.load(f).get(name)
    except (OSError, ValueError):
        return default
    return default if value is None else value

def relationship() -> str:
    return str(_read("relationship") or _DEFAULT_RELATIONSHIP)

def target_app() -> str:
    """盯哪个聊天应用：wechat（默认，行为与以前完全一致）或 qq（QQNT 主面板内嵌形态）。
    白名单之外的值一律回默认，改坏了 config.json 也不会把采集指到奇怪的东西上。"""
    value = _read("target_app", profiles.WECHAT)
    return value if value in profiles.TARGET_APPS else profiles.WECHAT

def context() -> int:
    """参考上下文条数：起草和判断各看最近多少条消息。3~30，缺失/脏数据一律退默认值。"""
    try:
        n = int(_read("context", _DEFAULT_CONTEXT))
    except (TypeError, ValueError):
        return _DEFAULT_CONTEXT
    return max(3, min(30, n))

def style() -> str:
    """用户自己描述的说话风格（可选，自由文本），只喂给起草模型。默认空 = 只照着最近的消息模仿。"""
    if cloud_mode():
        from .cloud_runtime import current
        try:
            row = current().require().db.visible("preferences", "00000000-0000-4000-8000-000000000001")
            return str((row or {}).get("payload", {}).get("style", ""))
        except RuntimeError:
            return ""
    return str(_read("style") or "")


def cloud_mode() -> bool:
    return _read("connection_mode", "direct") == "cloud"


def cloud_server() -> str:
    return str(_read("cloud_server") or "https://api.zovoeh.cn")

def profile_of(title: str) -> dict:
    """Opt-in per-conversation context; never stores a transcript."""
    if cloud_mode():
        from .cloud_runtime import current
        try:
            service = current()
            uid, _ = service.require().conversation_for(target_app(), title, service.source_device)
            row = service.require().db.visible("profiles", uid)
            payload = row["payload"]
            return {**{key: payload.get(key, default) for key, default in
                    (("stage", "未填写"), ("goal", "自然接话"), ("background", ""))},
                    "_cloud_uid": uid, "_cloud_version": row["version"], "_cloud_original": payload}
        except (RuntimeError, ValueError):
            return dict(stage="未填写", goal="自然接话", background="")
    data = _read("app_profiles", {})
    scoped = data.get(target_app(), {}) if isinstance(data, dict) else {}
    row = scoped.get(title) if isinstance(scoped, dict) else None
    if row is None and target_app() == profiles.WECHAT:
        legacy = _read("profiles", {})
        row = legacy.get(title, {}) if isinstance(legacy, dict) else {}
    if not isinstance(row, dict):
        row = {}
    return {"stage": row.get("stage", "未填写"), "goal": row.get("goal", "自然接话"),
            "background": row.get("background", "")}

def save_profile(title: str, stage: str, goal: str, background: str, *, expected_uid=None, expected_version=None, original_payload=None) -> None:
    if not title or len(title) > 200 or stage not in STAGES or goal not in GOALS or len(background) > 200:
        raise ValueError("请确认会话、阶段和目标；补充背景最多 200 字")
    if cloud_mode():
        from .cloud_runtime import current
        service = current()
        uid, _ = service.require().conversation_for(target_app(), title, service.source_device)
        if expected_uid and expected_uid != uid:
            raise ValueError("当前人物关联已变化，请重新打开设置核对")
        row = service.require().db.visible("profiles", uid)
        payload = row["payload"]
        if original_payload == payload:
            expected_version = row["version"]
        service.require().db.save("profiles", uid, {**payload, "stage": stage, "goal": goal, "background": background},
                                  expected_version=expected_version)
        return
    try:
        with open(_CONFIG, encoding="utf-8") as handle:
            data = json.load(handle)
    except (OSError, ValueError):
        data = {}
    app_profiles = data.get("app_profiles", {})
    if not isinstance(app_profiles, dict):
        app_profiles = {}
    scoped = app_profiles.get(target_app(), {})
    if not isinstance(scoped, dict):
        scoped = {}
    scoped[title] = {"stage": stage, "goal": goal, "background": background}
    app_profiles[target_app()] = scoped
    data["app_profiles"] = app_profiles
    with open(_CONFIG, "w", encoding="utf-8") as handle:
        json.dump(data, handle, ensure_ascii=False)

def profile_context(title: str) -> str:
    row = profile_of(title)
    extra = f"关系阶段：{row['stage']}；本轮目标：{row['goal']}"
    if row["background"]:
        extra += f"；补充背景：{row['background']}"
    return relationship() + "；" + extra

def jev_provider() -> str:
    """判断模型走哪家：openrouter（默认）或 typesafe 直连。"""
    v = _read("jev_provider")
    return v if v in JEV_PROVIDERS else _DEFAULT_JEV

def strategy_provider() -> str:
    """Independent strategy route: Jev or official DeepSeek."""
    value = _read("strategy_provider", "jev")
    return value if value in ("jev", "deepseek") else "jev"

def strategy_model() -> str:
    return str(_read("strategy_model") or "deepseek-flash")

def strategy_key() -> str:
    return _get_key(DEEPSEEK_STRATEGY_ENV) or (llm_key() if draft_provider() == "deepseek" else "")

def has_strategy_key() -> bool:
    return bool(jev_key()) if strategy_provider() == "jev" else bool(strategy_key())

def jev_model() -> str:
    """判断模型 id；空 = 用该来源的默认模型。"""
    return str(_read("jev_model") or "") or JEV_PROVIDERS[jev_provider()].default

def draft_provider() -> str:
    """起草走哪家（见 core/providers.DRAFT_PROVIDERS）。老配置里的 openrouter/deepseek 照样认。"""
    v = _read("draft_provider")
    return v if v in DRAFT_PROVIDERS else _DEFAULT_DRAFT

def draft_provider_name() -> str:
    return DRAFT_PROVIDERS[draft_provider()].name

def draft_model() -> str:
    """起草模型 id；空 = 用该来源的默认模型（有的来源没有默认，那就得自己选）。"""
    return str(_read("draft_model") or "") or DRAFT_PROVIDERS[draft_provider()].default

def draft_base_url() -> str:
    """自定义来源的 Base URL；其余来源用表里的，这里返回空。"""
    return str(_read("draft_base_url") or "") if draft_provider() in CUSTOM else ""

def reply_target() -> bool:
    """群聊指定回复对象：开了才在界面上选回复给谁、才把对象喂给模型。默认关。"""
    return bool(_read("reply_target", False))

def auto_analyze() -> bool:
    return bool(_read("auto_analyze", False))

def ocr_provider() -> str:
    if cloud_mode():
        return "local"  # This phase has no gateway OCR endpoint; never use a device vendor key.
    value = _read("ocr_provider", "local")
    return value if value in ("local", "deepseek", "openrouter") else "local"

def ocr_model() -> str:
    default = "deepseek-flash" if ocr_provider() == "deepseek" else "openrouter/free"
    return str(_read("ocr_model") or default)

def ocr_key() -> str:
    own = _get_key(OCR_ENV)
    return own or (llm_key() if draft_provider() == ocr_provider() else "")

def thinking() -> bool:
    """起草时是否开思考模式：慢且贵，默认关。只有 DeepSeek / OpenRouter / Anthropic / Gemini 吃它。"""
    return bool(_read("thinking", False))

def check_update() -> bool:
    """启动时要不要去 GitHub 查一次最新版本号：默认开，只出这一次网，设置里能关。"""
    return bool(_read("check_update", True))

def debug_view() -> bool:
    """调试视图：另开一个窗口实时画识别框。默认关，开了子进程才往队列里送帧。"""
    return bool(_read("debug_view", False))

def _read_env(env_name: str) -> str:
    """进程环境优先；没有就读注册表并带进进程环境，之后 core/ 里按 os.environ 读就有了。"""
    v = os.environ.get(env_name, "").strip()
    if not v:
        try:
            import winreg

            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment") as k:
                v = str(winreg.QueryValueEx(k, env_name)[0]).strip()
        except Exception:  # 非 Windows / 没这个值
            v = ""
        if v:
            os.environ[env_name] = v
    return v

def _get_key(env_name: str) -> str:
    """Only read this project's key slots; never inherit keys from upstream apps."""
    return _read_env(env_name)

def _set_key(env_name: str, value: str) -> None:
    """只写进程环境 + HKCU\\Environment，不写任何文件。"""
    os.environ[env_name] = value
    try:
        import winreg

        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, "Environment", 0, winreg.KEY_SET_VALUE) as k:
            winreg.SetValueEx(k, env_name, 0, winreg.REG_SZ, value)
        # 广播一下，之后新开的终端/进程就能看到；已经开着的 IDE 看不到也无所谓，启动时会读注册表
        ctypes.windll.user32.SendMessageTimeoutW(0xFFFF, 0x1A, 0, "Environment", 2, 5000, None)
    except Exception:
        pass  # 非 Windows（本机 Mac 开发）走不到，忽略

def jev_key() -> str:
    """判断那把 key，两家来源共用。"""
    return _get_key(JEV_ENV)

def has_jev_key() -> bool:
    return bool(jev_key())

def llm_key() -> str:
    """起草那把 key，所有语言模型来源共用。"""
    return _get_key(LLM_ENV)

def has_llm_key() -> bool:
    return bool(llm_key())

def has_key() -> bool:
    if cloud_mode():
        from .cloud_runtime import current
        return bool(current().session)
    return has_strategy_key()

def save(relationship_text: str | None = None, context_n: int | None = None, *,
         jev_provider_text: str | None = None, jev_key_text: str | None = None,
         jev_model_text: str | None = None, draft_provider_text: str | None = None,
         llm_key_text: str | None = None, draft_model_text: str | None = None,
         draft_base_url_text: str | None = None, reply_target_on: bool | None = None,
         style_text: str | None = None, thinking_on: bool | None = None,
         check_update_on: bool | None = None, debug_view_on: bool | None = None,
         strategy_provider_text: str | None = None, strategy_model_text: str | None = None,
         strategy_key_text: str | None = None, auto_analyze_on: bool | None = None,
         ocr_provider_text: str | None = None, ocr_model_text: str | None = None,
         ocr_key_text: str | None = None, target_app_text: str | None = None,
         cloud_mode_on: bool | None = None, cloud_server_text: str | None = None) -> None:
    """每个参数为空/None = 保留当前值。两把 key 写进程环境 + HKCU\\Environment，不写任何文件。"""
    jev = jev_provider_text if jev_provider_text in JEV_PROVIDERS else jev_provider()
    draft = draft_provider_text if draft_provider_text in DRAFT_PROVIDERS else draft_provider()
    for env, typed in ((JEV_ENV, jev_key_text), (LLM_ENV, llm_key_text),
                       (DEEPSEEK_STRATEGY_ENV, strategy_key_text), (OCR_ENV, ocr_key_text)):
        if typed:
            _set_key(env, typed)
    n = context() if context_n is None else max(3, min(30, int(context_n)))
    # 空串 = 清掉，None = 原样留着（读原始字段，别读补过默认值的那个）
    keep = lambda new, name: str(_read(name) or "") if new is None else str(new).strip()
    flag = lambda new, now: now() if new is None else bool(new)
    using_cloud = flag(cloud_mode_on, cloud_mode)
    if using_cloud and style_text is not None:
        from .cloud_runtime import current
        db = current().require().db
        uid = "00000000-0000-4000-8000-000000000001"
        old = (db.visible("preferences", uid) or {}).get("payload", {})
        db.save("preferences", uid, {**dict(tone="natural", length="short", count=3), **old, "style": str(style_text).strip()})
    # 整个 dict 必须在 open(..., "w") **之前**拼好：open 一上来就把文件截断，
    # 之后再 _read() 读到的是空文件，None 那几项就不是「保留」而是被清空了。
    data = {
        "connection_mode": "cloud" if using_cloud else "direct",
        "cloud_server": cloud_server() if cloud_server_text is None else cloud_server_text,
        # 关系为空 = 只改别的开关（调试视图那种单项保存），别把它写没了
        "relationship": relationship_text or relationship(), "context": n,
        "target_app": target_app_text if target_app_text in profiles.TARGET_APPS else target_app(),
        "style": keep(None if using_cloud else style_text, "style"),
        "jev_provider": jev, "jev_model": keep(jev_model_text, "jev_model"),
        "draft_provider": draft, "draft_model": keep(draft_model_text, "draft_model"),
        "draft_base_url": keep(draft_base_url_text, "draft_base_url"),
        "strategy_provider": strategy_provider_text if strategy_provider_text in ("jev", "deepseek") else strategy_provider(),
        "strategy_model": keep(strategy_model_text, "strategy_model"),
        "ocr_provider": ocr_provider_text if ocr_provider_text in ("local", "deepseek", "openrouter") else ocr_provider(),
        "ocr_model": keep(ocr_model_text, "ocr_model"),
        "reply_target": flag(reply_target_on, reply_target),
        "auto_analyze": flag(auto_analyze_on, auto_analyze),
        "thinking": flag(thinking_on, thinking),
        "check_update": flag(check_update_on, check_update),
        "debug_view": flag(debug_view_on, debug_view),
        "profiles": _read("profiles", {}) if isinstance(_read("profiles", {}), dict) else {},
        "app_profiles": _read("app_profiles", {}) if isinstance(_read("app_profiles", {}), dict) else {},
    }
    with open(_CONFIG, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False)
