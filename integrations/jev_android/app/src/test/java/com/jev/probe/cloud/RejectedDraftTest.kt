package com.jev.probe.cloud

import android.content.Context
import androidx.test.core.app.ApplicationProvider
import com.jev.probe.data.LocalDatabase
import org.json.JSONArray
import org.json.JSONObject
import org.junit.Assert.*
import org.junit.Test
import org.junit.runner.RunWith
import org.robolectric.RobolectricTestRunner
import org.robolectric.annotation.Config
import java.util.UUID

@RunWith(RobolectricTestRunner::class)
@Config(sdk = [30], application = android.app.Application::class)
class RejectedDraftTest {
    private val context: Context get() = ApplicationProvider.getApplicationContext()
    private fun database() = LocalDatabase.get(context, "rejected-${UUID.randomUUID()}")
    private fun record(type: String, uid: String, payload: JSONObject, version: Int = 1, deleted: Boolean = false) = JSONObject()
        .put("entity_type", type).put("uid", uid).put("version", version).put("payload", payload)
        .put("created_at", "2026-10-21T00:00:00Z").put("updated_at", "2026-10-21T00:00:00Z")
        .put("deleted_at", if (deleted) "2026-10-21T00:00:00Z" else JSONObject.NULL)
    private fun page(record: JSONObject) = JSONObject().put("cursor", UUID.randomUUID().toString())
        .put("changes", JSONArray().put(JSONObject().put("seq", 1).put("record", record)))
    private fun reject(op: JSONObject, code: String = "dependency_failed") = JSONObject().put("op_id", op.getString("op_id"))
        .put("status", "rejected").put("code", code)

    @Test fun rejectionCodeRemainsVisibleAndAutomaticBatchDoesNotRetry() {
        val db = database(); db.edit("notes", "n", JSONObject().put("body", "草稿"))
        val op = db.batch().getJSONObject(0)
        db.receipts(JSONArray().put(reject(op)))
        assertEquals("dependency_failed", db.rejectedOperations().single().getString("code"))
        assertEquals(op.getString("op_id"), db.rejectedOperations().single().getString("op_id"))
        assertEquals(0, db.batch().length())
    }
    @Test fun manualRequeuePreservesDependencyOrderAndGetsNewOperationIds() {
        val db = database(); db.edit("profiles", "p", JSONObject().put("name", "本地"))
        db.edit("conversations", "c", JSONObject().put("profile_uid", "p").put("title", "聊天").put("context_revision", 0))
        for (uid in listOf("m1", "m2")) db.edit("messages", uid, JSONObject().put("profile_uid", "p")
            .put("conversation_uid", "c").put("text", uid).put("confirmed", true))
        val original = db.batch(); val profile = original.getJSONObject(0)
        val receipt = JSONArray().put(JSONObject().put("op_id", profile.getString("op_id")).put("status", "conflict")
            .put("record", record("profiles", "p", JSONObject().put("name", "云端"))))
        for (i in 1 until original.length()) receipt.put(reject(original.getJSONObject(i)))
        db.receipts(receipt)
        assertEquals(3, db.rejectedOperations().size); assertEquals(0, db.batch().length())
        assertThrows(IllegalStateException::class.java) { db.requeueRejectedDependencies() }
        db.edit("messages", "m1", JSONObject().put("profile_uid", "p").put("conversation_uid", "c")
            .put("text", "m1-最新编辑").put("confirmed", true))
        db.resolve(profile.getString("op_id"), true)
        val job = JSONObject().put("request_id", "paid-request-is-unchanged")
        db.saveJob("analysis", job, "existing-job")
        assertEquals(3, db.requeueRejectedDependencies())
        val next = db.batch()
        assertEquals(listOf("p", "c", "m1", "m2"), (0 until next.length()).map { next.getJSONObject(it).getString("entity_uid") })
        val oldIds = (1 until original.length()).map { original.getJSONObject(it).getString("op_id") }
        for (i in 1 until next.length()) {
            val op = next.getJSONObject(i)
            assertFalse(op.getString("op_id") in oldIds)
            assertEquals(original.getJSONObject(i).getJSONObject("payload").toString(), op.getJSONObject("payload").toString())
        }
        assertTrue(db.rejectedOperations().isEmpty())
        assertEquals("m1-最新编辑", db.record("messages", "m1")!!.getJSONObject("payload").getString("text"))
        val applied = JSONArray()
        for (i in 0 until next.length()) {
            val op = next.getJSONObject(i)
            applied.put(JSONObject().put("op_id", op.getString("op_id")).put("status", "applied").put("record",
                record(op.getString("entity_type"), op.getString("entity_uid"), op.getJSONObject("payload"),
                    if (op.getString("entity_type") == "profiles") 2 else 1)))
        }
        db.receipts(applied)
        val later = db.batch()
        assertEquals(1, later.length()); assertEquals(1, later.getJSONObject(0).getInt("base_version"))
        assertEquals("m1-最新编辑", later.getJSONObject(0).getJSONObject("payload").getString("text"))
        assertEquals("paid-request-is-unchanged", db.job("analysis")!!.getJSONObject("body").getString("request_id"))
        assertEquals("existing-job", db.job("analysis")!!.getString("analysis_id"))
    }
    @Test fun manualRequeueRefusesDeletedObjectOrDeletedParent() {
        for (deletedType in listOf("profiles", "messages")) {
            val db = database()
            db.applyPage(page(record("profiles", "p", JSONObject().put("name", "云端"))))
            db.applyPage(page(record("conversations", "c", JSONObject().put("profile_uid", "p"))))
            db.edit("messages", "m", JSONObject().put("profile_uid", "p").put("conversation_uid", "c").put("text", "待上传"))
            val op = db.batch().getJSONObject(0); db.receipts(JSONArray().put(reject(op)))
            val uid = if (deletedType == "profiles") "p" else "m"
            db.applyPage(page(record(deletedType, uid, JSONObject(), version = 2, deleted = true)))
            assertThrows(IllegalStateException::class.java) { db.requeueRejectedDependencies() }
            assertEquals(op.getString("op_id"), db.rejectedOperations().single().getString("op_id"))
            assertEquals(0, db.batch().length())
        }
    }
}
