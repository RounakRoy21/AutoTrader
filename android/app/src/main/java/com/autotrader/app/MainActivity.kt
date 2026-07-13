package com.autotrader.app

import android.Manifest
import android.annotation.SuppressLint
import android.content.Intent
import android.content.pm.PackageManager
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.PowerManager
import android.provider.Settings
import androidx.activity.result.contract.ActivityResultContracts
import androidx.appcompat.app.AppCompatActivity
import androidx.core.content.ContextCompat
import androidx.lifecycle.lifecycleScope
import com.autotrader.app.databinding.ActivityMainBinding
import com.autotrader.app.groww.GrowwRestClient
import com.autotrader.app.groww.LtpResult
import com.autotrader.app.groww.TokenResult
import com.autotrader.app.security.SecurePrefs
import com.autotrader.app.security.Totp
import com.autotrader.app.service.TradingService
import com.autotrader.app.util.IstTime
import kotlinx.coroutines.launch

/**
 * Phase 0 control panel. Not a real dashboard — just the manual triggers needed to
 * verify the two PoC gates: background survival and Groww auth + LTP on-device.
 */
class MainActivity : AppCompatActivity() {

    private lateinit var binding: ActivityMainBinding
    private lateinit var prefs: SecurePrefs
    private val groww = GrowwRestClient()

    // Focus universe for the LTP smoke test (mirrors config.focus_stocks).
    private val focusSymbols = listOf(
        "NSE_RELIANCE", "NSE_HDFCBANK", "NSE_INFY", "NSE_TCS", "NSE_ICICIBANK", "NSE_BHARTIARTL",
    )

    private val requestNotifications = registerForActivityResult(
        ActivityResultContracts.RequestPermission()
    ) { granted -> log(if (granted) "Notifications permission granted" else "Notifications permission denied") }

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        binding = ActivityMainBinding.inflate(layoutInflater)
        setContentView(binding.root)
        prefs = SecurePrefs(this)

        // Prefill the API key (never the secret) so the field shows the saved value.
        binding.inputApiKey.setText(prefs.apiKey)

        binding.btnSaveCreds.setOnClickListener { saveCredentials() }
        binding.btnStartService.setOnClickListener { startService() }
        binding.btnStopService.setOnClickListener {
            TradingService.stop(this)
            log("Stop requested.")
        }
        binding.btnBattery.setOnClickListener { requestBatteryExemption() }
        binding.btnTestGroww.setOnClickListener { testGroww() }

        maybeRequestNotifications()
        log("Ready @ ${IstTime.full()} IST")
    }

    private fun saveCredentials() {
        val apiKey = binding.inputApiKey.text?.toString()?.trim().orEmpty()
        val secret = binding.inputTotpSecret.text?.toString()?.trim().orEmpty()
        if (apiKey.isBlank() || secret.isBlank()) {
            log("Enter both API key and TOTP secret before saving.")
            return
        }
        prefs.apiKey = apiKey
        prefs.totpSecret = secret
        binding.inputTotpSecret.setText("")  // do not keep the secret on screen
        log("Credentials saved (encrypted). Secret field cleared.")
    }

    private fun startService() {
        maybeRequestNotifications()
        TradingService.start(this)
        log("Heartbeat service started. Watch: adb logcat -s AutoTrader")
    }

    @SuppressLint("BatteryLife")
    private fun requestBatteryExemption() {
        val pm = getSystemService(PowerManager::class.java)
        if (pm.isIgnoringBatteryOptimizations(packageName)) {
            log("Already exempt from battery optimization.")
            return
        }
        val intent = Intent(Settings.ACTION_REQUEST_IGNORE_BATTERY_OPTIMIZATIONS).apply {
            data = Uri.parse("package:$packageName")
        }
        runCatching { startActivity(intent) }.onFailure {
            // Fallback to the settings list if the direct dialog is unavailable.
            startActivity(Intent(Settings.ACTION_IGNORE_BATTERY_OPTIMIZATION_SETTINGS))
        }
        log("Requested battery-optimization exemption.")
    }

    private fun testGroww() {
        if (!prefs.hasCredentials()) {
            log("Save credentials first.")
            return
        }
        log("Groww test starting @ ${IstTime.hms()} IST…")
        lifecycleScope.launch {
            // 1. Generate TOTP from the stored base32 secret.
            val code = runCatching { Totp.now(prefs.totpSecret) }.getOrElse {
                log("TOTP generation failed: ${it.message}")
                return@launch
            }
            log("TOTP generated (6-digit).")

            // 2. Mint the access token.
            when (val tok = groww.mintAccessToken(prefs.apiKey, code)) {
                is TokenResult.Failure -> {
                    log("Token mint FAILED: HTTP ${tok.httpCode} — ${tok.message}")
                    return@launch
                }
                is TokenResult.Success -> log("Token minted OK (len=${tok.token.length}).")
            }

            // 3. Batched LTP fetch for the focus universe.
            when (val ltp = groww.getLtp(focusSymbols)) {
                is LtpResult.Failure -> log("LTP FAILED: HTTP ${ltp.httpCode} — ${ltp.message}")
                is LtpResult.Success -> {
                    if (ltp.prices.isEmpty()) {
                        log("LTP returned OK but empty (market closed or no entitlement).")
                    } else {
                        val rendered = ltp.prices.entries.joinToString("\n") { "  ${it.key} = ${it.value}" }
                        log("LTP OK (${ltp.prices.size} symbols):\n$rendered")
                    }
                }
            }
        }
    }

    private fun maybeRequestNotifications() {
        if (Build.VERSION.SDK_INT >= Build.VERSION_CODES.TIRAMISU) {
            val granted = ContextCompat.checkSelfPermission(
                this, Manifest.permission.POST_NOTIFICATIONS
            ) == PackageManager.PERMISSION_GRANTED
            if (!granted) requestNotifications.launch(Manifest.permission.POST_NOTIFICATIONS)
        }
    }

    /** Append a line to the on-screen log (newest at the bottom). */
    private fun log(msg: String) {
        val current = binding.outputLog.text?.toString().orEmpty()
        binding.outputLog.text = if (current.isBlank() || current == "Ready.") msg else "$current\n$msg"
    }
}
