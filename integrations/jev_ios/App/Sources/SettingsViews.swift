import SwiftUI

struct ProviderView: View {
    @EnvironmentObject var model: AppModel
    @State private var draft = AppConfig()
    @State private var deepseekKey = ""
    @State private var openrouterKey = ""
    @State private var jevKey = ""
    @State private var savedKeys = Set<String>()
    @State private var message = ""
    @State private var testing = false
    @State private var testTask: Task<Void, Never>?
    @State private var testRevision = 0

    var body: some View {
        Form {
            Section("运行方式") {
                Toggle("使用统一云服务", isOn: Binding(get: { model.cloudEnabled }, set: { model.setCloudEnabled($0); draft = model.config }))
                Text(model.cloudEnabled ? "账号、人物与已确认聊天经同一网关；截图只在手机本地识别。" : "直连接口模式，使用本机自行配置的服务商 Key。")
                    .font(.caption).foregroundStyle(Palette.secondary)
            }
            if model.cloudEnabled { CloudAccountView() }
            if !model.cloudEnabled {
            Section("策略判断与回复生成") {
                Picker("策略判断", selection: $draft.judge) { ForEach(JudgeProvider.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
                Picker("回复生成", selection: $draft.reply) { ForEach(ReplyProvider.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
                Button("判断＋回复均使用 DeepSeek") { draft.judge = .deepseek; draft.reply = .deepseek }
                Text("只用 DeepSeek 时只需配置 DeepSeek Key。策略先整理证据，再检查首 token 权重；缺失或不稳定时标注不可用。")
                    .font(.caption).foregroundStyle(Palette.secondary)
            }
            Section("DeepSeek 官方") {
                Text("https://api.deepseek.com").font(.caption).foregroundStyle(Palette.secondary)
                modelField("模型 ID", value: $draft.deepseekModel)
                keyField("deepseek", value: $deepseekKey)
                testButton("测试 DeepSeek", provider: .deepseek)
            }
            Section("OpenRouter") {
                Text("https://openrouter.ai/api/v1").font(.caption).foregroundStyle(Palette.secondary)
                modelField("回复模型 ID", value: $draft.openrouterModel)
                modelField("识图模型 ID（独立配置）", value: $draft.visionModel)
                keyField("openrouter", value: $openrouterKey)
                testButton("测试 OpenRouter 回复", provider: .openrouter)
                Text("识图模型须支持图片输入。模型 ID 可从服务商模型页复制；免费模型的可用性和限额由服务商决定。")
                    .font(.caption).foregroundStyle(Palette.secondary)
            }
            Section("TypeSafe Jev（选用）") {
                modelField("模型 ID", value: $draft.jevModel)
                keyField("jev", value: $jevKey)
                Button("测试 Jev 判断") { testJev() }.disabled(testing)
            }
            }
            Section("截图识别") {
                if model.cloudEnabled { Text("Apple Vision · 本地识别；本阶段不上传截图。") }
                else {
                Picker("识别方式", selection: $draft.ocr) { ForEach(OCRProvider.allCases, id: \.self) { Text($0.rawValue).tag($0) } }
                Text("Apple Vision 在手机本地识别。云端识图会发送你选的截图，使用对应服务的 Key 并产生用量；键盘自身不截屏，日常拼音和英文输入在本机完成。")
                    .font(.caption).foregroundStyle(Palette.secondary)
                }
            }
            Section("回复偏好") {
                Picker("口吻", selection: $draft.tone) { ForEach(preferenceChoices(["稳健", "会撩", "直接"], selected: draft.tone), id: \.self) { Text($0.isEmpty ? "未指定" : $0).tag($0) } }
                Picker("长度", selection: $draft.length) { ForEach(preferenceChoices(["简短", "适中", "详细"], selected: draft.length), id: \.self) { Text($0.isEmpty ? "未指定" : $0).tag($0) } }
                Stepper("最多 \(draft.count) 条候选", value: $draft.count, in: 1...3)
                TextField("我的表达风格（选填）", text: Binding(get: { draft.style ?? "" }, set: { draft.style = $0 }), axis: .vertical).lineLimit(2...5)
                Text("例如：自然、少用表情。清空后保存可移除已有风格。最多 4000 字；保留其他设备填写的口吻和长度。") .font(.caption).foregroundStyle(Palette.secondary)
            }
            Section {
                Button("保存并应用配置") { save() }.buttonStyle(BrandPrimaryButtonStyle())
                    .disabled(testing || model.store == nil).accessibilityIdentifier("saveConfigButton")
                if testing { HStack { ProgressView(); Button("取消测试") { cancelTest(); message = "已取消" } } }
                Text(message).font(.footnote).foregroundStyle(Palette.secondary).textSelection(.enabled)
            } footer: {
                Text(model.cloudEnabled ? "账号会话保存在共享钥匙串。确认后的聊天才同步；服务器统一配置模型，候选发送由你决定。" : "密钥保存在 App 与键盘共享的本机钥匙串，已存 Key 不回显；空白表示保留原 Key。聊天仅在你确认分析时发送到所选接口。")
            }
            Section("本地数据") {
                Button("清除键盘暂存与等待取用的内容") {
                    model.store?.clearHandoff(); model.store?.captureMailbox.clear(); model.store?.clearKeyboardSession()
                    message = "已清除键盘本轮暂存、临时交接与截屏内容"
                }
                Button("清空本轮原文与结果") { model.transcript = ""; model.invalidate(clear: true); message = "已清空本轮" }
            }
        }
        .scrollContentBackground(.hidden).background(Palette.cream)
        .onAppear { draft = model.config; if !model.cloudEnabled { refreshKeys() } }
        .onChange(of: model.config) { updated in if !testing { draft = updated } }
        .onDisappear { cancelTest(); deepseekKey = ""; openrouterKey = ""; jevKey = "" }
    }

    private func cancelTest() {
        testRevision += 1; testTask?.cancel(); testTask = nil; testing = false
    }
    private func preferenceChoices(_ presets: [String], selected: String) -> [String] {
        presets.contains(selected) ? presets : presets + [selected]
    }
    private func modelField(_ title: String, value: Binding<String>) -> some View {
        TextField(title, text: value).textInputAutocapitalization(.never).autocorrectionDisabled()
    }
    private func keyField(_ name: String, value: Binding<String>) -> some View {
        VStack(alignment: .leading, spacing: 8) {
            SecureField(savedKeys.contains(name) ? "已配置，填写新 Key 可替换" : "填写 API Key", text: value)
                .textInputAutocapitalization(.never).autocorrectionDisabled()
                .accessibilityIdentifier(name + "KeyField")
            if savedKeys.contains(name) {
                Button("删除此服务的 Key", role: .destructive) {
                    do { try CredentialVault.delete(name); value.wrappedValue = ""; refreshKeys(); model.cancel(); message = "已删除此服务的 Key" }
                    catch { message = error.localizedDescription }
                }.font(.caption)
            }
        }
    }
    private func refreshKeys() {
        do {
            savedKeys = []
            for name in CredentialVault.services where !(try CredentialVault.read(name)).isEmpty { savedKeys.insert(name) }
        } catch { message = error.localizedDescription }
    }
    private func secrets() throws -> ProviderSecrets {
        var result = try CredentialVault.load()
        if !deepseekKey.trimmed.isEmpty { result.deepseek = deepseekKey.trimmed }
        if !openrouterKey.trimmed.isEmpty { result.openrouter = openrouterKey.trimmed }
        if !jevKey.trimmed.isEmpty { result.jev = jevKey.trimmed }
        return result
    }
    private func save() {
        do {
            try draft.validate()
            guard let store = model.store else { throw GoutouError.invalid("App Group 未连接") }
            if model.cloudEnabled { draft.ocr = .local }
            for (name, value) in [("deepseek", deepseekKey), ("openrouter", openrouterKey), ("jev", jevKey)] where !model.cloudEnabled && !value.trimmed.isEmpty {
                try CredentialVault.write(value, name: name)
            }
            if model.cloudEnabled { try model.saveCloudPreferences(draft) }
            try store.saveConfig(draft)
            deepseekKey = ""; openrouterKey = ""; jevKey = ""
            if !model.cloudEnabled { refreshKeys() }; model.reload(); message = "已保存，下一次分析使用新配置。"
        } catch { message = error.localizedDescription }
    }
    private func testButton(_ title: String, provider: ReplyProvider) -> some View {
        Button(title) {
            do {
                let api = ModelAPI(transport: model.transport, config: draft, secrets: try secrets())
                cancelTest()
                let generation = testRevision
                testing = true; message = "连接测试中，会产生少量接口用量…"
                testTask = Task {
                    do {
                        try await api.test(provider); try Task.checkCancellation()
                        guard testRevision == generation else { return }
                        message = "连接成功；真实分析与识图还需分别验证。"
                    } catch {
                        guard testRevision == generation else { return }
                        message = error is CancellationError ? "已取消" : error.localizedDescription
                    }
                    testing = false
                }
            } catch { message = error.localizedDescription }
        }.disabled(testing)
    }
    private func testJev() {
        do {
            let api = ModelAPI(transport: model.transport, config: draft, secrets: try secrets())
            cancelTest()
            let generation = testRevision
            testing = true; message = "测试 Jev 判断中，会产生接口用量…"
            testTask = Task {
                do {
                    _ = try await GoutouPipeline(api: api).jevDecision("连接测试。对方说今天工作忙，用户希望自然接话。")
                    try Task.checkCancellation()
                    guard testRevision == generation else { return }
                    message = "Jev 策略接口连接成功。"
                } catch {
                    guard testRevision == generation else { return }
                    message = error is CancellationError ? "已取消" : error.localizedDescription
                }
                testing = false
            }
        } catch { message = error.localizedDescription }
    }
}

struct RelationshipView: View {
    @EnvironmentObject var model: AppModel
    @State private var draft = Relationship(id: "current")
    @State private var archive = false
    @State private var profiles = [Relationship]()
    @State private var message = ""
    @State private var deleteAll = false

    var body: some View {
        Form {
            Section {
                TextField("称呼或代号", text: $draft.label)
                Picker("关系阶段", selection: $draft.stage) { ForEach(Relationship.stages, id: \.self) { Text($0).tag($0) } }
                Picker("本轮目标", selection: $draft.goal) { ForEach(Relationship.goals, id: \.self) { Text($0).tag($0) } }
                TextField("最近发生了什么（最多 200 字）", text: $draft.background, axis: .vertical).lineLimit(3...6)
                TextField("需要遵守的边界或备注（最多 200 字）", text: $draft.notes, axis: .vertical).lineLimit(2...4)
                if !model.cloudEnabled { Toggle("同时保存为可切换的对象档案", isOn: $archive) }
                Button("保存当前对象与目标") {
                    do {
                        guard let store = model.store else { throw GoutouError.invalid("共享配置未连接") }
                        if model.cloudEnabled { try model.saveCloudProfile(draft); profiles = model.cloudProfiles }
                        else { try store.saveRelationship(draft, archive: archive); model.reload(); profiles = store.archived() }
                        message = "已保存；下次分析前请核对聊天对象。"
                    } catch { message = error.localizedDescription }
                }.buttonStyle(BrandPrimaryButtonStyle())
                Text(message).font(.footnote).foregroundStyle(Palette.secondary)
            } header: {
                Text("当前对象")
            } footer: {
                Text(model.cloudEnabled ? "保存后进入当前账号的同步队列。键盘从主 App 发布的快照读取人物，切换聊天后请手动核对对象。" : "只保存你主动填写的对象资料，不保存整份聊天。分析时会把当前对象的阶段、目标、背景与本轮原文交给所选模型。键盘无法自动识别聊天对象，换会话后请手动选择。")
            }
            Section("已保存的对象") {
                Button("新建对象") { draft = Relationship(); archive = true }
                ForEach(profiles) { profile in
                    Button { draft = profile; archive = true; if model.cloudEnabled { model.selectCloudProfile(profile) }; message = "已载入当前对象。" } label: {
                        VStack(alignment: .leading) { Text(profile.label); Text(profile.stage + " · " + profile.goal).font(.caption).foregroundStyle(Palette.secondary) }
                    }
                }.onDelete { indices in
                    do {
                        guard let store = model.store else { return }
                        for index in indices {
                            if model.cloudEnabled { try model.deleteCloudProfile(profiles[index].id) }
                            else { try store.deleteProfile(profiles[index].id) }
                        }
                        profiles = model.cloudEnabled ? model.cloudProfiles : store.archived(); model.reload(); draft = model.relationship
                    } catch { message = error.localizedDescription }
                }
                Button("删除所有对象资料", role: .destructive) { deleteAll = true }
            }
        }.scrollContentBackground(.hidden).background(Palette.cream)
            .onAppear {
                draft = model.cloudEnabled && UUID(uuidString: model.relationship.id) == nil ? Relationship() : model.relationship
                profiles = model.cloudEnabled ? model.cloudProfiles : model.store?.archived() ?? []
            }
            .onChange(of: model.cloudProfiles) { value in if model.cloudEnabled { profiles = value } }
            .confirmationDialog("删除所有本机对象资料？", isPresented: $deleteAll, titleVisibility: .visible) {
                Button("删除所有资料", role: .destructive) {
                    if model.cloudEnabled { do { for profile in profiles { try model.deleteCloudProfile(profile.id) } } catch { message = error.localizedDescription } }
                    else { model.store?.clearProfiles() }
                    model.reload(); draft = model.relationship; profiles = []
                }
            }
    }
}
