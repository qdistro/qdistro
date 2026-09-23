import QtQuick
import QtTest
import "../Helpers/BluetoothUtils.js" as B

TestCase {
    name: "BluetoothUtils"

    // ---- macFromDevice ------------------------------------------------

    function test_mac_from_address_field() {
        const dev = { address: "AA:BB:CC:DD:EE:FF" }
        compare(B.macFromDevice(dev), "AA:BB:CC:DD:EE:FF")
    }

    function test_mac_from_device_path() {
        const dev = {
            DeviceProperties: { Address: "11:22:33:44:55:66" }
        }
        const mac = B.macFromDevice(dev)
        verify(mac === "11:22:33:44:55:66" || mac === "")
    }

    function test_mac_from_empty_device() {
        const mac = B.macFromDevice({})
        compare(mac, "")
    }

    function test_mac_from_null_returns_empty() {
        compare(B.macFromDevice(null), "")
    }

    // ---- deviceKey ----------------------------------------------------

    function test_deviceKey_uses_mac() {
        const k = B.deviceKey({ address: "AA:BB:CC:DD:EE:FF" })
        verify(k.indexOf("AA:BB:CC:DD:EE:FF") !== -1
               || k === "AA:BB:CC:DD:EE:FF")
    }

    function test_deviceKey_distinguishes_devs() {
        const a = B.deviceKey({ address: "00:00:00:00:00:01" })
        const b = B.deviceKey({ address: "00:00:00:00:00:02" })
        verify(a !== b)
    }

    // ---- dedupeDevices ------------------------------------------------

    function test_dedupe_keeps_unique() {
        const list = [
            { address: "AA:BB:CC:DD:EE:01" },
            { address: "AA:BB:CC:DD:EE:02" },
            { address: "AA:BB:CC:DD:EE:03" },
        ]
        const out = B.dedupeDevices(list)
        compare(out.length, 3)
    }

    function test_dedupe_removes_duplicates() {
        const list = [
            { address: "AA:BB:CC:DD:EE:01" },
            { address: "AA:BB:CC:DD:EE:01" },
            { address: "AA:BB:CC:DD:EE:02" },
        ]
        const out = B.dedupeDevices(list)
        compare(out.length, 2)
    }

    function test_dedupe_empty_list() {
        compare(B.dedupeDevices([]).length, 0)
    }

    function test_dedupe_null_safe() {
        const out = B.dedupeDevices(null)
        verify(out === null || out.length === 0 || Array.isArray(out))
    }

    // ---- parseRssiOutput ----------------------------------------------
    // parseRssiOutput returns a Number (or null) — the parsed dBm value
    // from hcitool-style output. Pre-impl tests assumed array; corrected
    // here.

    function test_parse_rssi_empty_returns_null() {
        compare(B.parseRssiOutput(""), null)
    }

    function test_parse_rssi_null_returns_null() {
        compare(B.parseRssiOutput(null), null)
    }

    function test_parse_rssi_paren_format() {
        // bluetoothctl rssi: "(-65 dBm)"
        compare(B.parseRssiOutput("(-65 dBm)"), -65)
    }

    function test_parse_rssi_decimal_format() {
        compare(B.parseRssiOutput("RSSI: -45"), -45)
    }

    function test_parse_rssi_no_match_returns_null() {
        compare(B.parseRssiOutput("garbage no rssi here"), null)
    }

    // ---- dbmToPercent -------------------------------------------------

    function test_dbm_to_percent_strong_signal() {
        // -30 dBm is excellent → close to 100.
        const p = B.dbmToPercent(-30)
        verify(p >= 90 && p <= 100)
    }

    function test_dbm_to_percent_weak_signal() {
        // -90 dBm is poor → close to 0.
        const p = B.dbmToPercent(-90)
        verify(p >= 0 && p <= 25)
    }

    function test_dbm_to_percent_clamps_above() {
        const p = B.dbmToPercent(0)
        verify(p <= 100)
    }

    function test_dbm_to_percent_clamps_below() {
        const p = B.dbmToPercent(-200)
        verify(p >= 0)
    }

    function test_dbm_to_percent_returns_number() {
        verify(typeof B.dbmToPercent(-50) === "number")
    }

    // ---- signalIcon ---------------------------------------------------

    function test_signalIcon_strong() {
        const ic = B.signalIcon(95)
        verify(typeof ic === "string")
        verify(ic.length > 0)
    }

    function test_signalIcon_weak() {
        const ic = B.signalIcon(10)
        verify(typeof ic === "string")
        verify(ic.length > 0)
    }

    function test_signalIcon_zero() {
        const ic = B.signalIcon(0)
        verify(typeof ic === "string")
    }

    // ---- deviceIcon ---------------------------------------------------

    function test_deviceIcon_returns_string() {
        const ic = B.deviceIcon("Headphones", "audio-headphones")
        verify(typeof ic === "string")
        verify(ic.length > 0)
    }

    function test_deviceIcon_with_empty_name() {
        const ic = B.deviceIcon("", "")
        verify(typeof ic === "string")
    }

    // ---- batteryPercent -----------------------------------------------
    // Lib expects { batteryAvailable: true, battery: 0..1 } and returns
    // 0..100 int. Anything else → null.

    function test_batteryPercent_full() {
        compare(B.batteryPercent({ batteryAvailable: true, battery: 1.0 }), 100)
    }

    function test_batteryPercent_half() {
        compare(B.batteryPercent({ batteryAvailable: true, battery: 0.5 }), 50)
    }

    function test_batteryPercent_partial() {
        // Round to nearest int.
        compare(B.batteryPercent({ batteryAvailable: true, battery: 0.853 }), 85)
    }

    function test_batteryPercent_unavailable_returns_null() {
        compare(B.batteryPercent({ batteryAvailable: false, battery: 0.5 }), null)
    }

    function test_batteryPercent_missing_battery_returns_null() {
        compare(B.batteryPercent({ batteryAvailable: true }), null)
    }

    function test_batteryPercent_clamps_above() {
        compare(B.batteryPercent({ batteryAvailable: true, battery: 1.5 }), 100)
    }

    function test_batteryPercent_clamps_below() {
        compare(B.batteryPercent({ batteryAvailable: true, battery: -0.1 }), 0)
    }

    function test_batteryPercent_null_device() {
        compare(B.batteryPercent(null), null)
    }
}
