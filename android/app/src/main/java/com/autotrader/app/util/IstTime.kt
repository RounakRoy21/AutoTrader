package com.autotrader.app.util

import java.time.ZoneId
import java.time.ZonedDateTime
import java.time.format.DateTimeFormatter

/**
 * Central IST time helpers. Every scheduling / timestamp decision in the port must
 * go through [IST] so behaviour matches the Python backend (Asia/Kolkata) exactly.
 */
object IstTime {
    val ZONE: ZoneId = ZoneId.of("Asia/Kolkata")

    private val HMS: DateTimeFormatter = DateTimeFormatter.ofPattern("HH:mm:ss")
    private val FULL: DateTimeFormatter = DateTimeFormatter.ofPattern("yyyy-MM-dd HH:mm:ss")

    fun now(): ZonedDateTime = ZonedDateTime.now(ZONE)

    fun hms(t: ZonedDateTime = now()): String = t.format(HMS)

    fun full(t: ZonedDateTime = now()): String = t.format(FULL)
}
