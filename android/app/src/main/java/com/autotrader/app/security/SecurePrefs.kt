package com.autotrader.app.security

import android.content.Context
import android.content.SharedPreferences
import androidx.security.crypto.EncryptedSharedPreferences
import androidx.security.crypto.MasterKey

/**
 * Encrypted key/value store for on-device secrets (Groww API key + TOTP secret).
 *
 * Backed by Jetpack Security [EncryptedSharedPreferences] (AES-256 GCM), with the
 * master key held in the Android Keystore. This is the single place secrets are
 * read/written — no secret should ever be logged or placed in BuildConfig.
 */
class SecurePrefs(context: Context) {

    private val prefs: SharedPreferences by lazy {
        val masterKey = MasterKey.Builder(context)
            .setKeyScheme(MasterKey.KeyScheme.AES256_GCM)
            .build()
        EncryptedSharedPreferences.create(
            context,
            "autotrader_secure_prefs",
            masterKey,
            EncryptedSharedPreferences.PrefKeyEncryptionScheme.AES256_SIV,
            EncryptedSharedPreferences.PrefValueEncryptionScheme.AES256_GCM,
        )
    }

    var apiKey: String
        get() = prefs.getString(KEY_API, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_API, value).apply()

    var totpSecret: String
        get() = prefs.getString(KEY_TOTP, "").orEmpty()
        set(value) = prefs.edit().putString(KEY_TOTP, value).apply()

    fun hasCredentials(): Boolean = apiKey.isNotBlank() && totpSecret.isNotBlank()

    companion object {
        private const val KEY_API = "groww_api_key"
        private const val KEY_TOTP = "groww_totp_secret"
    }
}
