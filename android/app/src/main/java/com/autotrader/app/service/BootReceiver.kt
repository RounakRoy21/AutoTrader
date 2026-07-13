package com.autotrader.app.service

import android.content.BroadcastReceiver
import android.content.Context
import android.content.Intent
import android.util.Log

/**
 * Restarts the foreground service after a device reboot so the trading loop
 * re-arms without the user reopening the app.
 *
 * Phase 0: optional (checklist 0.2, last item). Later phases will re-arm the
 * AlarmManager schedule here as well.
 */
class BootReceiver : BroadcastReceiver() {
    override fun onReceive(context: Context, intent: Intent?) {
        val action = intent?.action ?: return
        if (action == Intent.ACTION_BOOT_COMPLETED || action == Intent.ACTION_LOCKED_BOOT_COMPLETED) {
            Log.i("AutoTrader", "BOOT_COMPLETED received — restarting TradingService")
            TradingService.start(context)
        }
    }
}
