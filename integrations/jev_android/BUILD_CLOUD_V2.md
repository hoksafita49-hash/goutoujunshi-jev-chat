# Android 原文证据与人物记忆增量

2026-10-21；仅修改 `integrations/jev_android/`，未提交、推送或连接实际云服务器，未读取生产凭据，未操作真实手机。沿用 `contracts/cloud-v2.md` 和现有账号 SQLite/outbox。

## 使用入口

- 设置 → 云账号 → 查看已同步分析 → 选择分析：通过带会话认证的 `GET /v1/analyses/{id}/evidence` 查看说法、原话、说话人、时间、原消息版本及完整原消息快照。页面说明这是原分析上下文；“有原文引用”不代表推断已证实。旧分析或无有效引用的说法标明原话引用不可用。可明确保存为待确认记忆。接口返回 404 时不使用本地旧正文兜底。
- 设置 → 云账号 → 人物记忆：按人物手动新增，查看来源，编辑正文/类型并填写原因，单独确认事实、退回待确认、确认删除。假设须先明确改为事实并保存；新增/更正需先同步成功，才允许另行确认，避免未发送操作合并丢失确认步骤。列表与详情均提供立即同步/刷新，并区分本地请求确认、待上传和云端已确认。
- 记忆详情 → 云端历史：分页读取 `GET /v1/memories/{uid}/history?before_version=...&limit=50`。选择版本后明确恢复，使用点击恢复时的当前可见版本提交普通 outbox 更新，正文/来源取自所选版本，状态固定 pending，原因固定 `恢复版本 N`。不会回退本地/服务端版本；已删除对象不能恢复，服务端重新核对来源。
- 云账号 → 跨端回复偏好：保存最新 payload 的未知字段，候选数限 1–3，空 style 明确清除。新任务完整发送 tone/length/count/style 及保留字段；已有任务仍使用旧查找键、原请求编号和原请求正文，防止升级后重复创建任务。

证据/历史页面捕获账号、设备和认证世代；账号变化或认证失效会清空并关闭页面。网络返回前后均检查捕获绑定。更正保留 source_uids、source_refs 和未知 payload 字段；source_refs 仅作服务端快照字段保留，不作为客户端证明。编辑时检查当前可见记录，旧界面不能静默覆盖新版本；发送中操作的编号和正文仍不可变。历史和证据只读取网关，不生成模型任务。

收到分析删除墓碑时，在同一 SQLite 事务中清除任务结果和 `previous_job` 恢复归档正文，也清除请求中的 selected_text 与可能含候选正文的原缓存键。已取消任务改用 `deleted/<SHA256(原键)>` 查找，仅保留请求编号、分析编号及非正文上下文字段；原键查找仍命中取消记录，分析执行和“允许新请求”均拒绝重放。晚到网络写入先保留取消记录，再抛错阻止显示或复活正文；旧版本已缓存墓碑的数据库在重新打开及恢复导出时也会清理。此修复不改变数据库 schema。

## 实际验证

使用原有本机 Microsoft OpenJDK 21、Gradle 8.9、Android SDK 35、已缓存 AAPT2 与 Robolectric；未接受任何新 SDK 许可，全部构建使用 `--offline`。沙箱内 Gradle daemon 本地通信失败，随后用已获自动批准的本机工具执行。

在本目录运行：

```powershell
$env:GRADLE_USER_HOME = Join-Path (Get-Location) 'work/gradle-home'
$env:ANDROID_HOME = Join-Path (Get-Location) 'work/android-sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
.\gradlew.bat :app:testDebugUnitTest :app:assembleDebug --offline --no-daemon "-Pandroid.aapt2FromMavenOverride=$((Join-Path (Get-Location) 'work/aapt2.exe'))"
```

最终输出 `BUILD SUCCESSFUL in 28s`。JUnit XML 汇总 **92 tests、18 suites、0 failures、0 errors、0 skipped**；其中新增 16 项真实 SQLite/Robolectric 回归。已有未使用参数等 Kotlin 警告及 AAPT2 override 实验选项提示保留。

先实际观察到第一轮 6 个失败，再逐项修复；后续实际重现并修复：旧分析缺少外层 profile_uid、完整消息快照/版本未映射、更正/改类型仍保留确认态、偏好默认值改变已有任务查找键，以及更正未同步就确认被 outbox 合并。回归还覆盖跨人物/已删除来源、过期界面、来源/未知字段保留、历史分页与当前版本恢复、删除确认与墓碑、发送中正文/编号不变和回执后 base_version 更新、跨账号晚到响应。测试模型/HTTP 数据均为本机伪造，未调用付费模型。

最终删除隐私审查又先重现 4 个 SQLite 失败，再修复：活动/旧归档任务结果及候选正文泄漏到恢复导出、晚到结果/旧 pull 复活已删除内容、升级前墓碑旁的旧任务缓存未清理，以及取消任务仍返回缓存正文或允许另一次请求。回归同时断言散列键可持续命中取消记录、重复晚到写入不能替换原请求编号、无关结果保留，以及取消后的模型请求数为零。

`git diff --check -- integrations/jev_android` 通过，仅有 Git LF/CRLF 提示。Build Tools 35 `apksigner verify --verbose --print-certs` 返回 `Verifies`，APK Signature Scheme v2 有效。

APK：`app/build/outputs/apk/debug/app-debug.apk`，28,990,544 bytes。

SHA-256：`21602674C9E04C80B1C3917CCABE85534945A030EF41F95F2B5B9CE0700F321C`。

Android Debug 证书 SHA-256：`9db6bb66f92611f8f436a5abb0566a9844571a7dd742e767fd3ebcc61ad97602`。

## 实际设备限制

未验收目标 Android 手机/模拟器上的安装升级、真实 Keystore、权限/OCR/浮窗、页面布局与键盘交互、进程被杀后恢复或三端真实账号互通。当前结果证明源码编译、单元测试、SQLite 行为与调试 APK 签名校验，不代表真实设备或线上服务端验收完成。调试签名包不能据此视作发布签名；能否原位升级仍需核对目标设备已安装包的签名。
