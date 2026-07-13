package com.autotrader.app.service

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.Service
import android.content.Context
import android.content.Intent
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import android.util.Log
import androidx.core.app.NotificationCompat
import com.autotrader.app.MainActivity
import com.autotrader.app.R
import com.autotrader.app.util.IstTime
import kotlinx.coroutines.CoroutineScope
import kotlinx.coroutines.Dispatchers
import kotlinx.coroutines.Job
import kotlinx.coroutines.SupervisorJob
import kotlinx.coroutines.cancel
import kotlinx.coroutines.delay
import kotlinx.coroutines.isActive
import kotlinx.coroutines.launch

/**
 * Phase 0 background-survival probe.
 *
 * A foreground service that logs a heartbeat every [HEARTBEAT_SECONDS] with an IST
 * timestamp. Its ONLY job in Phase 0 is to prove the process survives a full
 * 09:15–15:30 IST session with the screen off on the OnePlus Nord (see
 * ANDROID_MIGRATION_CHECKLIST.md 0.2). No trading logic lives here yet.
 *
 * Verify with:  adb logcat -s AutoTrader
 * Expect:       heartbeat #N @ HH:mm:ss IST   at ~5 s cadence, no gap > 30 s.
 */
class TradingService : Service() {

    private val scope = CoroutineScope(SupervisorJob() + Dispatchers.Default)
    private var loopJob: Job? = null
    private var wakeLock: PowerManager.WakeLock? = null
    private var heartbeatCount = 0L
    private var startedAt = IstTime.now()

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        createChannel()
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        startedAt = IstTime.now()
        startForeground(NOTIF_ID, buildNotification("starting…"))
        acquireWakeLock()
        startHeartbeat()
        Log.i(TAG, "TradingService started @ ${IstTime.full(startedAt)} IST")
        // STICKY: the OS should recreate the service if it is killed for memory.
        return START_STICKY
    }

    private fun startHeartbeat() {
        if (loopJob?.isActive == true) return
        loopJob = scope.launch {
            while (isActive) {
                heartbeatCount++
                val now = IstTime.now()
                val elapsedMin = (now.toEpochSecond() - startedAt.toEpochSecond()) / 60.0
                Log.i(TAG, "heartbeat #$heartbeatCount @ ${IstTime.hms(now)} IST (elapsed ${"%.1f".format(elapsedMin)}m)")
                updateNotification(
                    "beat #$heartbeatCount @ ${IstTime.hms(now)} IST · ${"%.0f".format(elapsedMin)}m uptime"
                )
                delay(HEARTBEAT_SECONDS * 1000L)
            }
        }
    }

    private fun acquireWakeLock() {
        if (wakeLock?.isHeld == true) return
        val pm = getSystemService(Context.POWER_SERVICE) as PowerManager
        wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "AutoTrader::TradingWakeLock").apply {
            setReferenceCounted(false)
            acquire(SESSION_WAKELOCK_MS)
        }
    }

    private fun releaseWakeLock() {
        wakeLock?.let { if (it.isHeld) it.release() }
        wakeLock = null
    }

    override fun onDestroy() {
        Log.i(TAG, "TradingService stopping @ ${IstTime.hms()} IST after $heartbeatCount beats")
        releaseWakeLock()
        scope.cancel()
        super.onDestroy()
    }

    // ── Notification ──────────────────────────────────────────────────────────

    private fun createChannel() {
        val mgr = getSystemService(NotificationManager::class.java)
        val channel = NotificationChannel(
            CHANNEL_ID,
            "AutoTrader session",
            NotificationManager.IMPORTANCE_LOW,
        ).apply {
            description = "Keeps the trading loop alive during market hours."
            setShowBadge(false)
        }
        mgr.createNotificationChannel(channel)
    }

    private fun buildNotification(text: String): Notification {
        val tapIntent = Intent(this, MainActivity::class.java).apply {
            flags = Intent.FLAG_ACTIVITY_SINGLE_TOP or Intent.FLAG_ACTIVITY_CLEAR_TOP
        }
        val pending = android.app.PendingIntent.getActivity(
            this, 0, tapIntent,
            android.app.PendingIntent.FLAG_IMMUTABLE or android.app.PendingIntent.FLAG_UPDATE_CURRENT,
        )
        return NotificationCompat.Builder(this, CHANNEL_ID)
            .setContentTitle("AutoTrader running")
            .setContentText(text)
            .setSmallIcon(R.drawable.ic_stat_trading)
            .setOngoing(true)
            .setOnlyAlertOnce(true)
            .setContentIntent(pending)
            .setForegroundServiceBehavior(NotificationCompat.FOREGROUND_SERVICE_IMMEDIATE)
            .build()
    }

    private fun updateNotification(text: String) {
        val mgr = getSystemService(NotificationManager::class.java)
        mgr.notify(NOTIF_ID, buildNotification(text))
    }

    companion object {
        private const val TAG = "AutoTrader"
        private const val CHANNEL_ID = "autotrader_session"
        private const val NOTIF_ID = 1001
        private const val HEARTBEAT_SECONDS = 5L

        // Hold a partial wake lock for a full session + margin. Phase 0 measures
        // whether this (plus battery exemption) is sufficient on OxygenOS.
        private const val SESSION_WAKELOCK_MS = 7L * 60 * 60 * 1000  // 7 hours

        fun start(context: Context) {
            val intent = Intent(context, TradingService::class.java)
            if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.O) {
                context.startForegroundService(intent)
            } else {
                context.startService(intent)
            }
        }

        fun stop(context: Context) {
            context.stopService(Intent(context, TradingService::class.java))
        }
    }
}
