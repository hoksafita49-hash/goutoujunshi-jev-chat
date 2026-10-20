import SwiftUI

struct CloudAccountView: View {
    @EnvironmentObject var model: AppModel
    @State private var server = ""
    @State private var email = ""
    @State private var password = ""
    @State private var legacy = [Relationship]()
    @State private var previewed = false
    @State private var localMessage = ""
    var body: some View {
        Section("云账号") {
            TextField("HTTPS 服务器根地址", text: $server).textInputAutocapitalization(.never).autocorrectionDisabled()
                .disabled(!model.cloudEmail.isEmpty)
                .accessibilityIdentifier("cloudServerField")
            if model.cloudEmail.isEmpty {
                TextField("账号邮箱", text: $email).textContentType(.username).keyboardType(.emailAddress)
                    .textInputAutocapitalization(.never).autocorrectionDisabled().accessibilityIdentifier("cloudEmailField")
                SecureField("密码", text: $password).textContentType(.password).accessibilityIdentifier("cloudPasswordField")
                Button("登录") {
                    model.loginCloud(base: server, email: email, password: password); password = ""
                }.disabled(model.cloudBusy || email.trimmed.isEmpty || password.isEmpty).accessibilityIdentifier("cloudLoginButton")
                Text("仅使用服务器预建账号，不开放注册。域名启用 HTTPS 后方可连接。")
                    .font(.caption).foregroundStyle(Palette.secondary)
            } else {
                Text("当前账号：" + model.cloudEmail)
                Button("立即同步与查询任务") { model.syncCloud() }.disabled(model.cloudBusy)
                Button("退出当前设备", role: .destructive) { previewed = false; legacy = []; model.logoutCloud() }.disabled(model.cloudBusy)
            }
            if model.cloudBusy { ProgressView() }
            Text(model.cloudStatus).font(.footnote).textSelection(.enabled)
            Text(localMessage).font(.footnote)
        }.onAppear { server = model.store?.cloudServer ?? "https://api.zovoeh.cn" }
            .onDisappear { password = "" }
        if !model.cloudEmail.isEmpty {
            if model.cloudRecovery {
                Section("服务器恢复后处理") {
                    Text("上传已暂停。重建会先保存完整 SQLite 恢复副本（包含待上传操作），再重新拉取服务器数据；旧内容不会自动回灌。")
                    Button("保留恢复副本并重建本机云缓存") { model.rebuildCloud() }
                }
            }
            Section("旧人物资料导入") {
                Button("预览本机旧资料") {
                    do { legacy = try model.previewLegacy(); previewed = true; localMessage = "" }
                    catch { localMessage = error.localizedDescription }
                }
                if previewed {
                    Text("待导入 \(legacy.count) 项；原资料保留，同名人物分别导入。")
                    ForEach(legacy) { profile in
                        VStack(alignment: .leading) { Text(profile.label); Text(profile.stage + " · " + profile.goal); Text(profile.background); Text(profile.notes) }.font(.caption)
                    }
                    Button("确认导入以上资料到当前账号") { model.importLegacy(legacy); legacy = []; previewed = false }
                        .disabled(legacy.isEmpty || model.cloudBusy)
                }
            }
            if !model.cloudConflicts.isEmpty {
                Section("待处理的同步冲突") {
                    ForEach(model.cloudConflicts) { conflict in
                        VStack(alignment: .leading, spacing: 8) {
                            Text(conflict.operation.entityType + " · " + conflict.code)
                            Text("本机：" + describe(conflict.operation.payload)).font(.caption).textSelection(.enabled)
                            Text("服务器：" + describe(conflict.remote?.payload ?? [:])).font(.caption).textSelection(.enabled)
                            Button("采用服务器版本") { model.resolveCloud(conflict, useLocal: false) }
                            if conflict.remote?.deletedAt == nil { Button("保留本机版本并重新提交") { model.resolveCloud(conflict, useLocal: true) } }
                        }
                    }
                }
            }
            if !model.cloudPendingJobs.isEmpty {
                Section("持久分析任务") {
                    ForEach(model.cloudPendingJobs) { job in
                        VStack(alignment: .leading) {
                            Text(job.kind + " · " + (job.analysisId ?? "提交结果未确认")).font(.caption).textSelection(.enabled)
                            if job.analysisId == nil {
                                Button("使用同一个请求 ID 重试提交") { model.retryCloudJob(job) }
                                Text("请求内容保持原样；不会自动创建新任务。") .font(.caption)
                            }
                        }
                    }
                }
            }
        }
    }
    private func describe(_ payload: [String: JSONValue]) -> String {
        guard let data = try? JSONEncoder().encode(payload), let value = String(data: data, encoding: .utf8) else { return "无" }
        return value
    }
}
