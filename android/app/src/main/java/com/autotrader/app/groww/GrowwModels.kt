package com.autotrader.app.groww

/** Result of a token mint attempt. */
sealed interface TokenResult {
    data class Success(val token: String) : TokenResult
    data class Failure(val httpCode: Int, val message: String) : TokenResult
}

/** Result of a batched LTP fetch. Keys are exchange symbols e.g. "NSE_RELIANCE". */
sealed interface LtpResult {
    data class Success(val prices: Map<String, Double>) : LtpResult
    data class Failure(val httpCode: Int, val message: String) : LtpResult
}

/** Raised when the access token is missing/expired (HTTP 401). Drives re-mint. */
class GrowwAuthException(message: String) : Exception(message)
