# 私有云服务部署

本分支实现账号、设备撤销、事务同步和持久分析任务，服务于 Android、iOS 主 App、Windows 的统一云模式。它仍是开发预览：构建和自动测试不等于三台真机验收。客户端的旧直连模式继续保留，云模式失败不会自动切换。

- Ubuntu 24.04 amd64 + systemd；固定 PocketBase 0.40.5，校验官方 SHA256。
- PocketBase `127.0.0.1:8090`；FastAPI 网关 `127.0.0.1:8000`；只运行一个 Uvicorn worker。
- 基础安装保持私网；域名审核及解析完成后可按下方步骤启用客户端 HTTPS。SSH、分区不作修改。
- 账号预建，无公开注册；模型 Key 只在服务端。所有客户端账号请求经过网关；PB 集合全部禁止普通 REST 直接读写。

## 安装及升级

在核实 SSH 身份后上传审阅过的 `deploy/` 和 `services/`，以 root 执行 `bash deploy/bootstrap.sh`。已有部署升级前必须先运行 `/usr/local/sbin/goutou-backup` 并确认成功。不要在另一个部署/备份运行时升级。

安装器保留已有模型配置、Key、管理员和应用账号密码；独立生成 PB 内部凭据，PB 不持有模型 Key。首次预建 `owner@zovoeh.cn` 的随机密码，只写入服务器 root 专用文件。账户密码不是供应商 Key，也不是 PocketBase 超级管理员密码。

发布目录不可变，`/opt/goutou/current` 同时选择网关和 PB hooks/migrations。启动失败恢复旧服务定义及旧发布目录，不自动降级数据库。首次添加云集合是加法迁移；已有数据不作批量转换。数据库发生不兼容变更时必须人工审阅一致备份后恢复。

| 路径 | 用途 |
| --- | --- |
| `/etc/goutou/application-user.json` | 首个应用登录邮箱及密码，root:600 |
| `/etc/goutou/pocketbase-admin.json` | PB 超级管理员凭据，root:600 |
| `/etc/goutou/gateway.env` | operator、PB 内部凭据、模型 Key，root:600 |
| `/etc/goutou/pocketbase.env` | 仅内部凭据，root:600 |
| `/etc/goutou/models.yaml` | 服务端模型别名及配置 |
| `/var/lib/goutou/pocketbase` | 数据库及文件 |
| `/opt/goutou/current` | 当前不可变发布 |
| `/var/backups/goutou` | 含秘密的本地备份 |

凭据只在自己的 SSH 窗口查看，不发送到聊天或 Git。Windows 交付的 `open-server-manager.cmd` 可开启隧道、查看应用账号、查看 PB 管理员、检查健康状态。`configure-model-api.cmd` 用遮蔽输入框经固定主机 SSH stdin 更新 DeepSeek Key，Key 不进入命令行或本地配置文件。供应商列表校验不会发起生成；余额及实际生成能力需另行验证。

## 账号与同步

协议和固定 JSON 样例见 `contracts/cloud-v1.md`、`contracts/fixtures/`。

1. `POST /v1/auth/login` 创建设备会话，返回 PB token、设备 ID/secret 及 server_epoch。
2. 后续使用 Bearer token + `X-Device-Session` + `X-Device-Secret`，每次校验账号归属、撤销及到期。支持 refresh/logout、设备列表和撤销。
3. `POST /v1/sync/push` 每批最多50个不可变 op_id。实体、变更日志、回执在同一 PB 事务提交；重复提交不会重复写入。冲突带回服务端版本，客户端保留本地版本待明确解决。
4. `GET /v1/sync/pull` 使用账号及 epoch 绑定的不透明游标，每页最多200条，分页固定上界。历史分页不覆盖较新镜像。删除同步墓碑并清除历史内容，旧离线编辑不能自动复活。
5. 人物、会话、已确认消息、笔记、记忆、通用偏好、来源映射及分析记录按账号隔离。同名人物不自动合并；会话关联需手工确认。服务端拥有上下文版本和分析记录写权限。

初始阶段云模式仅用设备 OCR。Android 不支持微信的原有限制仍在；iOS 键盘只读主 App 发布的账号快照，不写同步库。各端前台约15秒同步一次；系统后台及离线期间不保证即时到达。

## 模型任务

`POST /v1/analyses` 创建持久任务；`GET /v1/analyses/{id}` 查询，不再生成。客户端先同步确认文字，再以固定 request_id 提交。相同请求重放复用原任务。普通账号不能访问 operator 专用 `/v1/chat/completions` 绕过任务记录。

服务端集中执行证据整理 → 候选生成 → 排序；解释/改写是独立持久任务。保留候选 ID 和顺序；当前不推算概率，权重为空并明确排序权重不可用。模型接口固定 HTTPS、禁止重定向、限制体积、并发与用量；无付费请求自动重试。超时/断线/进程中止后的不确定结果标为 indeterminate，用户明确新建才会再次付费。删除上下文会取消待执行任务；已经到达供应商的请求无法撤回。

当前限制：登录20次/分钟；新任务10次/分钟；模型阶段30次/分钟、最多2次并发。单个普通分析上限为1024+768+128输出 token（不是实际账单保证），解释/改写最多768输出 token。网关没有月度费用硬上限，需要自行控制供应商余额并关注用量。日志不记录请求正文、Key 或供应商错误正文。

## 旧开发隧道（可选）

