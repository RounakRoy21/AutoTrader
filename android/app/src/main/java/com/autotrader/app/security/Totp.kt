package com.autotrader.app.security

import java.nio.ByteBuffer
import javax.crypto.Mac
import javax.crypto.spec.SecretKeySpec

/**
 * RFC 6238 TOTP generator, matching Python `pyotp.TOTP(secret).now()` defaults:
 * HMAC-SHA1, 6 digits, 30-second time step, Base32-encoded secret.
 *
 * Parity check (Phase 0 checklist 0.3): for the same secret at the same UNIX
 * second, [now] must equal `pyotp.TOTP(secret).now()`.
 */
object Totp {

    private const val DIGITS = 6
    private const val PERIOD_SECONDS = 30L

    /** Current 6-digit TOTP code for [base32Secret]. */
    fun now(base32Secret: String, epochSeconds: Long = System.currentTimeMillis() / 1000): String {
        val counter = epochSeconds / PERIOD_SECONDS
        val key = base32Decode(base32Secret)
        return hotp(key, counter)
    }

    private fun hotp(key: ByteArray, counter: Long): String {
        val msg = ByteBuffer.allocate(8).putLong(counter).array()
        val mac = Mac.getInstance("HmacSHA1")
        mac.init(SecretKeySpec(key, "HmacSHA1"))
        val hash = mac.doFinal(msg)

        // Dynamic truncation (RFC 4226 §5.3).
        val offset = (hash[hash.size - 1].toInt() and 0x0F)
        val binary = ((hash[offset].toInt() and 0x7F) shl 24) or
            ((hash[offset + 1].toInt() and 0xFF) shl 16) or
            ((hash[offset + 2].toInt() and 0xFF) shl 8) or
            (hash[offset + 3].toInt() and 0xFF)

        val otp = binary % 1_000_000  // 10^DIGITS
        return otp.toString().padStart(DIGITS, '0')
    }

    /** Decode an RFC 4648 Base32 string (case-insensitive, padding + spaces ignored). */
    fun base32Decode(input: String): ByteArray {
        val cleaned = input.trim().replace(" ", "").replace("=", "").uppercase()
        if (cleaned.isEmpty()) return ByteArray(0)

        val out = ArrayList<Byte>(cleaned.length * 5 / 8)
        var buffer = 0
        var bitsLeft = 0
        for (c in cleaned) {
            val value = ALPHABET.indexOf(c)
            require(value >= 0) { "Invalid Base32 character: $c" }
            buffer = (buffer shl 5) or value
            bitsLeft += 5
            if (bitsLeft >= 8) {
                bitsLeft -= 8
                out.add(((buffer shr bitsLeft) and 0xFF).toByte())
            }
        }
        return out.toByteArray()
    }

    private const val ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
}
