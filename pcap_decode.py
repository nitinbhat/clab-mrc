#!/usr/bin/env python3
"""Decode lab pcaps: uSID path, EV, PSN, SACK contents, ECN.

Optional analysis tool; the lab itself does not need scapy (pip install scapy).

    python3 pcap_decode.py docs/evidence/multi-plane-multi-link/mrc/spray/1-h1-port1.pcap
    python3 pcap_decode.py FILE --only sack --limit 5
    python3 pcap_decode.py FILE --summary
    python3 pcap_decode.py FILE --timeline     # data per EV per 100 ms, resends
"""
import argparse
import ipaddress
from collections import Counter

from scapy.all import IPv6, UDP, Raw, rdpcap

import mrc_paths
from mrc_reliability import Sack
from transport import HEADER, SACK_EXT, UDP_PORT

OPCODES = {1: "DATA", 2: "SACK", 3: "NAK", 4: "PROBE", 5: "PROBE_RESP",
           6: "EV_PROBE", 7: "EV_PROBE_RESP", 8: "SELF_PROBE"}
ECN = {0: "not-ECT", 1: "ECT(1)", 2: "ECT(0)", 3: "CE"}
SACK_BYTES = 36


def usid_list(address):
    """Remaining uSIDs in an outer destination (empty when it is not a uSID path)."""
    packed = ipaddress.IPv6Address(address).packed
    words = [int.from_bytes(packed[i:i + 2], "big") for i in range(4, 16, 2)]
    return [f"{w:04x}" for w in words if w]


def describe(pkt, number):
    """Return a one-line description and a record dict, or None for non-lab packets."""
    if IPv6 not in pkt:
        return None
    outer = pkt[IPv6]
    srv6 = outer.nh == 41
    inner = outer.payload if srv6 else outer
    if not isinstance(inner, IPv6) or UDP not in inner:
        return None
    udp = inner[UDP]
    payload = bytes(udp.payload)
    if len(payload) < HEADER.size:
        return None
    opcode, flags, qp, psn, ev, _ = HEADER.unpack(payload[:HEADER.size])
    if opcode not in OPCODES:
        return None
    name = OPCODES[opcode]
    parts = [f"#{number}", name, f"qp={qp}", f"psn={psn}"]
    if srv6:
        plane, link, spine = mrc_paths.ev_fields(ev)
        parts.append(f"ev={ev:#x}(plane={plane},link={link},spine={spine})")
        parts.append(f"path={'>'.join(usid_list(outer.dst))}")
        parts.append(f"outer_ecn={ECN[outer.tc & 3]}")
    else:
        parts.append(f"ev={ev:#x} (udp sport={udp.sport})")
    parts.append(f"{inner.src}:{udp.sport}>{inner.dst}:{udp.dport}")
    parts.append(f"inner_ecn={ECN[inner.tc & 3]}")
    body = payload[HEADER.size:]
    if opcode == SACK and len(body) >= SACK_BYTES:
        sack = Sack.decode(body[:SACK_BYTES])
        extra = body[SACK_BYTES:SACK_BYTES + SACK_EXT.size]
        parts.append(f"cack={sack.cack_psn} ack={sack.ack_psn} "
                     f"sack_base={sack.sack_base} bitmap={sack.sack_bitmap:#x}")
        if len(extra) == SACK_EXT.size:
            ports, ce = SACK_EXT.unpack(extra)
            parts.append(f"port_bitmap={ports:#04b} ce_echo={ce}")
    return " ".join(parts), {"opcode": name, "ev": ev, "psn": psn, "srv6": srv6}


SACK = 2


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("pcap")
    ap.add_argument("--only", choices=[n.lower() for n in OPCODES.values()],
                    help="show only this packet type")
    ap.add_argument("--limit", type=int, help="stop after N packets shown")
    ap.add_argument("--summary", action="store_true",
                    help="print packet counts per type and per EV instead of each packet")
    ap.add_argument("--timeline", action="store_true",
                    help="data packets per EV per 100 ms, plus resent PSNs and probes")
    args = ap.parse_args()

    counts, evs, shown = Counter(), Counter(), 0
    buckets, seen, start = {}, set(), None
    for number, pkt in enumerate(rdpcap(args.pcap), 1):
        result = describe(pkt, number)
        if result is None:
            continue
        line, rec = result
        if args.only and rec["opcode"].lower() != args.only:
            continue
        counts[rec["opcode"]] += 1
        if args.timeline:
            start = float(pkt.time) if start is None else start
            row = buckets.setdefault(int((float(pkt.time) - start) * 10), Counter())
            if rec["opcode"] == "DATA":
                row[f"ev={rec['ev']:#x}"] += 1
                if rec["psn"] in seen:
                    row["resent"] += 1
                seen.add(rec["psn"])
            elif "PROBE" in rec["opcode"]:
                row["probes"] += 1
            continue
        if rec["opcode"] == "DATA":
            evs[rec["ev"]] += 1
        if args.summary:
            continue
        print(line)
        shown += 1
        if args.limit and shown >= args.limit:
            break
    for tenth, row in sorted(buckets.items()):
        print(f"{tenth / 10:5.1f}s  " + "  ".join(f"{k}:{v}" for k, v in sorted(row.items())))
    if args.summary:
        for name, n in counts.most_common():
            print(f"{name:14} {n}")
        for ev, n in sorted(evs.items()):
            print(f"DATA ev={ev:#x}: {n}")


if __name__ == "__main__":
    main()
