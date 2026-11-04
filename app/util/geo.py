"""Italian-IP check for the signup geofence (stdlib only).

Ranges = RIPE NCC delegated stats for country IT (country of *registration*,
not precise geolocation; reliable for Italian ISPs). Stored in it_ranges.txt
next to this file, committed. Refresh + self-check:

    python -m app.util.geo

Fail-open by design (anti-noise, not a security boundary): private/unparsable
IPs or a missing ranges file → allowed.
"""

import bisect
import ipaddress
import urllib.request
from functools import lru_cache
from pathlib import Path

_FILE = Path(__file__).with_name("it_ranges.txt")
_RIPE_URL = "https://ftp.ripe.net/pub/stats/ripencc/delegated-ripencc-latest"


@lru_cache(maxsize=1)
def _ranges() -> dict[int, tuple[list[int], list[int]]]:
    """{ip_version: (sorted starts, matching ends)}; empty lists if no file."""
    spans: dict[int, list[tuple[int, int]]] = {4: [], 6: []}
    if _FILE.exists():
        for line in _FILE.read_text().split():
            net = ipaddress.ip_network(line)
            spans[net.version].append((int(net.network_address), int(net.broadcast_address)))
    return {v: ([s for s, _ in sorted(r)], [e for _, e in sorted(r)]) for v, r in spans.items()}


def is_italian_ip(ip: str) -> bool:
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return True
    if isinstance(addr, ipaddress.IPv6Address) and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    if not addr.is_global:  # localhost/dev, LAN, CGNAT
        return True
    starts, ends = _ranges()[addr.version]
    if not starts:
        return True
    i = bisect.bisect_right(starts, int(addr)) - 1
    return i >= 0 and int(addr) <= ends[i]


def _refresh() -> None:
    nets: dict[int, list] = {4: [], 6: []}
    with urllib.request.urlopen(_RIPE_URL, timeout=120) as resp:
        for raw in resp:
            f = raw.decode("ascii", "replace").strip().split("|")
            if len(f) < 7 or f[1] != "IT" or f[6] not in ("allocated", "assigned"):
                continue
            if f[2] == "ipv4":
                start = ipaddress.IPv4Address(f[3])
                nets[4] += ipaddress.summarize_address_range(start, start + int(f[4]) - 1)
            elif f[2] == "ipv6":
                nets[6].append(ipaddress.IPv6Network(f"{f[3]}/{f[4]}"))
    out = [str(n) for v in (4, 6) for n in ipaddress.collapse_addresses(nets[v])]
    _FILE.write_text("\n".join(out) + "\n")
    _ranges.cache_clear()
    print(f"[geo] {len(out)} IT networks -> {_FILE}")


if __name__ == "__main__":
    _refresh()
    # Bot IPs seen on /signup (Nov 2026): TR FibaCloud, US HostPapa.
    assert not is_italian_ip("155.103.69.118")
    assert not is_italian_ip("192.210.150.198")
    assert not is_italian_ip("8.8.8.8")
    assert is_italian_ip("127.0.0.1") and is_italian_ip("10.0.0.1") and is_italian_ip("garbage")
    first_v4 = _FILE.read_text().split()[0].split("/")[0]
    assert is_italian_ip(first_v4) and is_italian_ip(f"::ffff:{first_v4}")
    print("[geo] self-check ok; 185.11.194.23 italiano:", is_italian_ip("185.11.194.23"))
