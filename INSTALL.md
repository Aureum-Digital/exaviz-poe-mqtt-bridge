# Installation guide — Exaviz Cruiser CM5 + Home Assistant OS (KVM) + PoE MQTT bridge

End-to-end setup for exposing the Cruiser's PoE ports to a Home Assistant OS
VM running on the same board, using this bridge over MQTT.

```
Internet ── wan ── Ubuntu 26.04 host (Cruiser CM5)
                     ├─ /dev/pse, poe0-7            (exaviz-dkms)
                     ├─ exaviz-poe-mqtt-bridge      (this project, systemd)
                     │        │ MQTT (192.168.122.x, libvirt NAT)
                     └─ KVM ──┴─ HAOS VM
                                 ├─ NIC 1: macvtap on wan   (LAN presence: mDNS, HomeKit…)
                                 ├─ NIC 2: libvirt NAT      (host ↔ VM traffic — see §3)
                                 └─ Mosquitto add-on + MQTT integration
```

## 1. Host prerequisites

Ubuntu 26.04 on the Cruiser CM5 with the Exaviz packages from
[apt.exaviz.com](https://exa-pedia.com/docs/software/apt-repository/):

```bash
sudo apt update && sudo apt upgrade
sudo apt install --only-upgrade exaviz-dkms   # >= 1.1.14 (device-tree persistence fix)

# Ubuntu ships the Raspberry Pi kernel WITHOUT matching headers; without them
# DKMS builds against the generic kernel and the PoE modules never load:
sudo apt install linux-headers-raspi linux-headers-$(uname -r)
sudo dkms autoinstall -k $(uname -r)
sudo reboot
```

After reboot verify the PoE hardware is visible:

```bash
ls -la /dev/pse            # → symlink to ttyAMA3
ls /sys/class/net | grep poe   # → poe0 … poe7
```

## 2. Home Assistant OS VM

Install KVM and create the HAOS VM (UEFI/AAVMF, VirtIO disk & NIC, 4 vCPU,
4–8 GB RAM):

```bash
sudo apt install qemu-system-arm qemu-efi-aarch64 libvirt-daemon-system virtinst bridge-utils
sudo systemctl enable --now libvirtd
```

Give the VM its **primary NIC as macvtap** on the physical WAN interface so
HAOS appears on the LAN like a physical appliance (own DHCP lease, working
mDNS/SSDP/HomeKit):

```xml
<interface type='direct'>
    <source dev='wan' mode='bridge'/>
    <model type='virtio'/>
</interface>
```

Install the **QEMU Guest Agent add-on** inside HA and expose the virtio
channel in libvirt — it enables graceful shutdown and lets you query the
VM's IPs from the host (`virsh domifaddr haos --source agent`).

## 3. Host ↔ VM network (required for the bridge)

**macvtap isolates the host from its own guest**: external machines reach
the VM fine, but the Ubuntu host itself cannot connect to it — so the
bridge on the host could never reach a broker inside the VM. The fix is a
**second NIC on libvirt's NAT network**, used only for host↔VM traffic:

```bash
sudo virsh net-autostart default
sudo virsh net-start default 2>/dev/null || true   # may already be active
sudo virsh attach-interface haos network default --model virtio --live --config
```

The running guest kernel does not pick up the hot-plugged PCI NIC, so
**reboot HAOS once** (Settings → System → Restart → *Reboot system*). After
boot, check Settings → System → Network in HA and make sure the new
interface (e.g. `enp5s0`) is enabled with DHCP.

Find the VM's NAT address:

```bash
sudo virsh net-dhcp-leases default
# e.g. 52:54:00:46:b0:d3  ipv4  192.168.122.233/24  homeassistant
```

### Pin the VM's NAT address (recommended)

The address above is a **plain dynamic DHCP lease** from libvirt's dnsmasq
(range `192.168.122.2-254`). In practice dnsmasq tends to hand the same IP
back to the same MAC, but nothing guarantees it: the lease can rotate after
the VM is down past its expiry, after a host reboot clears dnsmasq state,
or if another VM grabs the address first.

The bridge config (§5) references this IP **by value** in `mqtt.host` — if
the VM ever comes back with a different lease, the bridge silently loses
the broker (you'd see `Cannot reach MQTT broker` warnings, and the HA
entities would eventually go stale). Pinning the lease to the VM's MAC
makes the address deterministic:

```bash
# MAC from `virsh net-dhcp-leases default` / `virsh domiflist haos`
sudo virsh net-update default add ip-dhcp-host \
  '<host mac="52:54:00:46:b0:d3" name="homeassistant" ip="192.168.122.233"/>' \
  --live --config
```

`--live` applies it to the running network, `--config` persists it across
host reboots. Verify with:

```bash
sudo virsh net-dumpxml default | grep dhcp -A 4
```

## 4. MQTT broker

Inside Home Assistant:

1. Install the **Mosquitto broker** add-on (Settings → Add-ons) and start it.
2. Create a dedicated HA user for the bridge (Settings → People → Users),
   e.g. `poebridge` — the Mosquitto add-on authenticates against HA users.
3. Set up the **MQTT integration** (Settings → Devices & Services) against
   the local broker if HA didn't do it automatically.

Verify the broker is reachable *from the host* via the NAT address:

```bash
nc -z 192.168.122.233 1883 && echo OK
```

(If you test against the VM's LAN address instead, it will fail from the
host — that's the macvtap isolation from §3, not a broker problem.)

## 5. Install the bridge

On the host:

```bash
sudo apt install python3-venv        # HAOS-side: nothing to install
sudo python3 -m venv /opt/exaviz-poe-mqtt-bridge
sudo /opt/exaviz-poe-mqtt-bridge/bin/pip install /path/to/exaviz-poe-mqtt-bridge

sudo mkdir -p /etc/exaviz-poe-mqtt-bridge
sudo cp config.example.yaml /etc/exaviz-poe-mqtt-bridge/config.yaml
sudo chmod 600 /etc/exaviz-poe-mqtt-bridge/config.yaml
sudo $EDITOR /etc/exaviz-poe-mqtt-bridge/config.yaml
```

Key config values:

```yaml
mqtt:
  host: 192.168.122.233     # the VM's NAT address from §3 — NOT its LAN IP
  username: poebridge       # the HA user from §4
  password: ...
web:
  enabled: true             # optional local status UI on :8088
```

Install the systemd unit (adjust `ExecStart` to the venv path):

```bash
sudo sed 's|/usr/local/bin/|/opt/exaviz-poe-mqtt-bridge/bin/|' \
  systemd/exaviz-poe-mqtt-bridge.service | sudo tee /etc/systemd/system/exaviz-poe-mqtt-bridge.service
sudo systemctl daemon-reload
```

> Developing from another machine? `scripts/deploy.sh user@host config.yaml`
> automates all of the above over SSH.

## 6. First run and verification

Run once in the foreground to see everything working:

```bash
sudo /opt/exaviz-poe-mqtt-bridge/bin/exaviz-poe-mqtt-bridge \
  --config /etc/exaviz-poe-mqtt-bridge/config.yaml --log-level debug
```

Expected log lines:

```
Detected board type: cruiser
Detected onboard PoE ports: poe0, … poe7 (8 ports)
Connected to MQTT broker 192.168.122.233:1883
Published 48 retained discovery configs for 8 ports
Found ESP32 data for 8 ports        (every poll cycle)
```

In Home Assistant a device **Exaviz Cruiser CM5** appears under the MQTT
integration with 6 entities per port (switch, power, voltage, current,
link, connected device). Toggling a switch cuts/restores PoE power on the
physical port.

Then enable the service permanently:

```bash
sudo systemctl enable --now exaviz-poe-mqtt-bridge
journalctl -u exaviz-poe-mqtt-bridge -f
```

If you enabled the web UI, browse to `http://<host>:8088` — note it has
**no authentication** (same control surface as the MQTT command topics).

## 7. Troubleshooting

| Symptom | Cause / fix |
|---|---|
| `ensurepip is not available` creating the venv | `sudo apt install python3-venv` (or `python3.X-venv` matching your Python) |
| No `Connected to MQTT broker` in logs, `Cannot reach MQTT broker` warnings | Broker unreachable from the host — you're probably using the VM's LAN IP; use the NAT address (§3) |
| `MQTT connect failed: Not authorized` | Wrong credentials — use a real HA user (§4) |
| All ports show OFF / 0 W | Normal if the `poe*` interfaces are admin-down and nothing is connected; enable a port from HA and plug in a device |
| Connected-device sensor shows `none` despite an active link | The device has no IP: current `exaviz-netplan` doesn't yet configure per-port subnets/DHCP on Ubuntu (Exaviz is working on it). Link, power and control still work |
| Bridge entities `unavailable` in HA | The daemon is down (availability topic went `offline` via MQTT Last Will) — `systemctl status exaviz-poe-mqtt-bridge` |

## 8. Known limitations

- **Interceptor boards**: detected but telemetry/control not wired up yet
  (Cruiser-first; the `/proc/pse` reader can be ported the same way).
- **Enable-port workaround**: after enabling a port the bridge sends a full
  ESP32 `reset` (~8 s settle) to work around a firmware bug where the port
  would stay stuck in `detecting`. Other ports keep power during the reset.
  Disable via `bridge.enable_reset_workaround: false` once fixed upstream.
