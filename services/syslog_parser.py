"""
Reads one syslog datagram as RouterOS sends it. Tolerant on purpose: RouterOS can send
plain text ("system,info,account user admin logged in"), BSD syslog (RFC 3164, with a
timestamp and the router's name in front) or RFC 5424 - depending on version and settings.

Never raises: whatever arrives, something readable comes out.
"""
import re
from dataclasses import dataclass

SEVERITIES = ["emergency", "alert", "critical", "error", "warning", "notice", "info", "debug"]
MAX_MESSAGE = 2000

_PRI = re.compile(r"^<(\d{1,3})>")
_BSD_TS = re.compile(r"^[A-Z][a-z]{2} +\d{1,2} \d{2}:\d{2}:\d{2} ")
_RFC5424 = re.compile(r"^1 (\S+) (\S+) (\S+) (\S+) (\S+) (-|(?:\[[^\]]*\])+) ?(.*)$", re.S)
_TOPICS = re.compile(r"^([a-z0-9][a-z0-9\-]*(?:,[a-z0-9\-]+)*):?(?:\s+(.*))?$", re.S)

# A lone word is only taken for the topic list when RouterOS really uses it as a topic.
_KNOWN_TOPICS = {
    "info", "error", "warning", "critical", "debug", "system", "account", "firewall", "dhcp",
    "wireless", "interface", "script", "route", "ppp", "pppoe", "l2tp", "ipsec", "ospf", "bgp",
    "hotspot", "web-proxy", "caps", "snmp", "watchdog", "manager", "backup", "ntp", "dns",
    "certificate", "ovpn", "pptp", "sstp", "wifi", "bridge", "vrrp", "event", "gps", "lte",
}
_TOPIC_SEVERITY = [("critical", 2), ("error", 3), ("warning", 4), ("debug", 7), ("info", 6)]


@dataclass
class Parsed:
    severity_num: int
    topics: str
    message: str

    @property
    def severity(self) -> str:
        return SEVERITIES[self.severity_num]


def _split_topics(rest: str):
    """('system,info,account user x', ...) -> ('system,info,account', 'user x'); else ('', rest)."""
    m = _TOPICS.match(rest)
    if not m:
        return "", rest
    token, message = m.group(1), m.group(2)
    topics = token.split(",")
    if "," in token or token in _KNOWN_TOPICS:
        # a lone "info" with nothing after it is a message, not a topic list
        if message is None:
            return "", rest
        return ",".join(topics), message
    return "", rest


def parse_syslog(data: bytes) -> Parsed:
    text = data.decode("utf-8", errors="replace").replace("\x00", "").strip(" \r\n\t")
    severity = None

    m = _PRI.match(text)
    if m:
        pri = int(m.group(1))
        if pri <= 191:
            severity = pri & 7
        text = text[m.end():]

    topics, message = "", text
    m5 = _RFC5424.match(text)
    if m5:                                               # RFC 5424: ... APP-NAME ... MSG
        app, message = m5.group(3), m5.group(7)
        topics, rest = _split_topics(app + " x")
        if not topics:
            topics, message = _split_topics(message)
    else:
        if _BSD_TS.match(text):                          # RFC 3164: "Nov 22 12:00:00 <host> ..."
            text = text[len(_BSD_TS.match(text).group(0)):]
            parts = text.split(" ", 1)
            text = parts[1] if len(parts) > 1 else ""
        topics, message = _split_topics(text)

    if severity is None:                                 # no PRI: guess from the topics
        severity = 6
        for name, num in _TOPIC_SEVERITY:
            if name in topics.split(","):
                severity = num
                break

    message = message.strip()
    if len(message) > MAX_MESSAGE:
        message = message[:MAX_MESSAGE] + " [cut]"
    return Parsed(severity, topics, message)
