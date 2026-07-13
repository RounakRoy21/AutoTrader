package com.autotrader.app.security

import org.junit.Assert.assertEquals
import org.junit.Test

/**
 * RFC 6238 test vectors for HMAC-SHA1 (the mode pyotp uses by default).
 * Secret = ASCII "12345678901234567890" = Base32 "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ".
 * The RFC lists 8-digit codes; our generator emits the low 6 digits, which must
 * match `pyotp.TOTP(secret).at(epoch)` for the same instant.
 */
class TotpTest {

    private val secret = "GEZDGNBVGY3TQOJQGEZDGNBVGY3TQOJQ"

    @Test
    fun matchesRfc6238Sha1Vectors() {
        // epoch seconds -> expected low-6 of the RFC 8-digit value
        val vectors = mapOf(
            59L to "287082",          // RFC: 94287082
            1111111109L to "081804",  // RFC: 07081804
            1111111111L to "050471",  // RFC: 14050471
            1234567890L to "005924",  // RFC: 89005924
            2000000000L to "279037",  // RFC: 69279037
            20000000000L to "353130", // RFC: 65353130
        )
        for ((epoch, expected) in vectors) {
            assertEquals("epoch=$epoch", expected, Totp.now(secret, epoch))
        }
    }

    @Test
    fun base32DecodeIsCaseAndPaddingInsensitive() {
        val a = Totp.base32Decode("gezdgnbvgy3tqojq")
        val b = Totp.base32Decode("GEZDGNBVGY3TQOJQ")
        val c = Totp.base32Decode("GEZD GNBV GY3T QOJQ==")
        assertEquals(b.toList(), a.toList())
        assertEquals(b.toList(), c.toList())
    }
}
