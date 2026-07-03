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
