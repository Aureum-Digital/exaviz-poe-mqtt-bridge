"""Exaviz PoE MQTT bridge.

Host-side daemon that reads Exaviz Cruiser/Interceptor PoE port telemetry
from /dev/pse (and sysfs) and exposes each port to Home Assistant over
MQTT Discovery, so HAOS running inside a VM never needs direct hardware
access.
"""

__version__ = "0.1.0"
