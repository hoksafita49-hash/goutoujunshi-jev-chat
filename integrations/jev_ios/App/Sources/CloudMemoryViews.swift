import SwiftUI

struct CloudMemoryView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    @State private var editing: CloudRecord?
    @State private var creating = false
    @State private var deleting: CloudRecord?
    @State private var confirming: CloudRecord?
    @State private var message = ""
    @State private var identity = ""

    var body: some View {
        List {
            Section {
                Text("当前人物：" + model.relationship.label)
                Text("新记忆先待确认；“服务器已确认”表示已同步的事实。离线修改保留在本机队列，冲突在云账号页处理。") .font(.caption)
                Button("手动新增待确认记忆") { creating = true }
                Button("同步记忆") { model.syncCloud() }
            }
            ForEach(model.cloudMemories) { memory in
                Section {
                    Text(memory.payload["text"]?.string ?? "").textSelection(.enabled)
                    Text(memoryState(memory)).font(.caption).foregroundStyle(Palette.secondary)
                    Button("编辑或纠正") { editing = memory }
                    if memory.payload["kind"]?.string == "fact", memory.payload["status"]?.string != "confirmed" {
                        Button("明确确认为事实") { confirming = memory }
                    }
                    if memory.payload["status"]?.string == "confirmed" {
                        Button("退回待确认") { perform { try model.setCloudMemoryStatus(memory, status: "pending", identity: identity) } }
                    }
                    Button("查看修改历史（需联网）") { model.openCloudHistory(memory) }
                    Button("删除记忆", role: .destructive) { deleting = memory }
                }
            }
            if model.cloudMemories.isEmpty { Text("当前人物暂无记忆。可手动新增，或从分析依据主动保存。") }
            if !message.isEmpty { Text(message).font(.footnote).textSelection(.enabled) }
        }
        .navigationTitle("人物记忆")
        .onAppear { identity = model.cloudReviewIdentity; model.refreshCloudView() }
        .onChange(of: model.cloudReviewIdentity) { value in if value != identity { editing = nil; creating = false; deleting = nil; confirming = nil; dismiss() } }
        .sheet(isPresented: $creating) { NavigationStack { CloudMemoryEditor(record: nil, identity: identity) } }
        .sheet(item: $editing) { record in NavigationStack { CloudMemoryEditor(record: record, identity: identity) } }
        .sheet(item: $model.cloudHistoryMemory, onDismiss: { model.closeCloudReview() }) { _ in NavigationStack { CloudMemoryHistoryView(identity: identity) } }
        .confirmationDialog("删除这条记忆？", isPresented: Binding(get: { deleting != nil }, set: { if !$0 { deleting = nil } }), titleVisibility: .visible) {
            Button("确认删除", role: .destructive) { if let memory = deleting { perform { try model.deleteCloudMemory(memory, identity: identity) } }; deleting = nil }
            Button("取消", role: .cancel) { deleting = nil }
        } message: { Text("同步后将无法通过历史恢复。离线时先进入本机删除队列。") }
        .confirmationDialog("已核对内容，要将它确认为事实？", isPresented: Binding(get: { confirming != nil }, set: { if !$0 { confirming = nil } }), titleVisibility: .visible) {
            Button("我确认这是事实") { if let memory = confirming { perform { try model.setCloudMemoryStatus(memory, status: "confirmed", identity: identity) } }; confirming = nil }
            Button("取消", role: .cancel) { confirming = nil }
        }
    }
    private func memoryState(_ memory: CloudRecord) -> String {
        let kind = memory.payload["kind"]?.string == "fact" ? "事实" : "推测"
        let state = memory.payload["status"]?.string == "confirmed" ? "已确认" : "待确认"
        return kind + " · " + (model.cloudPendingMemoryUids.contains(memory.uid) ? "本机\(state)，待同步" : "服务器\(state)") + " · 版本 \(memory.version)"
    }
    private func perform(_ action: () throws -> Void) {
        do { try action(); message = "已进入当前账号的本机同步队列。" } catch { message = error.localizedDescription }
    }
}

