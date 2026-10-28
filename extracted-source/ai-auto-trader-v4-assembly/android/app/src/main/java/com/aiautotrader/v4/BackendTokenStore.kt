package com.aiautotrader.v4

import android.content.Context
import android.security.keystore.KeyGenParameterSpec
import android.security.keystore.KeyProperties
import android.util.Base64
import java.security.KeyStore
import javax.crypto.Cipher
import javax.crypto.KeyGenerator
import javax.crypto.SecretKey
import javax.crypto.spec.GCMParameterSpec

internal class BackendTokenStore(context: Context) {
    private val preferences = context.applicationContext.getSharedPreferences(PREFERENCES_NAME, Context.MODE_PRIVATE)

    fun read(): String? {
        val encoded = preferences.getString(TOKEN_KEY, null) ?: return null
        return try {
            val payload = Base64.decode(encoded, Base64.NO_WRAP)
            require(payload.size > IV_BYTES + TAG_BITS / 8)
            val iv = payload.copyOfRange(0, IV_BYTES)
            val encrypted = payload.copyOfRange(IV_BYTES, payload.size)
            val cipher = Cipher.getInstance(TRANSFORMATION)
            cipher.init(Cipher.DECRYPT_MODE, getOrCreateKey(), GCMParameterSpec(TAG_BITS, iv))
            String(cipher.doFinal(encrypted), Charsets.UTF_8).takeIf { it.isNotBlank() }
        } catch (_: Exception) {
            clear()
            null
        }
    }

    fun save(token: String) {
        val normalized = token.trim()
        require(normalized.length >= 32)
        val cipher = Cipher.getInstance(TRANSFORMATION)
        cipher.init(Cipher.ENCRYPT_MODE, getOrCreateKey())
        val iv = cipher.iv ?: error("Missing encryption IV")
        val encrypted = cipher.doFinal(normalized.toByteArray(Charsets.UTF_8))
        val payload = iv + encrypted
        check(preferences.edit().putString(TOKEN_KEY, Base64.encodeToString(payload, Base64.NO_WRAP)).commit()) {
            "Unable to save encrypted token"
        }
    }

    fun clear() {
        preferences.edit().remove(TOKEN_KEY).commit()
        try {
            val keyStore = KeyStore.getInstance(ANDROID_KEY_STORE)
            keyStore.load(null)
            keyStore.deleteEntry(KEY_ALIAS)
        } catch (_: Exception) {
        }
    }

    private fun getOrCreateKey(): SecretKey {
        val keyStore = KeyStore.getInstance(ANDROID_KEY_STORE)
        keyStore.load(null)
        (keyStore.getKey(KEY_ALIAS, null) as? SecretKey)?.let { return it }
        val generator = KeyGenerator.getInstance(KeyProperties.KEY_ALGORITHM_AES, ANDROID_KEY_STORE)
        val spec = KeyGenParameterSpec.Builder(
            KEY_ALIAS,
            KeyProperties.PURPOSE_ENCRYPT or KeyProperties.PURPOSE_DECRYPT
        )
            .setBlockModes(KeyProperties.BLOCK_MODE_GCM)
            .setEncryptionPaddings(KeyProperties.ENCRYPTION_PADDING_NONE)
            .setRandomizedEncryptionRequired(true)
            .build()
        generator.init(spec)
        return generator.generateKey()
    }

    private companion object {
        const val PREFERENCES_NAME = "backend_session_secure_v1"
        const val TOKEN_KEY = "encrypted_bearer_token"
        const val KEY_ALIAS = "ai_auto_trader_backend_token_aes_v1"
        const val ANDROID_KEY_STORE = "AndroidKeyStore"
        const val TRANSFORMATION = "AES/GCM/NoPadding"
        const val IV_BYTES = 12
        const val TAG_BITS = 128
    }
}
