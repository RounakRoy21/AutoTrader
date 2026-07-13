# AutoTrader Android — Phase 0 PoC

On-device Kotlin port of the AutoTrader NSE intraday system.
See [../trading-system/ANDROID_MIGRATION_PLAN.md](../trading-system/ANDROID_MIGRATION_PLAN.md)
for the full plan and [../trading-system/ANDROID_MIGRATION_CHECKLIST.md](../trading-system/ANDROID_MIGRATION_CHECKLIST.md)
for the measurable acceptance criteria.

> **This module is Phase 0 only** — a de-risking proof of concept. It contains **no trading
> logic**. Its sole purpose is to answer two go/no-go questions on the actual OnePlus Nord:
>
> 1. **Background survival** — can a foreground service run the 09:15–15:30 IST window with the
>    screen off without OxygenOS killing it?
> 2. **Groww on-device** — can a Kotlin client mint a TOTP access token and pull batched LTP?
>
> If either gate fails, revisit the approach *before* porting the trading brain (Phases 1–5).

---

## What's here

| Piece | File | Proves |
|---|---|---|
| Foreground heartbeat service | `service/TradingService.kt` | Background survival (logs a beat every 5 s) |
| Boot restart | `service/BootReceiver.kt` | Auto-restart after reboot |
| TOTP generator | `security/Totp.kt` | RFC 6238 codes matching `pyotp` (unit-tested) |
| Encrypted secret store | `security/SecurePrefs.kt` | API key + TOTP secret never stored in plaintext |
| Groww client (token + LTP) | `groww/GrowwRestClient.kt` | On-device auth + live data |
| Control panel | `MainActivity.kt` | Manual triggers for the two gates |

---

## Build

Requires JDK 17+ and the Android SDK (platform 34). Android Studio provides both.

```powershell
# From the android/ directory:
.\gradlew.bat :app:assembleDebug        # build debug APK
.\gradlew.bat :app:testDebugUnitTest    # run unit tests (TOTP parity)
```

Output APK: `app/build/outputs/apk/debug/app-debug.apk`

> `local.properties` (SDK path) is generated locally and git-ignored. If you open in Android
> Studio it is created automatically; otherwise set `sdk.dir=` to your SDK path.

---

## Install & run on the OnePlus Nord

1. Enable **Developer options** → **USB debugging** on the phone; connect via USB.
2. Install:
   ```powershell
   adb install -r app\build\outputs\apk\debug\app-debug.apk
   ```
3. Open the app. Grant the **notifications** permission when prompted.

### Gate 1 — Background survival (checklist 0.2)

1. Tap **Request battery-optimization exemption** → allow.
   - On OxygenOS also do this manually: **Settings → Apps → AutoTrader → Battery →**
     set **Allow background activity** / disable **"Optimize"** / disable **"Sleep standby
     optimization"**; then lock the app in the recents list.
2. Tap **Start heartbeat service**. A persistent "AutoTrader running" notification appears.
3. Turn the screen off and leave it for a full session (or force Doze to accelerate the test).

Watch the heartbeat from a connected machine:
```powershell
adb logcat -s AutoTrader
# expect:  heartbeat #N @ HH:mm:ss IST (elapsed M.m m)   every ~5 s
```

Force Doze (accelerated survival check):
```powershell
adb shell dumpsys deviceidle force-idle
adb shell dumpsys deviceidle unforce
adb shell dumpsys deviceidle whitelist | findstr com.autotrader.app   # confirm exemption
```

**PASS:** across the whole window the heartbeat count ≈ `elapsed_seconds / 5`, with **no gap
> 30 s**. Repeat on a second day. Record the result in the checklist.

### Gate 2 — Groww auth + LTP (checklist 0.3)

1. Enter your **Groww API key** (client id) and **TOTP secret** (base32), tap **Save credentials
   (encrypted)**. The secret field clears and is stored in EncryptedSharedPreferences.
2. Tap **Test Groww: mint token + fetch LTP**. The output panel shows:
   - `TOTP generated (6-digit).`
   - `Token minted OK (len=NN).`
   - `LTP OK (6 symbols):` with a live price per focus symbol (during market hours).

**PASS:** token mint returns 200 and LTP returns numeric prices on the device's network.
(Outside market hours prices may be last-close; an empty result is noted in the panel.)

---

## Notes / decisions

- **FGS type = `specialUse`** (not `dataSync`): `dataSync` foreground services are capped at
  ~6 h/day on Android 15, which would kill the ~6h15m session. `specialUse` is appropriate for a
  sideloaded, always-on personal automation. This is a key thing Phase 0 validates on the device's
  actual OS version.
- **Wake lock:** the service holds a `PARTIAL_WAKE_LOCK` for the session. Phase 0 measures whether
  wake lock + battery exemption is enough on OxygenOS.
- **Secrets:** stored only via `SecurePrefs` (AES-256, Keystore-backed). Never logged, never in
  `BuildConfig`.
- **Package:** `com.autotrader.app`. **minSdk 26, targetSdk/compileSdk 34.**

---

## Verified locally (build machine)

- `:app:assembleDebug` → `app-debug.apk` (~7.4 MB) ✅
- `:app:testDebugUnitTest` → TOTP RFC 6238 parity test passes ✅
- On-device gates (0.2, 0.3) → **pending run on the OnePlus Nord.**