struct CloudMemoryEditor: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let record: CloudRecord?
    let identity: String
    @State private var draft = CloudMemoryDraft(profileUid: "")
    @State private var initialized = false
    @State private var error = ""
    @State private var converting = false
    var body: some View {
        Form {
            Section("记忆内容") {
                TextEditor(text: $draft.text).frame(minHeight: 150)
                Text("\(draft.text.count)/4000 字").font(.caption)
                if draft.kind == "hypothesis" {
                    Text("类型：推测。推测不能确认，须主动改为事实。") .font(.caption)
                    Button("我已核实，将推测改为事实") { converting = true }
                } else {
                    Text("类型：事实（保存后仍待确认）") .font(.caption)
                    Button("改为推测") { draft.kind = "hypothesis" }
                }
                TextField("修改或纠正原因（最多 500 字）", text: $draft.changeReason, axis: .vertical).lineLimit(2...5)
                Text(draft.sourceUids.isEmpty ? "来源：用户手动陈述" : "来源：\(draft.sourceUids.count) 条已确认消息；保存时重新校验。") .font(.caption)
                if !draft.sourceUids.isEmpty { Button("移除消息来源，改为我的手动陈述") { draft.sourceUids = [] } }
            }
            Section {
                Button("保存为待确认记忆") {
                    do {
                        draft.status = "pending"
                        try model.saveCloudMemory(uid: record?.uid ?? UUID().uuidString.lowercased(), draft: draft, expectedVersion: record?.version ?? 0, identity: identity)
                        dismiss()
                    } catch { self.error = error.localizedDescription }
                }.disabled(draft.text.trimmed.isEmpty || draft.text.count > 4000 || draft.changeReason.count > 500)
                if !error.isEmpty { Text(error).font(.footnote).textSelection(.enabled) }
            }
        }
        .navigationTitle(record == nil ? "新增记忆" : "纠正记忆")
        .toolbar { ToolbarItem(placement: .cancellationAction) { Button("取消") { dismiss() } } }
        .onAppear {
            guard !initialized else { return }; initialized = true
            draft = record.map(CloudMemoryDraft.init(record:)) ?? CloudMemoryDraft(profileUid: model.relationship.id)
        }
        .onChange(of: model.cloudReviewIdentity) { value in if value != identity { draft.text = ""; dismiss() } }
        .onChange(of: model.relationship.id) { value in if initialized && value != draft.profileUid { draft.text = ""; dismiss() } }
        .confirmationDialog("将这条推测改为事实？", isPresented: $converting, titleVisibility: .visible) {
            Button("明确改为事实") { draft.kind = "fact"; draft.status = "pending" }
            Button("取消", role: .cancel) {}
        } message: { Text("保存后仍待确认；需要返回记忆列表再次明确确认。") }
    }
}

struct CloudEvidenceView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let document: CloudEvidenceDocument
    @State private var message = ""
    @State private var saved = Set<String>()
    var body: some View {
        List {
            Section {
                Text("这是提交该分析时的已确认原文快照，不会随之后编辑改变。") .font(.caption)
                Text("上下文版本 \(document.contextRevision)") .font(.caption)
            }
            ForEach(document.evidence) { claim in
                CloudEvidenceClaimSection(claim: claim, sources: document.verifiedSources(for: claim), saved: saved.contains(claim.id)) { save(claim) }
            }
            if document.evidence.isEmpty { Text("此分析没有可显示的依据。") }
            if !message.isEmpty { Text(message).font(.footnote) }
        }
        .navigationTitle("分析依据")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { model.closeCloudReview(); dismiss() } } }
        .onChange(of: model.cloudReviewIdentity) { _ in dismiss() }
        .onChange(of: model.relationship.id) { _ in dismiss() }
    }
    private func save(_ claim: CloudEvidenceClaim) {
        do {
            try model.saveCloudClaim(claim, document: document)
            saved.insert(claim.id); message = "已加入待确认记忆；可在人物记忆中编辑和核实。"
        } catch { message = error.localizedDescription }
    }
}

