"""Tests for switch-mode (bridged) connected-device detection."""
from __future__ import annotations

from exaviz_poe_mqtt_bridge.poe import parse_fdb_macs, parse_neigh_for_macs

FDB = """\
33:33:00:00:00:01 dev poe0 self permanent
01:00:5e:00:00:01 dev poe0 self permanent
88:a2:9e:43:d3:3b dev poe0 vlan 1 master br0 permanent
88:a2:9e:43:d3:3b dev poe0 master br0 permanent
d0:3b:f4:03:a6:f1 dev poe0 master br0
d0:3b:f4:03:a6:f1 dev poe0 self
aa:bb:cc:dd:ee:ff dev poe3 master br0
76:69:7a:00:00:65 dev wan master br0 permanent
52:54:00:67:95:7d dev vnet1 master br0
"""

NEIGH = """\
10.0.4.1 lladdr 0e:ea:14:48:ed:2e REACHABLE
10.0.4.55 lladdr d0:3b:f4:03:a6:f1 STALE
10.0.4.13 lladdr 50:a6:d8:b2:0f:5f REACHABLE
"""


class TestParseFdbMacs:
    def test_learned_mac_extracted(self):
        assert parse_fdb_macs(FDB, "poe0") == ["d0:3b:f4:03:a6:f1"]

    def test_permanent_and_self_excluded(self):
        macs = parse_fdb_macs(FDB, "wan")
        assert macs == []  # only a permanent entry on wan

    def test_other_ports_not_leaked(self):
        assert parse_fdb_macs(FDB, "poe3") == ["aa:bb:cc:dd:ee:ff"]
        assert parse_fdb_macs(FDB, "poe1") == []

    def test_garbage_lines_ignored(self):
        assert parse_fdb_macs("garbage\n\ndev poe0\n", "poe0") == []


class TestParseCaptureSrcIp:
    def test_ip_packet_source(self):
        from exaviz_poe_mqtt_bridge.poe import parse_capture_src_ip

        capture = (
            "10:41:22.42 IP 10.0.4.212.52034 > 10.0.4.102.53: 73+ AAAA? x. (34)\n"
            "10:41:22.44 IP 10.0.4.212.58932 > 51.145.123.29.123: NTPv4, Client\n"
        )
        assert parse_capture_src_ip(capture) == "10.0.4.212"

    def test_arp_tell_source(self):
        from exaviz_poe_mqtt_bridge.poe import parse_capture_src_ip

        capture = "10:40:38.28 ARP, Request who-has 10.0.4.1 tell 10.0.4.212, length 46\n"
        assert parse_capture_src_ip(capture) == "10.0.4.212"

    def test_dhcp_discover_zero_ip_skipped(self):
        from exaviz_poe_mqtt_bridge.poe import parse_capture_src_ip

        capture = (
            "10:40:00.00 IP 0.0.0.0.68 > 255.255.255.255.67: BOOTP/DHCP, Request\n"
            "10:40:01.00 IP 10.0.4.212.68 > 10.0.4.1.67: BOOTP/DHCP, Request\n"
        )
        assert parse_capture_src_ip(capture) == "10.0.4.212"

    def test_ipv6_and_garbage_ignored(self):
        from exaviz_poe_mqtt_bridge.poe import parse_capture_src_ip

        capture = (
            "10:40:38.11 IP6 fe80::babe > ff02::2: ICMP6, router solicitation\n"
            "garbage line\n"
        )
        assert parse_capture_src_ip(capture) is None


class TestParseArpScan:
    def test_mac_ip_map(self):
        from exaviz_poe_mqtt_bridge.poe import parse_arp_scan

        scan = (
            "Interface: br0, type: EN10MB, MAC: ae:10:23:54:11:46, IPv4: 10.0.4.213\n"
            "Starting arp-scan 1.10.0 with 256 hosts\n"
            "10.0.4.1\t0e:ea:14:48:ed:2e\tUnknown vendor\n"
            "10.0.4.212\tD0:3B:F4:03:A6:F1\t(Unknown)\n"
            "\n"
            "3 packets received by filter, 0 packets dropped by kernel\n"
        )
        result = parse_arp_scan(scan)
        assert result["d0:3b:f4:03:a6:f1"] == "10.0.4.212"
        assert result["0e:ea:14:48:ed:2e"] == "10.0.4.1"
        assert len(result) == 2

    def test_empty_or_garbage(self):
        from exaviz_poe_mqtt_bridge.poe import parse_arp_scan

        assert parse_arp_scan("") == {}
        assert parse_arp_scan("no devices found\n") == {}


class TestParseNeighForMacs:
    def test_ip_resolved_for_learned_mac(self):
        device = parse_neigh_for_macs(NEIGH, ["d0:3b:f4:03:a6:f1"])
        assert device == {
            "ip_address": "10.0.4.55",
            "mac_address": "d0:3b:f4:03:a6:f1",
            "arp_state": "STALE",
        }

    def test_no_match_returns_none(self):
        assert parse_neigh_for_macs(NEIGH, ["00:00:00:00:00:99"]) is None

    def test_does_not_match_other_devices(self):
        # The gateway/other hosts on the bridge must not be attributed
        # to this port.
        device = parse_neigh_for_macs(NEIGH, ["aa:bb:cc:dd:ee:ff"])
        assert device is None
