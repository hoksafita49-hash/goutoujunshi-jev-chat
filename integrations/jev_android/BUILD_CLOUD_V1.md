# Android Cloud v1 构建记录

2026-10-21，本次仅修改 `integrations/jev_android/`，未提交或推送，未连接服务器或读取服务器凭据。

## 已实现

- 明确的云/直连方式选择、账号登录/刷新/退出、设备列表及撤销。云凭据使用 Android Keystore AES-GCM 保护，不进入普通配置字段或同步内容。
- 每服务器/账号独立 SQLite；服务端镜像与本地修改分层，修改和 outbox 同事务；发送中操作不改内容/ID；确认后只移除 applied，保留 conflict/rejected。pull 页面与游标同事务，push seq 不推进游标。
- 前台 15 秒与手动同步、编辑触发、串行执行及失败退避。远端墓碑隐藏旧 dirty 对象，来源映射持久化；服务器世代变更保存恢复副本、停止上传并要求重新认证/明确重建。
- 联系人、通用笔记、确认聊天、偏好、云端结果同步；聊天与云人物需明确关联，不按同名自动合并。旧 JSON 完整预览后确认导入，稳定来源设备/旧 ID 映射，不删除源文件；重复短消息保留行位置，`ts -> observed_at`，`order_key` 保留顺序。
- 核对后先同步上下文，再创建/查询持久分析、解释和改写任务。请求 ID 在 POST 前落盘；网络结果不明不自动新建付费任务；只有明确手动允许新任务才换 ID。显示前核对账号、人物关联、会话和上下文版本；候选顺序/ID由服务器决定，null 权重显示不可用。
- 核对打开时固定账号、设备、认证世代、人物与原始采集内容；认证或方式切换主动取消旧核对。账号切换与本地确认事务共用边界锁，队列中的工作不重新读取新账号作为写入目标；每次网络调用前再校验捕获的绑定。
- 本设备人物重绑会替换旧身份链接，忽略其他设备同标题；同一采集内容的核对更正按原消息 UID/版本更新，不追加错误原文和更正副本。云端改写的 `text` 独立显示，传 `selected_text`，保留原分析、候选和父分析 ID。
- 被拒草稿显示数量和错误码；仅用户明确确认后，先同步最新状态，再为 `dependency_failed` 生成新操作 ID。保留正文和顺序，将其排在已处理的父对象后；同对象的后续未发送编辑仍跟随旧稿。已删除目标/父对象和未解决父冲突会阻止重排，旧草稿保留。此动作不修改或重试模型 jobs。
- 云模式强制 ML Kit 本机 OCR，禁止使用旧供应商 Key 作为回退；HTTP 仅调试 loopback:18000，发布登录需 HTTPS；禁止 URL 内凭据/查询/片段/路径及重定向。

## 实际验证

运行环境为 Windows、Microsoft OpenJDK 21.0.10、Gradle 8.9、Android SDK 35（AGP 默认使用已安装 Build Tools 34，另安装 Build Tools 35 用于签名校验）。项目编译目标仍为 JVM 17、Android 11+。

固定版官方 Android command-line tools 11076708、SDK/Gradle 缓存下载至本端 `work/`；首次安装流水线已接受 `android-sdk-license`（工具显示 2019-01-16 版本）。这一行为发生在收到“不再自动接受新的 SDK 许可”限制之前；此后未接受额外许可。官方协议入口：<https://developer.android.com/studio/terms>。

Windows 生成目录很长，Gradle 默认缓存中的 AAPT2 无法启动；将同一官方 `aapt2-8.7.3-12006047-windows` 二进制复制至本端 `work/aapt2.exe` 后运行：

```powershell
$env:GRADLE_USER_HOME = Join-Path (Get-Location) 'work/gradle-home'
$env:ANDROID_HOME = Join-Path (Get-Location) 'work/android-sdk'
$env:ANDROID_SDK_ROOT = $env:ANDROID_HOME
.\gradlew.bat :app:testDebugUnitTest :app:assembleDebug --no-daemon "-Pandroid.aapt2FromMavenOverride=$((Join-Path (Get-Location) 'work/aapt2.exe'))"
```

最终输出 `BUILD SUCCESSFUL in 40s`。JUnit XML 汇总：**76 项测试，0 failure，0 error，0 skipped，17 个 suite**。新增云协议/本机 HTTP/SQLite 测试 26 项；读取根目录两个共用 fixtures，另有真实 pipeline 形状的 `text + candidates=[]` 改写 fixture。已有少量未使用参数等 Kotlin 警告保留。

回归证据：曾实际观察到远端墓碑/旧笔记 always_on 两个失败，修复后通过；独立审查后又先重现队列中 A 文本写入 B 库、重绑仍命中甲、两条更正变四条三个失败，以及改写结果 text 被丢弃的 fixture 失败，再修复转绿。账号测试使用真实单线程 Executor 排队及 SQLite，包含在切 B 后才构造 analyzer、稳定 A 的正向确认、切走再切回/换设备失效、核对时改绑人物禁止写入等行为。历史 pull v1/v2 不回滚 v3 镜像、base_version=3 的测试实际通过。一次新增测试因方法名过长导致 Windows 的 Robolectric SQLite 路径超过限制，缩短测试名后完整断言通过。

被拒草稿的三个 SQLite 测试也先实际失败再修复：错误码可见且自动 batch 不重试、父冲突解决后新 ID/顺序及后续编辑版本保留、目标或父对象删除时拒绝复活。测试同时断言已有模型请求 ID/任务 ID 不变。

`git diff --check` 通过。`apksigner verify --print-certs` 返回成功，证书为 `CN=Android Debug`。SDK/Gradle/Robolectric `work/`、`.gradle/`、`local.properties`、`app/build/` 都被 Git 忽略；无这些工具缓存进入源码变更。

APK：`app/build/outputs/apk/debug/app-debug.apk`。

SHA-256：`48D966CA13B111E66D8A35F8CC37D8B7C97B0FC94AF313469D2EA5C1BD65FDBC`。

签名证书 SHA-256：`9db6bb66f92611f8f436a5abb0566a9844571a7dd742e767fd3ebcc61ad97602`。

## 仍需真实环境验收

当前结果证明源码编译、单元测试、SQLite 行为与 APK 签名校验；尚无目标 Android 手机/模拟器，因此尚未验收硬件 Keystore、安装升级签名兼容、OCR/权限、进程被杀后恢复、与 Windows/iOS 的实际互通或真实供应商模型。域名/HTTPS 与服务端上线由主任务处理，不能据此标成“三端实机互通可用”。这是调试签名 APK；发布签名和旧包能否原位升级需按目标设备已有签名核对，不能假定调试证书相同。