private struct CloudEvidenceClaimSection: View {
    let claim: CloudEvidenceClaim
    let sources: [CloudEvidenceSource]
    let saved: Bool
    let onSave: () -> Void
    var body: some View {
        Section(claim.kindLabel) {
            Text(claim.text).textSelection(.enabled)
            if sources.isEmpty {
                Text("引用不可用：旧分析或缺少可核实的原文引用。").font(.caption).foregroundStyle(Palette.secondary)
            }
            ForEach(sources) { source in CloudEvidenceSourceRow(source: source) }
            Button(saved ? "已保存为待确认记忆" : "主动保存为待确认记忆", action: onSave).disabled(saved)
            if sources.isEmpty { Text("无可用消息引用时，按用户主动保存的陈述记录。").font(.caption) }
        }
    }
}

private struct CloudEvidenceSourceRow: View {
    let source: CloudEvidenceSource
    private var metadata: String {
        let version = source.message.version.map { String($0) } ?? "未知"
        return "\(source.message.speaker) · \(source.message.observedAt) · 消息版本 \(version)"
    }
    var body: some View {
        VStack(alignment: .leading, spacing: 6) {
            Text("“\(source.quote)”").textSelection(.enabled)
            Text(metadata).font(.caption).foregroundStyle(Palette.secondary)
        }
    }
}

struct CloudMemoryHistoryView: View {
    @EnvironmentObject var model: AppModel
    @Environment(\.dismiss) private var dismiss
    let identity: String
    @State private var restoring: CloudRecord?
    @State private var error = ""
    var body: some View {
        List {
            Section { Text("从新到旧显示服务器历史。恢复会创建一个新的待确认修订，使用当前版本作为更新基础；消息来源在保存时重新校验。") .font(.caption) }
            ForEach(model.cloudHistoryVersions, id: \.version) { record in
                CloudMemoryHistorySection(record: record) { restoring = record }
            }
            if model.cloudReviewBusy { ProgressView("读取历史…") }
            if model.cloudHistoryMore { Button("加载更早版本") { model.loadCloudHistory() }.disabled(model.cloudReviewBusy) }
            if !model.cloudReviewStatus.isEmpty { Text(model.cloudReviewStatus).font(.footnote); Button("重试读取") { model.loadCloudHistory() } }
            if !error.isEmpty { Text(error).font(.footnote) }
        }
        .navigationTitle("记忆修改历史")
        .toolbar { ToolbarItem(placement: .confirmationAction) { Button("完成") { model.closeCloudReview(); dismiss() } } }
        .confirmationDialog("恢复为新的待确认修订？", isPresented: restorePresented, titleVisibility: .visible) {
            Button("恢复为待确认", action: restore)
            Button("取消", role: .cancel) { restoring = nil }
        }
        .onChange(of: model.cloudReviewIdentity) { value in if value != identity { restoring = nil; dismiss() } }
    }
    private var restorePresented: Binding<Bool> {
        Binding(get: { restoring != nil }, set: { if !$0 { restoring = nil } })
    }
    private func restore() {
        do {
            if let version = restoring { try model.restoreCloudMemory(version, identity: identity) }
            dismiss()
        } catch { error = error.localizedDescription }
        restoring = nil
    }
}

private struct CloudMemoryHistorySection: View {
    let record: CloudRecord
    let onRestore: () -> Void
    private var state: String {
        let kind = record.payload["kind"]?.string == "fact" ? "事实" : "推测"
        let status = record.payload["status"]?.string == "confirmed" ? "已确认" : "待确认"
        return "\(kind) · \(status)"
    }
    private var reason: String { record.payload["change_reason"]?.string ?? "" }
    var body: some View {
        Section("版本 \(record.version) · \(record.updatedAt)") {
            Text(record.payload["text"]?.string ?? "").textSelection(.enabled)
            Text(state).font(.caption)
            if !reason.isEmpty { Text("原因：\(reason)").font(.caption) }
            Button("恢复此版本为新的待确认修订", action: onRestore)
        }
    }
}
