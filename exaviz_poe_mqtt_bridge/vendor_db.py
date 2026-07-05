"""MAC OUI → manufacturer lookup for connected PoE devices.

DERIVED FROM Exaviz's official ha-poe-plugin
(custom_components/exaviz/device_identifier.py) — same curated table of
vendors commonly found on PoE ports (IP cameras especially) and the same
matching logic.  One upstream duplicate ("00:50:56" listed as both VMware
and Cisco) is resolved in favour of VMware, the registered holder.
"""
from __future__ import annotations

MAC_VENDOR_DB: dict[str, str] = {
    "00:1D:0F": "Ubiquiti Networks",
    "00:27:22": "Ubiquiti Networks",
    "24:5A:4C": "Ubiquiti Networks",
    "74:83:C2": "Ubiquiti Networks",
    "F0:9F:C2": "Ubiquiti Networks",
    "00:04:20": "Axis Communications (Camera)",
    "00:40:8C": "Axis Communications (Camera)",
    "AC:CC:8E": "Axis Communications (Camera)",
    "00:50:C2": "Axis Communications (Camera)",
    "B8:A4:4F": "Axis Communications (Camera)",
    "00:13:E2": "GeoVision (Camera)",
    "E4:30:22": "Hanwha Vision (Wisenet Camera)",
    "00:09:57": "Hanwha Vision (Wisenet Camera)",
    "00:D0:3E": "Hanwha Techwin (Wisenet Camera)",
    "00:07:5F": "VCS Video Communication Systems (Camera)",
    "5C:F2:07": "Speco Technologies (Camera)",
    "00:01:31": "Bosch Security Systems (Camera)",
    "00:04:63": "Bosch Security Systems (Camera)",
    "00:10:17": "Bosch Security Systems (Camera)",
    "00:1B:86": "Bosch Security Systems (Camera)",
    "00:1C:44": "Bosch Security Systems (Camera)",
    "00:0C:29": "VMware Virtual",
    "00:50:56": "VMware Virtual",
    "08:00:27": "VirtualBox Virtual",
    "00:15:5D": "Microsoft Hyper-V",
    "00:1B:21": "Intel Corporate",
    "00:1E:67": "Intel Corporate",
    "00:25:90": "Intel Corporate",
    "00:0D:B9": "Raspberry Pi Trading",
    "B8:27:EB": "Raspberry Pi Foundation",
    "DC:A6:32": "Raspberry Pi Trading",
    "E4:5F:01": "Raspberry Pi Trading",
    "00:1C:42": "Parallels Virtual",
    "00:11:32": "Synology",
    "00:D0:41": "TP-Link Technologies",
    "50:C7:BF": "TP-Link Technologies",
    "A0:F3:C1": "TP-Link Technologies",
    "00:03:7F": "Atheros Communications",
    "00:1B:63": "Apple",
    "00:26:B0": "Apple",
    "00:3E:E1": "Apple",
    "04:26:65": "Apple",
    "D4:61:9D": "Apple",
    "00:1C:B3": "Netgear",
    "00:26:F2": "Netgear",
    "28:C6:8E": "Netgear",
    "00:14:6C": "Cisco Systems",
    "00:18:B9": "Cisco Systems",
    "00:1D:70": "Cisco Systems",
    "00:22:90": "Cisco Systems",
}


def get_mac_vendor(mac_address: str | None) -> str:
    """Look up manufacturer from a MAC address OUI (first 3 bytes).

    Returns "Unknown" when the OUI is not in the table.
    """
    if not mac_address or len(mac_address) < 8:
        return "Unknown"

    oui = mac_address[:8].upper()
    vendor = MAC_VENDOR_DB.get(oui)
    if vendor:
        return vendor

    # Same fallback as upstream: match the first 2 bytes if the exact
    # 3-byte OUI is not present.
    for known_oui, manufacturer in MAC_VENDOR_DB.items():
        if oui.startswith(known_oui[:5]):
            return f"{manufacturer} (partial match)"

    return "Unknown"
