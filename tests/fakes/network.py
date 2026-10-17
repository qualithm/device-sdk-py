"""A host fake of MicroPython's `network`: a station interface that follows a scripted join."""

STA_IF = 0
STAT_IDLE = 0
STAT_CONNECTING = 1
STAT_GOT_IP = 3
STAT_CONNECT_FAIL = -1
STAT_NO_AP_FOUND = -2
STAT_WRONG_PASSWORD = -3


class WLAN:
    """A station interface. `statuses` scripts each status() after connect(); the last repeats."""

    statuses = [STAT_GOT_IP]  # noqa: RUF012 -- state a test scripts
    connected = False
    rssi = -60
    instances = []  # noqa: RUF012 -- state a test scripts

    def __init__(self, interface):
        self.interface = interface
        self.is_active = False
        self.joined = None
        self._reads = 0
        WLAN.instances.append(self)

    def active(self, is_active):
        self.is_active = is_active

    def connect(self, ssid, password):
        self.joined = (ssid, password)

    def _current(self):
        if WLAN.connected:
            return STAT_GOT_IP
        if self.joined is None:
            return STAT_IDLE
        index = min(self._reads, len(WLAN.statuses) - 1)
        return WLAN.statuses[index]

    def isconnected(self):
        return self._current() == STAT_GOT_IP

    def status(self, param=None):
        if param == "rssi":
            return WLAN.rssi
        status = self._current()
        self._reads += 1
        return status


def reset():
    WLAN.statuses = [STAT_GOT_IP]
    WLAN.connected = False
    WLAN.rssi = -60
    WLAN.instances = []
