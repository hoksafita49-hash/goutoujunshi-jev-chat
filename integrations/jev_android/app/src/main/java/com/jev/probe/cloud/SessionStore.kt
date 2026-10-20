package com.jev.probe.cloud

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import org.json.JSONObject
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

/** Only ciphertext and IV enter private preferences; the AES key never leaves Android Keystore. */
class SessionStore(context: Context) {
    private val prefs = context.applicationContext.getSharedPreferences("cloud_session_v1", Context.MODE_PRIVATE)
    private fun key(): SecretKey {
        val store = KeyStore.getInstance("AndroidKeyStore").apply { load(null) }
        (store.getKey(ALIAS, null) as? SecretKey)?.let { return it }
        return KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, "AndroidKeyStore").run {
            init(KeyGenParameterSpec.Builder(ALIAS, KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT)
                .setBlockModes(KeyProperties.BLOCK_MODE_GCM).setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE).build())
            generateKey()
        }
    }
    fun save(session: CloudSession) = synchronized(CloudAccountBoundary.lock) {
        val previous = load()
        val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply { init(Cipher.ENCRYPT_MODE, key()) }
        val bytes = cipher.doFinal(session.json().toString().toByteArray(Charsets.UTF_8))
        check(prefs.edit().putString("iv", Base64.encodeToString(cipher.iv, Base64.NO_WRAP))
            .putString("data", Base64.encodeToString(bytes, Base64.NO_WRAP)).commit()) { "无法保存登录状态" }
        if (previous?.accountKey != session.accountKey || previous.deviceId != session.deviceId) CloudAccountBoundary.changed()
    }
    fun load(): CloudSession? = synchronized(CloudAccountBoundary.lock) {
        val data = prefs.getString("data", null) ?: return@synchronized null
        try {
            val cipher = Cipher.getInstance("AES/GCM/NoPadding").apply {
                init(Cipher.DECRYPT_MODE, key(), GCMParameterSpec(128, Base64.decode(prefs.getString("iv", ""), Base64.NO_WRAP)))
            }
            CloudSession.read(JSONObject(String(cipher.doFinal(Base64.decode(data, Base64.NO_WRAP)), Charsets.UTF_8)))
        } catch (_: Exception) { clear(); null }
    }
    fun clear() = synchronized(CloudAccountBoundary.lock) {
        val existed = prefs.contains("data")
        prefs.edit().clear().commit()
        if (existed) CloudAccountBoundary.changed()
    }
    companion object { private const val ALIAS = "goutou_cloud_session_v1" }
}
