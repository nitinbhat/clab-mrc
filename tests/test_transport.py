import argparse
import socket
import struct
import sys
import unittest
from pathlib import Path

# Add src/ to path for imports
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import mrc_paths
import transport
from mrc_reliability import Receiver


CONFIG = {
    "fabric": "mrc", "planes": 2, "host_links": 2, "spines": 2,
    "hosts": {
        "h1": {"index": 1, "nic": "fc00:100::1",
               "ports": {"eth1": [1, 1], "eth2": [1, 2], "eth3": [2, 1], "eth4": [2, 2]}},
        "h2": {"index": 2, "nic": "fc00:200::1",
               "ports": {"eth1": [1, 1], "eth2": [1, 2], "eth3": [2, 1], "eth4": [2, 2]}},
    },
}
ALL_PORTS = 0b1111


class FakeNic:
    send_errors = 0

    def __init__(self):
        self.sent = []

    def send(self, peer, ev, payload, dport=transport.UDP_PORT, outer=None):
        self.sent.append((ev, transport.HEADER.unpack(payload[:transport.HEADER.size])))

    def receive(self):
        return iter(())

    def filenos(self):
        return []


def args(**overrides):
    values = dict(host="h1", peer="h2", qp=1, evs=None, active_per_plane=2, seed=1,
                  size=64, window=128, inflight=128, rto=0.02, probe_interval=0.02,
                  probe_timeout=0.05, resurrect_after=3, ecn_skip=0.004, no_ecn_avoid=False)
    values.update(overrides)
    return argparse.Namespace(**values)


class SenderHarness:
    """An MrcSender wired to a fake NIC and a real mrc_reliability Receiver."""

    def __init__(self, **overrides):
        self.nic = FakeNic()
        self.ports = ALL_PORTS
        self.sender = transport.MrcSender(args(**overrides), CONFIG, nic=self.nic,
                                          read_ports=lambda: self.ports)
        self.receiver = Receiver(transport.initial_psn(1), 1, 1, 128)

    def send_on(self, ev):
        psn = self.sender.requestor.send()
        self.sender.transmit(psn, ev, 1)
        return psn

    def sack(self, psn, ev, ce=False, ports=ALL_PORTS):
        reception = self.receiver.receive(psn, nominal_bytes=64, entropy=ev)
        payload = (transport.HEADER.pack(transport.SACK, 0, 1, psn, ev, 0)
                   + reception.sack.encode() + transport.SACK_EXT.pack(ports, ce))
        self.sender.on_packet(payload, {"sport": 0, "ce": ce})

    def active(self, plane=None):
        return sorted(ev for ev, e in self.sender.evs.items() if e["state"] == "ACTIVE"
                      and (plane is None or e["plane"] == plane))


class EvSetTests(unittest.TestCase):
    def test_srv6_ev_set_covers_every_path_equally_per_plane(self):
        evs = transport.ev_set(CONFIG, "h1", "h2", salt=7)
        self.assertEqual(len(evs), 8)
        self.assertTrue(all(ev >> 16 == 7 for ev in evs))
        planes = [mrc_paths.ev_fields(ev)[0] for ev in evs]
        self.assertEqual(planes.count(1), planes.count(2))

    def test_ecmp_ev_set_uses_distinct_source_ports(self):
        config = {**CONFIG, "fabric": "baseline"}
        evs = transport.ev_set(config, "h1", "h2", salt=3)
        self.assertEqual(len({ev & 0xFFFF for ev in evs}), len(evs))
        self.assertTrue(all(ev & 0xFFFF >= 0xC000 for ev in evs))


class PacketTests(unittest.TestCase):
    def test_inner_packet_round_trip_and_checksum(self):
        packet = transport.inner_packet("fc00:100::1", "fc00:200::1", 0x0111, 4791,
                                        0x1, b"payload")
        parsed = transport.parse_inner(packet)
        self.assertEqual((parsed["src"], parsed["dst"], parsed["sport"], parsed["flow"],
                          parsed["payload"]), ("fc00:100::1", "fc00:200::1", 0x0111, 1,
                                               b"payload"))
        src = socket.inet_pton(socket.AF_INET6, "fc00:100::1")
        dst = socket.inet_pton(socket.AF_INET6, "fc00:200::1")
        udp = packet[40:]
        pseudo = src + dst + struct.pack("!I3xB", len(udp), 17)
        # A valid checksum makes the one's-complement sum all ones.
        self.assertEqual(transport.checksum(pseudo + udp), 0xFFFF)


