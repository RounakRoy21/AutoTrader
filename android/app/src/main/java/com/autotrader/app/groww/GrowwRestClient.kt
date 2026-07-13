package com.autotrader.app.groww

import android.util.Log
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.withContext
import kotlinx.serialization.json.Json
import kotlinx.serialization.json.JsonObject
import kotlinx.serialization.json.JsonPrimitive
import kotlinx.serialization.json.contentOrNull
import kotlinx.serialization.json.doubleOrNull
import kotlinx.serialization.json.jsonObject
import kotlinx.serialization.json.jsonPrimitive
import okhttp3.HttpUrl.Companion.toHttpUrl
import okhttp3.MediaType.Companion.toMediaType
import okhttp3.OkHttpClient
import okhttp3.Request
import okhttp3.RequestBody.Companion.toRequestBody
import java.util.concurrent.TimeUnit

/**
 * Minimal Groww REST client for the Phase 0 PoC.
 *
 * Scope (PoC only): mint an access token via TOTP, and fetch batched LTP.
 * The full contract (orders, positions, quote, historical, instruments) is
 * Phase 1 — see ANDROID_MIGRATION_PLAN.md §9.
 *
 * Endpoint reference: /memories/repo/groww-rest-api-map.md
 *   Base URL   : https://api.groww.in
 *   Headers    : Authorization: Bearer <token>, Accept: application/json, X-API-VERSION: 1.0
 *   Token mint : POST /v1/token/api/access  (Bearer <API_KEY>, body {key_type:"totp", totp:"NNNNNN"})
 *   LTP (batch): GET  /v1/live-data/ltp?segment=CASH&exchange_symbols=NSE_RELIANCE,NSE_TCS  (<=50/call)
 */
class GrowwRestClient(
    private val http: OkHttpClient = defaultHttp(),
) {
    private val json = Json { ignoreUnknownKeys = true; isLenient = true }

    @Volatile
    private var accessToken: String? = null

    fun setAccessToken(token: String) { accessToken = token }

    fun hasToken(): Boolean = !accessToken.isNullOrBlank()

    // ── Auth ────────────────────────────────────────────────────────────────

    /**
     * Mint a daily access token. [apiKey] is the Bearer for THIS call only;
     * [totp] is the current 6-digit code from [com.autotrader.app.security.Totp].
     */
    suspend fun mintAccessToken(apiKey: String, totp: String): TokenResult = withContext(Dispatchers.IO) {
        val bodyJson = buildString {
            append("{\"key_type\":\"totp\",\"totp\":\"")
            append(totp)
            append("\"}")
        }
        val request = Request.Builder()
            .url("$BASE_URL/v1/token/api/access")
            .addHeader("Authorization", "Bearer $apiKey")
            .addHeader("Accept", "application/json")
            .addHeader("X-API-VERSION", API_VERSION)
            .post(bodyJson.toRequestBody(JSON_MEDIA))
            .build()

        try {
            http.newCall(request).execute().use { resp ->
                val raw = resp.body?.string().orEmpty()
                if (!resp.isSuccessful) {
                    return@withContext TokenResult.Failure(resp.code, raw.take(300))
                }
                val token = extractToken(raw)
                if (token.isNullOrBlank()) {
                    TokenResult.Failure(resp.code, "token field not found in response")
                } else {
                    accessToken = token
                    Log.i(TAG, "token minted, len=${token.length}")
                    TokenResult.Success(token)
                }
            }
        } catch (e: Exception) {
            TokenResult.Failure(-1, e.message ?: e.javaClass.simpleName)
        }
    }

    /** Groww wraps the token under `token` or `payload.token` depending on flow. */
    private fun extractToken(raw: String): String? {
        val root = runCatching { json.parseToJsonElement(raw).jsonObject }.getOrNull() ?: return null
        root["token"]?.jsonPrimitive?.contentOrNull?.let { return it }
        (root["payload"] as? JsonObject)?.get("token")?.jsonPrimitive?.contentOrNull?.let { return it }
        (root["data"] as? JsonObject)?.get("token")?.jsonPrimitive?.contentOrNull?.let { return it }
        return null
    }

    // ── Live data ───────────────────────────────────────────────────────────

    /**
     * Batched LTP for [exchangeSymbols] (e.g. ["NSE_RELIANCE", "NSE_TCS"]).
     * Groww accepts up to 50 symbols per call; callers should chunk beyond that.
     * Throws [GrowwAuthException] on HTTP 401 so the caller can re-mint + retry.
     */
    suspend fun getLtp(exchangeSymbols: List<String>): LtpResult = withContext(Dispatchers.IO) {
        val token = accessToken
            ?: return@withContext LtpResult.Failure(-1, "no access token")
        if (exchangeSymbols.isEmpty()) return@withContext LtpResult.Success(emptyMap())

        val url = "$BASE_URL/v1/live-data/ltp".toHttpUrl().newBuilder()
            .addQueryParameter("segment", "CASH")
            .addQueryParameter("exchange_symbols", exchangeSymbols.joinToString(","))
            .build()
        val request = Request.Builder()
            .url(url)
            .addHeader("Authorization", "Bearer $token")
            .addHeader("Accept", "application/json")
            .addHeader("X-API-VERSION", API_VERSION)
            .get()
            .build()

        try {
            http.newCall(request).execute().use { resp ->
                val raw = resp.body?.string().orEmpty()
                if (resp.code == 401) throw GrowwAuthException("401 from live-data/ltp")
                if (!resp.isSuccessful) {
                    return@withContext LtpResult.Failure(resp.code, raw.take(300))
                }
                LtpResult.Success(parseLtp(raw))
            }
        } catch (e: GrowwAuthException) {
            throw e
        } catch (e: Exception) {
            LtpResult.Failure(-1, e.message ?: e.javaClass.simpleName)
        }
    }

    /**
     * Parse the LTP payload into a flat {symbol -> price} map. Groww returns the
     * prices either at the root or under `payload`, so we probe both.
     */
    private fun parseLtp(raw: String): Map<String, Double> {
        val root = runCatching { json.parseToJsonElement(raw).jsonObject }.getOrNull() ?: return emptyMap()
        val container = (root["payload"] as? JsonObject) ?: root
        val out = LinkedHashMap<String, Double>()
        for ((k, v) in container) {
            val price = (v as? JsonPrimitive)?.doubleOrNull
            if (price != null) out[k] = price
        }
        return out
    }

    companion object {
        private const val TAG = "AutoTrader"
        private const val BASE_URL = "https://api.groww.in"
        private const val API_VERSION = "1.0"
        private val JSON_MEDIA = "application/json".toMediaType()

        fun defaultHttp(): OkHttpClient = OkHttpClient.Builder()
            .connectTimeout(10, TimeUnit.SECONDS)
            .readTimeout(15, TimeUnit.SECONDS)
            .callTimeout(20, TimeUnit.SECONDS)
            .build()
    }
}
