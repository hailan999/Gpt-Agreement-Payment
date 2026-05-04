# GoPay ADB Coordinate Automation

This helper drives the normal Android UI through ADB coordinate taps. It is
intended for unlinking apps from your own GoPay account in LDPlayer/Leidian.
It does not use private GoPay APIs and does not bypass PIN, OTP, captcha, or
risk checks.

## 1. Prepare ADB

Confirm ADB can see the emulator:

```powershell
adb devices -l
```

If `adb` is not in PATH, pass the LDPlayer ADB path with `--adb`. Common
locations include `C:\leidian\LDPlayer9\adb.exe` and
`C:\LDPlayer\LDPlayer9\adb.exe`:

```powershell
python scripts/gopay_adb_unlink.py --adb "C:\leidian\LDPlayer9\adb.exe" devices
```

If LDPlayer exposes a TCP device, connect it first if needed:

```powershell
adb connect 127.0.0.1:5555
```

Then confirm the screen size:

```powershell
python scripts/gopay_adb_unlink.py --device emulator-5554 size
```

Replace `emulator-5554` with the device id shown by `adb devices -l`.

## 2. Create Your Local Coordinate Config

Copy the example config and edit only the copy:

```powershell
Copy-Item scripts/gopay_adb_coords.example.json scripts/gopay_adb_coords.json
```

Save screenshots while you calibrate:

```powershell
python scripts/gopay_adb_unlink.py --config scripts/gopay_adb_coords.json --device emulator-5554 screenshot --name account_settings
```

Open the screenshot from `output/gopay_adb_screenshots`, check the target
button position, and adjust `coords` in `scripts/gopay_adb_coords.json`.
Prefer `ratio` coordinates so the script can adapt to the actual screen size:

```json
"linked_apps": {
  "ratio": [0.43, 0.59]
}
```

## 3. Run Safe Navigation First

From the GoPay Profile page:

```powershell
python scripts/gopay_adb_unlink.py --config scripts/gopay_adb_coords.json --device emulator-5554 run-flow open_linked_apps_from_profile --execute
```

If you are already on the Account & app settings page:

```powershell
python scripts/gopay_adb_unlink.py --config scripts/gopay_adb_coords.json --device emulator-5554 run-flow open_linked_apps_from_account_settings --execute
```

## 4. Unlink One App

If you are on the Account & app settings page, use this single entrypoint to
open Linked apps and unlink the first visible app:

```powershell
python scripts/gopay_adb_unlink.py --config scripts/gopay_adb_coords.json --device emulator-5554 run-flow unlink_first_app_from_account_settings --execute --allow-unlink --yes
```

If you are already on the Linked apps list and the app has a visible `Unlink`
button on the right side, use the list-page flow:

```powershell
python scripts/gopay_adb_unlink.py --config scripts/gopay_adb_coords.json --device emulator-5554 run-flow unlink_first_app_from_list --execute --allow-unlink
```

For fully automated execution with no pause prompts, add `--yes`:

```powershell
python scripts/gopay_adb_unlink.py --config scripts/gopay_adb_coords.json --device emulator-5554 run-flow unlink_first_app_from_list --execute --allow-unlink --yes
```

The default example coordinate for `first_linked_app_unlink` is based on a
557x360 screenshot where the `Unlink` button center is around `426,184`
(`ratio: [0.765, 0.511]`). Recalibrate this coordinate against your own ADB
screenshot before running the unlink flow.

Manually enter the detail page for the app you want to unlink, then run:

```powershell
python scripts/gopay_adb_unlink.py --config scripts/gopay_adb_coords.json --device emulator-5554 run-flow unlink_current_app --execute --allow-unlink
```

The unlink flow pauses before destructive taps. If GoPay asks for PIN, OTP,
captcha, or extra verification, complete that step manually or stop the script.

## Useful Single Commands

Tap a named coordinate:

```powershell
python scripts/gopay_adb_unlink.py --config scripts/gopay_adb_coords.json --device emulator-5554 tap --coord linked_apps
```

Tap a ratio coordinate:

```powershell
python scripts/gopay_adb_unlink.py --device emulator-5554 tap --ratio 0.43 0.59
```

Swipe:

```powershell
python scripts/gopay_adb_unlink.py --device emulator-5554 swipe 285 820 285 260 --duration-ms 450
```