class MrcSenderTests(unittest.TestCase):
    def test_active_set_is_split_equally_with_backups(self):
        h = SenderHarness()
        self.assertEqual(len(h.active(1)), 2)
        self.assertEqual(len(h.active(2)), 2)
        backups = [e for e in h.sender.evs.values() if e["state"] == "BACKUP"]
        self.assertEqual(len(backups), 4)

    def test_gap_on_a_path_retires_it_and_promotes_same_plane_backup(self):
        h = SenderHarness()
        ev = h.active(1)[0]
        first, second, third = (h.send_on(ev) for _ in range(3))
        h.sack(third, ev)  # first and second were overtaken on a FIFO path
        self.assertEqual(h.sender.evs[ev]["state"], "ASSUMED_BAD")
        self.assertEqual(list(h.sender.retransmit), [first, second])
        self.assertEqual(len(h.active(1)), 2)
        self.assertNotIn(ev, h.active(1))

    def test_timeout_retires_path(self):
        h = SenderHarness()
        ev = h.active(2)[0]
        psn = h.send_on(ev)
        h.sender.outstanding[psn]["sent"] -= 1
        h.sender.check_timers()
        self.assertEqual(h.sender.evs[ev]["state"], "ASSUMED_BAD")

    def test_probes_resurrect_a_retired_path(self):
        h = SenderHarness()
        ev = h.active(1)[0]
        h.sender.set_state(ev, "ASSUMED_BAD", "test")
        for _ in range(3):
            h.nic.sent.clear()
            h.sender.send_probes()
            (probe_ev, header), = h.nic.sent
            self.assertEqual((probe_ev, header[0]), (ev, transport.PROBE))
            reply = transport.HEADER.pack(transport.PROBE_RESP, 0, 1, header[3], ev, 0)
            h.sender.on_packet(reply, {"sport": 0, "ce": False})
        self.assertEqual(h.sender.evs[ev]["state"], "BACKUP")
        self.assertTrue(any(e["event"] == "ev_resurrected" for e in h.sender.rec.events))

    def test_ecn_echo_temporarily_avoids_path(self):
        h = SenderHarness(active_per_plane=1)
        marked = h.active(1)[0]
        psn = h.send_on(marked)
        h.sack(psn, marked, ce=True)
        self.assertEqual(h.sender.evs[marked]["state"], "ACTIVE")
        chosen = {h.sender.choose() for _ in range(6)}
        self.assertNotIn(marked, chosen)

    def test_peer_port_bitmap_denies_and_restores_paths(self):
        h = SenderHarness()
        ev = h.active(1)[0]
        psn = h.send_on(ev)
        down = ALL_PORTS & ~(1 << mrc_paths.port_index(1, 1, 2))
        h.sack(psn, ev, ports=down)
        denied = {e for e, s in h.sender.evs.items() if s["state"] == "DENIED"}
        self.assertEqual({mrc_paths.ev_fields(e)[:2] for e in denied}, {(1, 1)})
        self.assertEqual(len(h.active(1)), 2)  # plane 1 port 2 paths take over
        h.sender.refresh_ports(ALL_PORTS)
        self.assertFalse(any(s["state"] == "DENIED" for s in h.sender.evs.values()))
        # Paths behind a recovered port are probed before they carry data again.
        self.assertTrue(all(h.sender.evs[e]["state"] == "ASSUMED_BAD" for e in denied))

    def test_local_port_down_is_noticed_without_any_feedback(self):
        h = SenderHarness()
        h.ports = ALL_PORTS & ~(1 << mrc_paths.port_index(2, 2, 2))
        h.sender.check_timers()
        denied = {mrc_paths.ev_fields(e)[:2] for e, s in h.sender.evs.items()
                  if s["state"] == "DENIED"}
        self.assertEqual(denied, {(2, 2)})


if __name__ == "__main__":
    unittest.main()