日常客户端已使用 `https://api.zovoeh.cn`，不需要 SSH 隧道或 USB 转发。以下保留旧开发路径；PB 管理和私网就绪检查仍可使用管理隧道。

Windows 上运行交付的管理入口，选择隧道；保持窗口打开，客户端服务器填 `http://127.0.0.1:18000`。SSH 将其加密转发到 VPS 本机8000，同时提供 PB 管理入口 `http://127.0.0.1:18090/_/`。网关没有网页后台，访问 `/` 返回404正常；健康检查是 `/health/ready`，不代表真实模型生成已验证。

Android 调试 APK 可通过 USB 调试，在同一台 Windows 运行 `adb reverse tcp:18000 tcp:18000`，手机填同一个 localhost 地址。发布版只接受 HTTPS。不要填 VPS 明文公网 IP，也不要把电脑隧道监听改成0.0.0.0。

iPhone 的 localhost 指手机本身。HTTPS 地址已启用；iPhone 仍须完成 Apple 签名和安装后再验收。远程 macOS 构建检查已通过，但模拟器 ZIP 和未签名设备构建不能装入 iPhone，仍需相应账号/证书。

## 备份与恢复

每天04:20 UTC（中国12:20）加最多15分钟抖动，保留7份。`goutou-backup` 先暂停原先活动的 Caddy，再停止网关和 PB，打包静止数据库、root 配置及 Caddy 配置/TLS 存储；校验归档后按 PB、网关、Caddy 顺序尽力恢复原先活动服务。部署和备份共享维护锁。备份含 Key 和账号密码；仅为同 VPS 本地备份，需另行安排加密异地副本。

有意恢复前先保留当前完整备份，在临时目录用匹配版本/扩展验证旧备份，不要覆盖运行中的 SQLite。恢复数据库和匹配配置时先停止活动 Caddy，再停止网关和 PB；修正目录属主；先只启动 PB，再执行 `/usr/local/sbin/goutou-reset-sync-epoch`，然后启动网关，私网检查通过后再恢复原先活动的 Caddy。此命令要求网关停止，轮换 epoch、撤销所有设备、将未完成任务标为不确定。客户端重新登录后会冻结旧队列并要求明确恢复，不能静默回灌。

## 客户端 HTTPS

已启用地址为 `https://api.zovoeh.cn`。Caddy 通过公网 TCP80/443完成证书验证、HTTP跳转和HTTPS服务，自动管理可信证书。网关8000、PB8090及Caddy管理2019继续仅监听本机。日常客户端不依赖管理隧道；PB后台及原始模型管理调用仍通过SSH访问。

新服务器初次启用：先按 [Caddy官方Ubuntu文档](https://caddyserver.com/docs/install#debian-ubuntu-raspbian) 安装其签名稳定软件包；确认A记录指向目标IP且没有错误AAAA记录，再审阅本目录的 `Caddyfile` 和 `enable-https.sh`，执行后者。脚本先冷备份、验证配置，再增加80/443规则并等待可信TLS就绪；失败时尽力恢复原Caddy配置、服务状态及本次新增规则。脚本会拒绝已有显式80/tcp或443/tcp规则，以及非enabled/disabled的Caddy启用状态；其他范围规则或UFW应用规则须事先人工检查。后续配置调整需审阅、`caddy validate` 后reload；首次激活脚本不会重复覆盖已启用配置。

公网仅允许客户端使用的方法及路径：账号、设备、配置、同步、持久分析查询及证据/记忆历史。`/v1/chat/completions`、PB路由、`/health/ready`、文档和其他路径为404。公开 `/health/live` 只说明网关进程存活；完整就绪检查仍在私网。请求体上限262144字节，无反代付费请求重试。

Caddy不记录访问日志，并排除可能包含自定义设备密钥的请求错误日志，保留系统及证书日志。因此上游502诊断需私下检查网关和服务状态。现有限流为网关单进程全局额度，未宣称有每IP防护或完整抗DDoS能力。HTTPS状态保存在root专用 `/etc/goutou/https.json`；基础bootstrap清单继续描述本机后端。

切换地址前先在旧隧道下同步完待上传内容、处理冲突并查询未确认任务。客户端缓存按服务器地址隔离；新地址会重新下载云数据，旧地址下未上传内容和请求 ID 不会自动搬迁。保留旧数据库核对，不能直接清空或重新生成未确认任务。

冷备份在Caddy已安装时同时保存 `/etc/caddy` 和 `/var/lib/caddy`，先暂停原活动Caddy，再停后端；退出或失败时按PB、网关、Caddy顺序尽力恢复原活动服务。该备份仍仅位于同VPS，异地备份尚未配置。实际切换记录见 [HTTPS切换记录](../docs/https-cutover.md)。

## 验证

Python 3.12 隔离环境安装 `services/gateway/requirements.lock` 和 pytest，再运行：

```text
python -m pytest services/gateway/tests services/pocketbase/tests deploy/tests -q
bash -n deploy/bootstrap.sh
bash -n deploy/backup-local.sh
```

PB 测试必须提供匹配0.40.5二进制（见测试文件的环境变量），真实运行临时数据库；网关 E2E 使用模拟供应商，不产生模型费用。三端额外验证见各端 README。HTTPS切换和远程iOS构建已完成；iPhone签名安装、真实聊天OCR及相应真机验收仍需完成，当前仍是开发预览。
