#!/usr/bin/env python3
"""Educational userspace MRC endpoint ("NIC") for the containerlab fabric.

NOT an interoperable MRC implementation: there is no RDMA/verbs, BTH/RETH,
NSCC congestion control, or packet trimming, and the data header is a lab
format. What it does model, following the OpenAI/Microsoft MRC paper:

* packet spraying over an EV set split equally across planes, with backups;
* IPv6-in-IPv6 SRv6 uSID source routing (outer = path, inner = NIC address),
  or plain ECMP hashing of the EV in the UDP source port (baseline fabric);
* 24-bit PSNs and binary SACKs from mrc_reliability (SETH + CC_STATE);
* retiring an EV on its first lost packet, selective retransmission on
  another EV, replacement from the same plane, background probes and
  resurrection;
* ECN echo as a load-balancing signal (temporarily skip a marked EV);
* a NIC port-state bitmap in SACKs so the peer avoids a failed plane/port;
* a reverse EV set, kept working with EV probes, for control packets;
* Clustermapper-style self probes (to a T0/T1 switch and back);
* a single-path, go-back-N "roce" mode for comparison.
"""
import argparse
from collections import deque
import hashlib
import json
from pathlib import Path
import random
import select
import socket
import struct
import time

import mrc_paths
from mrc_reliability import PSN_MASK, Receiver, Requestor, Sack, WindowError

RESULTS = Path("/results")
UDP_PORT = 4791  # RoCEv2
IPV6_RECVTCLASS = 66
IPV6_TCLASS = 67
ECT0, CE = 0x02, 0x03

DATA, SACK, NAK, PROBE, PROBE_RESP, EV_PROBE, EV_PROBE_RESP, SELF_PROBE = range(1, 9)
FLAG_ROCE = 1
# Lab header (not the MRC BTH): opcode, flags, QP, PSN/probe id, EV, tx time (us).
HEADER = struct.Struct("!BBHIII")
# Lab extension after the 36-byte SETH+CC_STATE: NIC port-up bitmap, CE echo.
SACK_EXT = struct.Struct("!BB")
ACTIVE, BACKUP, ASSUMED_BAD, DENIED = "ACTIVE", "BACKUP", "ASSUMED_BAD", "DENIED"


def now_us():
    return int(time.time() * 1e6) & 0xFFFFFFFF


def initial_psn(qp):
    # Deterministic per QP, so both ends agree without a handshake. Starting
    # a few hundred PSNs below 2^24 makes every transfer cross PSN rollover.
    return (PSN_MASK - 255 - 16 * (qp & 0xF)) & PSN_MASK


def pattern(qp, psn, size):
    digest = hashlib.sha256(f"{qp}:{psn}".encode()).digest()
    return (digest * (size // 32 + 1))[:size]


def checksum(data):
    if len(data) % 2:
        data += b"\0"
    total = sum(struct.unpack(f"!{len(data) // 2}H", data))
    while total >> 16:
        total = (total & 0xFFFF) + (total >> 16)
    return (~total & 0xFFFF) or 0xFFFF


def inner_packet(src, dst, sport, dport, flow, payload):
    """Inner IPv6 + UDP header with a valid checksum over the inner addresses."""
    s, d = socket.inet_pton(socket.AF_INET6, src), socket.inet_pton(socket.AF_INET6, dst)
    length = 8 + len(payload)
    udp = struct.pack("!HHHH", sport, dport, length, 0) + payload
    csum = checksum(s + d + struct.pack("!I3xB", length, 17) + udp)
    ip = struct.pack("!IHBB", 6 << 28 | ECT0 << 20 | (flow & 0xFFFFF), length, 17, 64)
    return ip + s + d + udp[:6] + struct.pack("!H", csum) + udp[8:]


def parse_inner(data):
    if len(data) < 48 or data[0] >> 4 != 6 or data[6] != 17:
        return None
    flow = struct.unpack("!I", data[:4])[0] & 0xFFFFF
    sport, dport = struct.unpack("!HH", data[40:44])
    return {"src": socket.inet_ntop(socket.AF_INET6, data[8:24]),
            "dst": socket.inet_ntop(socket.AF_INET6, data[24:40]),
            "sport": sport, "dport": dport, "flow": flow, "payload": data[48:]}


def port_states(config, host):
    """NIC port-up bitmap read from the kernel (operstate of each fabric port)."""
    bitmap = 0
    links = config["host_links"]
    for iface, (plane, link) in config["hosts"][host]["ports"].items():
        try:
            state = Path(f"/sys/class/net/{iface}/operstate").read_text().strip()
        except OSError:
            state = "down"
        if state == "up":
            bitmap |= 1 << mrc_paths.port_index(plane, link, links)
    return bitmap


class SrNic:
    """Raw IPv6-in-IPv6: outer destination is the uSID path (protocol 41)."""

    def __init__(self, config, host):
        self.config, self.host = config, host
        self.index = config["hosts"][host]["index"]
        self.addr = config["hosts"][host]["nic"]
        self.sock = socket.socket(socket.AF_INET6, socket.SOCK_RAW, 41)
        self.sock.setsockopt(socket.IPPROTO_IPV6, IPV6_TCLASS, ECT0)
        self.sock.setsockopt(socket.IPPROTO_IPV6, IPV6_RECVTCLASS, 1)
        self.sock.setblocking(False)
        self.send_errors = 0

    def filenos(self):
        return [self.sock]

    def send(self, peer, ev, payload, dport=UDP_PORT, outer=None):
        peer_cfg = self.config["hosts"][peer]
        if outer is None:
            outer = mrc_paths.ev_address(ev, self.index, peer_cfg["index"])
        packet = inner_packet(self.addr, peer_cfg["nic"], ev & 0xFFFF, dport,
                              ev >> 16, payload)
        try:
            self.sock.sendto(packet, (outer, 0))
        except OSError:
            # No route: the local NIC port for this path is down.
            self.send_errors += 1

    def receive(self):
        while True:
            try:
                data, ancillary, _, _ = self.sock.recvmsg(4096, 64)
            except BlockingIOError:
                return
            inner = parse_inner(data)
            if inner is None or inner["dst"] != self.addr:
                continue
            tclass = next((struct.unpack("i", d[:4])[0] for _, t, d in ancillary
                           if t == IPV6_TCLASS), 0)
            yield inner["payload"], {"src": inner["src"], "sport": inner["sport"],
                                     "ce": tclass & 3 == CE}


class EcmpNic:
    """Plain UDP; the EV is the source port, so switches hash it onto a path."""

    def __init__(self, config, host, listen=False):
        self.config, self.host = config, host
        self.addr = config["hosts"][host]["nic"]
        self.sockets = {}
        self.listener = self._socket(UDP_PORT) if listen else None
        self.send_errors = 0

    def _socket(self, port):
        sock = socket.socket(socket.AF_INET6, socket.SOCK_DGRAM)
        sock.setsockopt(socket.IPPROTO_IPV6, IPV6_TCLASS, ECT0)
        sock.setsockopt(socket.IPPROTO_IPV6, IPV6_RECVTCLASS, 1)
        sock.bind((self.addr, port))
        sock.setblocking(False)
        return sock

    def filenos(self):
        return list(self.sockets.values()) + ([self.listener] if self.listener else [])

    def send(self, peer, ev, payload, dport=UDP_PORT, outer=None):
        if ev & 0xFFFF not in self.sockets:
            self.sockets[ev & 0xFFFF] = self._socket(ev & 0xFFFF)
        try:
            self.sockets[ev & 0xFFFF].sendto(
                payload, (self.config["hosts"][peer]["nic"], dport))
        except OSError:
            self.send_errors += 1

    def receive(self):
        for sock in self.filenos():
            while True:
                try:
                    payload, ancillary, _, src = sock.recvmsg(4096, 64)
                except BlockingIOError:
                    break
                tclass = next((struct.unpack("i", d[:4])[0] for _, t, d in ancillary
                               if t == IPV6_TCLASS), 0)
                yield payload, {"src": src[0], "sport": src[1], "ce": tclass & 3 == CE}


def make_nic(config, host, listen=False):
    return SrNic(config, host) if config["fabric"] == "mrc" else EcmpNic(config, host, listen)


def host_of(config, address):
    return next(h for h, c in config["hosts"].items() if c["nic"] == address)


def ev_set(config, src, dst, salt):
    """All EVs from src to dst: one per (plane, port, spine) in SRv6 mode."""
    if config["fabric"] == "mrc":
        return [mrc_paths.make_ev(p, l, s, salt)
                for p in range(1, config["planes"] + 1)
                for l in range(1, config["host_links"] + 1)
                for s in range(1, config["spines"] + 1)]
    # ECMP: the path is whatever the switches' hash picks for this source port.
    rng = random.Random(salt * 7919 + config["hosts"][src]["index"])
    count = config["planes"] * config["host_links"] * config["spines"]
    ports = rng.sample(range(0xC000, 0xFFFF), count)
    return [(salt & 0xFFFF) << 16 | port for port in ports]


def ev_plane(config, ev):
    return mrc_paths.ev_fields(ev)[0] if config["fabric"] == "mrc" else 0


def ev_port(config, ev):
    if config["fabric"] != "mrc":
        return None
    plane, link, _ = mrc_paths.ev_fields(ev)
    return mrc_paths.port_index(plane, link, config["host_links"])


class Recorder:
    """Per-10 ms timeline plus a timestamped event log (wall clock, shared host)."""

    BIN = 0.01

    def __init__(self):
        self.t0 = time.time()
        self.bins = {}
        self.events = []

    def count(self, field, n=1, ev=None):
        b = self.bins.setdefault(int((time.time() - self.t0) / self.BIN), {})
        b[field] = b.get(field, 0) + n
        if ev is not None:
            key = f"{field}_by_ev"
            b.setdefault(key, {})
            b[key][f"{ev:x}"] = b[key].get(f"{ev:x}", 0) + n

    def event(self, kind, **fields):
        self.events.append({"t": round(time.time(), 6), "event": kind, **{
            k: f"{v:x}" if k == "ev" else v for k, v in fields.items()}})

    def export(self):
        return {"t0": self.t0, "bin_seconds": self.BIN, "events": self.events,
                "timeline": [{"t": round(i * self.BIN, 3), **v}
                             for i, v in sorted(self.bins.items())]}


class MrcSender:
    def __init__(self, args, config, nic=None, read_ports=None):
        self.args, self.config = args, config
        self.nic = nic or make_nic(config, args.host)
        self.read_ports = read_ports or (lambda: port_states(config, args.host))
        self.peer, self.qp = args.peer, args.qp
        self.requestor = Requestor(initial_psn(self.qp), self.qp, self.qp, args.window)
        self.rec = Recorder()
        evs = args.evs or ev_set(config, args.host, self.peer, self.qp)
        self.evs = {ev: {"state": BACKUP, "plane": ev_plane(config, ev),
                         "port": ev_port(config, ev), "sent": 0, "acked": 0,
                         "lost": 0, "ce": 0, "probes_ok": 0, "skip_until": 0.0,
                         "srtt": None} for ev in evs}
        self.planes = sorted({e["plane"] for e in self.evs.values()})
        self.rng = random.Random(args.seed)
        self.rotation = []
        self.outstanding = {}  # psn -> {"ev", "sent", "attempt", "lost"}
        self.order = {ev: deque() for ev in self.evs}  # FIFO per path
        self.retransmit = deque()
        self.probes = {}  # probe id -> (ev, sent)
        self.next_probe_id = 1
        self.local_ports = self.read_ports()
        self.remote_ports = None
        self.delivered = self.retransmissions = self.losses = 0
        self.sent_new = 0
        self.rto_min = args.rto
        for plane in self.planes:
            self.fill(plane)

    # --- EV set management -------------------------------------------------
    def usable(self, ev):
        port = self.evs[ev]["port"]
        if port is None:
            return True
        up = self.local_ports >> port & 1
        if self.remote_ports is not None:
            up &= self.remote_ports >> port & 1
        return bool(up)

    def target(self, plane):
        total = sum(1 for e in self.evs.values() if e["plane"] == plane)
        return min(self.args.active_per_plane or total, total)

    def fill(self, plane):
        """Keep the active set per plane at its target, using same-plane backups."""
        active = [ev for ev, e in self.evs.items()
                  if e["plane"] == plane and e["state"] == ACTIVE]
        backups = [ev for ev, e in self.evs.items()
                   if e["plane"] == plane and e["state"] == BACKUP and self.usable(ev)]
        self.rng.shuffle(backups)
        while len(active) < self.target(plane) and backups:
            ev = backups.pop()
            self.evs[ev]["state"] = ACTIVE
            active.append(ev)
            self.rec.event("ev_activated", ev=ev, plane=plane)

    def set_state(self, ev, state, reason):
        entry = self.evs[ev]
        if entry["state"] == state:
            return
        previous, entry["state"] = entry["state"], state
        self.rec.event("ev_state", ev=ev, plane=entry["plane"], old=previous,
                       new=state, reason=reason)
        if state in (ASSUMED_BAD, DENIED):
            entry["probes_ok"] = 0
            self.rotation = [e for e in self.rotation if e != ev]
        self.fill(entry["plane"])

    def refresh_ports(self, remote=None):
        if remote is not None:
            if remote != self.remote_ports:
                self.rec.event("peer_port_bitmap", bitmap=remote)
            self.remote_ports = remote
        for ev, entry in self.evs.items():
            if entry["state"] == DENIED and self.usable(ev):
                # Port up is not proof the path works: probe before reuse.
                self.set_state(ev, ASSUMED_BAD, "port up, probing")
            elif entry["state"] in (ACTIVE, BACKUP) and not self.usable(ev):
                self.set_state(ev, DENIED, "port down")

    def choose(self, avoid=None):
        now = time.time()
        for _ in range(2):
            if not self.rotation:
                self.rotation = [ev for ev, e in self.evs.items() if e["state"] == ACTIVE]
                self.rng.shuffle(self.rotation)
            while self.rotation:
                ev = self.rotation.pop()
                entry = self.evs[ev]
                if entry["state"] != ACTIVE or ev == avoid:
                    continue
                if entry["skip_until"] > now and not self.args.no_ecn_avoid:
                    continue
                return ev
        candidates = [ev for ev, e in self.evs.items() if e["state"] == ACTIVE]
        if candidates:
            return min(candidates, key=lambda ev: self.evs[ev]["skip_until"])
        # Last resort when every EV is retired: keep trying usable ones rather
        # than going silent (with only a few paths, a reverse-path failure can
        # retire them all; real MRC has 100+ EVs and a backup set).
        retired = [ev for ev, e in self.evs.items()
                   if e["state"] == ASSUMED_BAD and self.usable(ev) and ev != avoid]
        return self.rng.choice(retired) if retired else None

    # --- transmission ----------------------------------------------------
    def transmit(self, psn, ev, attempt):
        entry = self.evs[ev]
        flags = 0
        header = HEADER.pack(DATA, flags, self.qp, psn, ev, now_us())
        self.nic.send(self.peer, ev, header + pattern(self.qp, psn, self.args.size))
        self.outstanding[psn] = {"ev": ev, "sent": time.time(), "attempt": attempt,
                                 "lost": False}
        self.order[ev].append((psn, attempt))
        entry["sent"] += 1
        self.rec.count("sent", ev=ev)

    def lose(self, psn, why):
        packet = self.outstanding.get(psn)
        if packet is None or packet["lost"]:
            return
        packet["lost"] = True
        ev = packet["ev"]
        self.losses += 1
        self.evs[ev]["lost"] += 1
        self.rec.count("lost", ev=ev)
        self.retransmit.append(psn)
        if self.evs[ev]["state"] == ACTIVE:
            # MRC: a lost (not trimmed) packet means the path is assumed failed.
            self.set_state(ev, ASSUMED_BAD, why)

    # --- feedback ----------------------------------------------------------
    def on_sack(self, body, meta):
        if len(body) < 36 + SACK_EXT.size:
            return  # malformed or truncated SACK
        sack = Sack.decode(body[:36])
        ports, ce = SACK_EXT.unpack(body[36:38])
        try:
            newly = self.requestor.apply_sack(sack)
        except ValueError:
            return
        now = time.time()
        for psn in newly:
            packet = self.outstanding.pop(psn, None)
            if packet is None:
                continue
            self.delivered += 1
            self.evs[packet["ev"]]["acked"] += 1
            self.rec.count("delivered", ev=packet["ev"])
        ev = sack.entropy
        if ev in self.evs:
            entry = self.evs[ev]
            if ce:
                entry["ce"] += 1
                entry["skip_until"] = now + self.args.ecn_skip
                self.rec.count("ce", ev=ev)
            # Paths are FIFO: anything sent earlier on this EV and still
            # unacknowledged when a later packet arrives was lost.
            acked = sack.ack_psn
            order = self.order[ev]
            if any(p == acked for p, _ in order):
                while order:
                    psn, attempt = order.popleft()
                    if psn == acked:
                        break
                    packet = self.outstanding.get(psn)
                    if packet and packet["attempt"] == attempt and packet["ev"] == ev:
                        self.lose(psn, "gap on path")
        self.refresh_ports(ports)

    def on_packet(self, payload, meta):
        if len(payload) < HEADER.size:
            return
        opcode, flags, qp, psn, ev, _ = HEADER.unpack(payload[:HEADER.size])
        if qp != self.qp:
            return
        body = payload[HEADER.size:]
        if opcode == SACK:
            self.on_sack(body, meta)
        elif opcode == PROBE_RESP:
            self.rec.count("probe_resp_rx", ev=ev)
            if psn not in self.probes:
                return
            probe_ev, sent = self.probes.pop(psn)
            entry = self.evs[probe_ev]
            if entry["state"] == ASSUMED_BAD and time.time() - sent < self.args.probe_timeout:
                entry["probes_ok"] += 1
                if entry["probes_ok"] >= self.args.resurrect_after:
                    self.set_state(probe_ev, BACKUP, "probes succeeded")
                    self.rec.event("ev_resurrected", ev=probe_ev)
        elif opcode == EV_PROBE:
            # Echo the receiver's reverse-path probe over a working EV. If every
            # EV is retired (e.g. all ACKs were lost on a broken reverse path),
            # try any usable one: staying silent would deadlock both ends.
            reply_ev = self.choose() or self.rng.choice(
                [e for e in self.evs if self.usable(e)] or list(self.evs))
            if reply_ev is not None:
                self.nic.send(self.peer, reply_ev,
                              HEADER.pack(EV_PROBE_RESP, 0, qp, psn, ev, now_us()),
                              dport=meta["sport"])

    # --- timers -----------------------------------------------------------
    def check_timers(self):
        now = time.time()
        for psn, packet in list(self.outstanding.items()):
            if not packet["lost"] and now - packet["sent"] > self.rto_min:
                self.lose(psn, "timeout")
        for probe_id, (ev, sent) in list(self.probes.items()):
            if now - sent > self.args.probe_timeout:
                del self.probes[probe_id]
                self.evs[ev]["probes_ok"] = 0
        local = self.read_ports()
        if local != self.local_ports:
            self.rec.event("local_port_bitmap", bitmap=local)
            self.local_ports = local
            self.refresh_ports()

    def send_probes(self):
        for ev, entry in self.evs.items():
            if entry["state"] == ASSUMED_BAD and self.usable(ev):
                if any(e == ev for e, _ in self.probes.values()):
                    continue
                probe_id = self.next_probe_id
                self.next_probe_id = self.next_probe_id % 0xFFFF + 1
                self.probes[probe_id] = (ev, time.time())
                self.nic.send(self.peer, ev, HEADER.pack(
                    PROBE, 0, self.qp, probe_id, ev, now_us()))
                self.rec.count("probes", ev=ev)

    def run(self):
        args = self.args
        start = time.time()
        stop_new = start + args.duration
        deadline = stop_new + args.drain
        interval = 1.0 / args.rate
        next_send = start
        next_timer = next_probe = start
        while True:
            now = time.time()
            if now >= deadline:
                break
            if now >= stop_new and not self.outstanding:
                break
            for payload, meta in self.nic.receive():
                self.on_packet(payload, meta)
            if now >= next_timer:
                self.check_timers()
                next_timer = now + 0.002
            if now >= next_probe:
                self.send_probes()
                next_probe = now + args.probe_interval
            if now >= next_send:
                next_send = max(next_send + interval, now - 0.01)
                if self.retransmit:
                    psn = self.retransmit[0]
                    packet = self.outstanding.get(psn)
                    if packet is None:
                        self.retransmit.popleft()
                    else:
                        ev = self.choose(avoid=packet["ev"])
                        if ev is not None:
                            self.retransmit.popleft()
                            self.retransmissions += 1
                            self.rec.count("retransmitted", ev=ev)
                            self.transmit(psn, ev, packet["attempt"] + 1)
                elif now < stop_new and len(self.outstanding) < args.inflight:
                    ev = self.choose()
                    if ev is not None:
                        try:
                            psn = self.requestor.send()
                        except WindowError:
                            self.rec.count("window_full")
                        else:
                            self.sent_new += 1
                            self.transmit(psn, ev, 1)
            wait = max(0.0, min(next_send, next_timer, next_probe) - time.time())
            select.select(self.nic.filenos(), [], [], min(wait, 0.001))
        return self.result(start)

    def result(self, start):
        return {
            "algo": "mrc", "fabric": self.config["fabric"], "host": self.args.host,
            "peer": self.peer, "qp": self.qp, "rate_pps": self.args.rate,
            "duration": self.args.duration, "size": self.args.size,
            "started": start, "finished": time.time(),
            "sent_new": self.sent_new, "delivered": self.delivered,
            "psn_range": [f"{initial_psn(self.qp):06x}",
                          f"{(initial_psn(self.qp) + self.sent_new - 1) & PSN_MASK:06x}"],
            "undelivered": len(self.outstanding), "losses": self.losses,
            "retransmissions": self.retransmissions,
            "send_errors": self.nic.send_errors,
            "evs": {f"{ev:x}": {k: v for k, v in e.items() if k not in ("skip_until", "srtt")}
                    for ev, e in self.evs.items()},
            **self.rec.export(),
        }


class RoceSender:
    """Single path (one EV), go-back-N: a stand-in for RoCEv2 RC over ECMP."""

    def __init__(self, args, config):
        self.args, self.config = args, config
        self.nic = make_nic(config, args.host)
        self.peer, self.qp = args.peer, args.qp
        self.ev = args.evs[0] if args.evs else ev_set(config, args.host, args.peer, args.qp)[0]
        self.base = initial_psn(self.qp)
        self.cack = -1  # unwrapped offsets from base
        self.next = 0
        self.highest = -1
        self.sent_at = {}
        self.rec = Recorder()
        self.delivered = self.retransmissions = self.timeouts = self.naks = 0

    def psn(self, offset):
        return (self.base + offset) & PSN_MASK

    def unwrap(self, psn):
        delta = (psn - self.psn(self.cack)) & PSN_MASK
        if delta >= 1 << 23:
            delta -= 1 << 24
        return self.cack + delta

    def transmit(self, offset):
        header = HEADER.pack(DATA, FLAG_ROCE, self.qp, self.psn(offset), self.ev, now_us())
        self.nic.send(self.peer, self.ev, header + pattern(self.qp, self.psn(offset), self.args.size))
        self.sent_at[offset] = time.time()
        if offset <= self.highest:
            self.retransmissions += 1
            self.rec.count("retransmitted", ev=self.ev)
        else:
            self.rec.count("sent", ev=self.ev)
        self.highest = max(self.highest, offset)

    def go_back(self, reason):
        self.rec.event("go_back_n", reason=reason, from_psn=self.psn(self.cack + 1))
        self.next = self.cack + 1

    def run(self):
        args = self.args
        start = time.time()
        stop_new = start + args.duration
        deadline = stop_new + args.drain
        interval = 1.0 / args.rate
        next_send = start
        while True:
            now = time.time()
            if now >= deadline or (now >= stop_new and self.cack >= self.highest):
                break
            for payload, _ in self.nic.receive():
                opcode, _, qp, _, _, _ = HEADER.unpack(payload[:HEADER.size])
                if qp != self.qp or opcode not in (SACK, NAK):
                    continue
                cack = self.unwrap(Sack.decode(payload[HEADER.size:HEADER.size + 36]).cack_psn)
                if cack > self.cack:
                    self.delivered += cack - self.cack
                    self.rec.count("delivered", cack - self.cack, ev=self.ev)
                    self.cack = cack
                    self.next = max(self.next, cack + 1)
                if opcode == NAK:
                    self.naks += 1
                    self.go_back("NAK sequence error")
            oldest = self.sent_at.get(self.cack + 1)
            if oldest and self.cack < self.highest and now - oldest > args.rto:
                self.timeouts += 1
                self.rec.count("lost", ev=self.ev)
                self.go_back("timeout")
                self.sent_at[self.cack + 1] = now
            if now >= next_send and self.next - self.cack <= min(args.window, args.inflight):
                if self.next <= self.highest or now < stop_new:
                    next_send = max(next_send + interval, now - 0.01)
                    self.transmit(self.next)
                    self.next += 1
            select.select(self.nic.filenos(), [], [],
                          min(max(0.0, next_send - time.time()), 0.001))
        return {"algo": "roce", "fabric": self.config["fabric"], "host": args.host,
                "peer": self.peer, "qp": self.qp, "ev": f"{self.ev:x}",
                "rate_pps": args.rate, "duration": args.duration, "size": args.size,
                "started": start, "finished": time.time(),
                "sent_new": self.highest + 1, "delivered": self.delivered,
                "undelivered": self.highest - self.cack, "losses": self.timeouts,
                "retransmissions": self.retransmissions, "naks": self.naks,
                "send_errors": self.nic.send_errors, **self.rec.export()}


class Responder:
    """Receiver side for any number of QPs; also keeps a reverse EV per plane."""

    def __init__(self, args, config):
        self.args, self.config, self.host = args, config, args.host
        self.nic = make_nic(config, args.host, listen=True)
        self.qps = {}
        self.ports = port_states(config, args.host)
        self.rng = random.Random(args.seed)
        self.rec = Recorder()

    def state(self, src, qp, roce):
        key = (src, qp)
        if key not in self.qps:
            peer = host_of(self.config, src)
            reverse = ev_set(self.config, self.host, peer, qp)
            self.qps[key] = {
                "peer": peer, "roce": roce, "received": 0, "duplicates": 0,
                "dropped_out_of_order": 0, "per_ev": {}, "ce_per_ev": {},
                "receiver": None if roce else Receiver(initial_psn(qp), qp, qp, 128),
                "expected": 0, "nak_sent": False, "seen": set(),
                "reverse_all": reverse, "reverse": {}, "reverse_probe": {},
                "reverse_failed": {}, "sack_ev": {},
                "peer_port": UDP_PORT, "last_data": 0.0,
                "reverse_changes": 0,
            }
        return self.qps[key]

    def reverse_ev(self, qp_state, plane):
        """Reverse EV for control packets: prefer a confirmed one on the data's plane."""
        planes = qp_state["reverse"]
        good = [p for p, r in planes.items() if r["confirmed"] and self.port_ok(r["ev"])]
        if plane in good:
            return planes[plane]["ev"]
        if good:
            return planes[self.rng.choice(good)]["ev"]
        now = time.time()
        candidates = [ev for ev in qp_state["reverse_all"] if self.port_ok(ev)]
        fresh = [ev for ev in candidates if now - qp_state["reverse_failed"].get(ev, 0.0)
                 > 4 * self.args.probe_timeout]
        return self.rng.choice(fresh or candidates or qp_state["reverse_all"])

    def suspect_reverse(self, qp_state, ev):
        """A duplicate arrived: the SACK we sent on reverse EV `ev` was lost."""
        qp_state["reverse_failed"][ev] = time.time()
        for plane, entry in qp_state["reverse"].items():
            if entry["ev"] == ev and entry["confirmed"]:
                entry["confirmed"] = False
                self.rec.event("reverse_ev_suspect", plane=plane, ev=ev)

    def port_ok(self, ev):
        port = ev_port(self.config, ev)
        return port is None or bool(self.ports >> port & 1)

    def send(self, qp_state, ev, payload):
        self.nic.send(qp_state["peer"], ev, payload, dport=qp_state["peer_port"])

    def on_data(self, payload, meta):
        opcode, flags, qp, psn, ev, ts = HEADER.unpack(payload[:HEADER.size])
        st = self.state(meta["src"], qp, bool(flags & FLAG_ROCE))
        st["peer_port"] = meta["sport"]
        st["last_data"] = time.time()
        body = payload[HEADER.size:]
        if body[:8] != pattern(qp, psn, 8):
            raise RuntimeError("Corrupt payload")
        key = f"{ev:x}"
        st["per_ev"][key] = st["per_ev"].get(key, 0) + 1
        if meta["ce"]:
            st["ce_per_ev"][key] = st["ce_per_ev"].get(key, 0) + 1
        self.rec.count("received", ev=ev)
        reply_ev = self.reverse_ev(st, ev_plane(self.config, ev))
        if st["roce"]:
            offset = (psn - initial_psn(qp)) & PSN_MASK
            if offset == st["expected"]:
                st["expected"] += 1
                st["received"] += 1
                st["nak_sent"] = False
                opcode = SACK
            elif offset > st["expected"] and offset - st["expected"] < 1 << 23:
                # RoCE RC discards out-of-order packets and NAKs once per gap.
                st["dropped_out_of_order"] += 1
                if st["nak_sent"]:
                    return
                st["nak_sent"] = True
                opcode = NAK
            else:
                st["duplicates"] += 1
                opcode = SACK
            cack = (initial_psn(qp) + st["expected"] - 1) & PSN_MASK
            sack = Sack(ack_psn_offset=0, entropy=ev, spdcid=qp, dpdcid=qp,
                        cack_psn=cack, sack_offset=0, sack_bitmap=0)
            self.send(st, reply_ev, HEADER.pack(opcode, 0, qp, psn, ev, ts)
                      + sack.encode() + SACK_EXT.pack(self.ports, meta["ce"]))
            return
        try:
            reception = st["receiver"].receive(psn, nominal_bytes=len(body), entropy=ev,
                                               tx_timestamp=ts & 0xFFFF)
        except WindowError:
            st["duplicates"] += 1
            return
        if reception.is_new:
            st["received"] += 1
        else:
            st["duplicates"] += 1
            if psn in st["sack_ev"]:
                self.suspect_reverse(st, st["sack_ev"][psn])
                reply_ev = self.reverse_ev(st, ev_plane(self.config, ev))
        st["sack_ev"][psn] = reply_ev
        if len(st["sack_ev"]) > 4096:
            del st["sack_ev"][next(iter(st["sack_ev"]))]
        self.send(st, reply_ev, HEADER.pack(SACK, 0, qp, psn, ev, ts)
                  + reception.sack.encode() + SACK_EXT.pack(self.ports, meta["ce"]))

    def probe_reverse(self):
        """Each plane keeps one reverse EV confirmed by an acknowledged EV probe."""
        now = time.time()
        for st in self.qps.values():
            if st["roce"] or now - st["last_data"] > 1.0:
                continue
            by_plane = {}
            for ev in st["reverse_all"]:
                by_plane.setdefault(ev_plane(self.config, ev), []).append(ev)
            for plane, evs in by_plane.items():
                current = st["reverse"].get(plane)
                pending = st["reverse_probe"].get(plane)
                if pending and now - pending["sent"] > self.args.probe_timeout:
                    del st["reverse_probe"][plane]
                    st["reverse_failed"][pending["ev"]] = now
                    self.rec.event("reverse_probe_timeout", plane=plane, ev=pending["ev"])
                    if current and current["ev"] == pending["ev"]:
                        current["confirmed"] = False
                    pending = None
                if pending:
                    continue
                stale = (current is None or not current["confirmed"]
                         or now - current["at"] > self.args.reverse_refresh
                         or not self.port_ok(current["ev"]))
                if not stale:
                    continue
                choices = [ev for ev in evs if self.port_ok(ev)] or evs
                # Prefer candidates that have not just failed a probe.
                fresh = [ev for ev in choices
                         if now - st["reverse_failed"].get(ev, 0.0) > 4 * self.args.probe_timeout]
                if current and current["confirmed"] and self.port_ok(current["ev"]):
                    candidate = current["ev"]
                else:
                    candidate = self.rng.choice(fresh or choices)
                probe_id = self.rng.randrange(1 << 24)
                st["reverse_probe"][plane] = {"ev": candidate, "id": probe_id, "sent": now}
                qp = next(q for (s, q), v in self.qps.items() if v is st)
                self.send(st, candidate, HEADER.pack(EV_PROBE, 0, qp, probe_id, candidate,
                                                     now_us()))

    def on_ev_probe_resp(self, payload, meta):
        _, _, qp, probe_id, ev, _ = HEADER.unpack(payload[:HEADER.size])
        st = self.qps.get((meta["src"], qp))
        if st is None:
            return
        for plane, pending in list(st["reverse_probe"].items()):
            if pending["id"] == probe_id and pending["ev"] == ev:
                del st["reverse_probe"][plane]
                previous = st["reverse"].get(plane)
                if previous is None or previous["ev"] != ev:
                    st["reverse_changes"] += 1
                    self.rec.event("reverse_ev", plane=plane, ev=ev)
                st["reverse"][plane] = {"ev": ev, "confirmed": True, "at": time.time()}

    def serve(self):
        stop_file = RESULTS / f"stop-{self.host}"
        stop_file.unlink(missing_ok=True)
        (RESULTS / f"ready-{self.host}").write_text("ready")
        next_timer = time.time()
        try:
            while not stop_file.exists():
                select.select(self.nic.filenos(), [], [], 0.002)
                for payload, meta in self.nic.receive():
                    if len(payload) < HEADER.size:
                        continue
                    opcode, _, qp, psn, ev, ts = HEADER.unpack(payload[:HEADER.size])
                    if opcode == DATA:
                        self.on_data(payload, meta)
                    elif opcode == PROBE:
                        self.rec.count("probe_rx", ev=ev)
                        st = self.state(meta["src"], qp, False)
                        st["peer_port"] = meta["sport"]
                        self.send(st, self.reverse_ev(st, ev_plane(self.config, ev)),
                                  HEADER.pack(PROBE_RESP, 0, qp, psn, ev, ts))
                    elif opcode == EV_PROBE_RESP:
                        self.on_ev_probe_resp(payload, meta)
                now = time.time()
                if now >= next_timer:
                    ports = port_states(self.config, self.host)
                    if ports != self.ports:
                        self.rec.event("local_port_bitmap", bitmap=ports)
                        self.ports = ports
                    self.probe_reverse()
                    next_timer = now + self.args.reverse_probe_interval
        finally:
            report = {f"{qp}": {k: v for k, v in st.items()
                                if k not in ("receiver", "seen", "reverse_all",
                                             "reverse_probe", "reverse_failed",
                                             "sack_ev")}
                      for (_, qp), st in self.qps.items()}
            for value in report.values():
                value["reverse"] = {str(p): {**r, "ev": f"{r['ev']:x}"}
                                    for p, r in value["reverse"].items()}
            (RESULTS / f"receiver-{self.host}.json").write_text(json.dumps(
                {"qps": report, **self.rec.export()}, indent=2) + "\n")
            (RESULTS / f"ready-{self.host}").unlink(missing_ok=True)


def clustermap(args, config):
    """Source-route probes to each attached T0, and via each T1, back to ourselves."""
    if config["fabric"] != "mrc":
        raise SystemExit("clustermap needs the SRv6 (mrc) fabric")
    nic = SrNic(config, args.host)
    me = config["hosts"][args.host]
    targets = []
    leaf = mrc_paths.LEAF_SIDE[me["index"]]
    for _, (plane, link) in sorted(me["ports"].items()):
        targets.append({"target": f"T0 {leaf}{plane} via port {link}", "plane": plane,
                        "link": link, "spine": None})
        for spine in range(1, config["spines"] + 1):
            targets.append({"target": f"T1 s{plane}{spine} via port {link}", "plane": plane,
                            "link": link, "spine": spine})
    for i, t in enumerate(targets):
        t.update(id=i, address=mrc_paths.self_probe_address(
            me["index"], t["plane"], t["link"], t["spine"]), sent=0, received=0, rtt_us=[])
    sent_at = {}
    for round_ in range(args.count):
        for t in targets:
            seq = round_ << 8 | t["id"]
            sent_at[seq] = time.time()
            nic.send(args.host, 0, HEADER.pack(SELF_PROBE, 0, 0, seq, 0, now_us()),
                     outer=t["address"])
            t["sent"] += 1
        deadline = time.time() + args.interval
        while time.time() < deadline:
            select.select(nic.filenos(), [], [], 0.002)
            for payload, _ in nic.receive():
                opcode, _, _, seq, _, _ = HEADER.unpack(payload[:HEADER.size])
                if opcode == SELF_PROBE and seq in sent_at:
                    t = targets[seq & 0xFF]
                    t["received"] += 1
                    t["rtt_us"].append(int((time.time() - sent_at.pop(seq)) * 1e6))
    for t in targets:
        rtts = sorted(t.pop("rtt_us"))
        t["median_rtt_us"] = rtts[len(rtts) // 2] if rtts else None
        t["ok"] = t["received"] == t["sent"]
    diagnosis = []
    for t in targets:
        if t["spine"] is None and not t["ok"]:
            diagnosis.append(f"NIC port plane {t['plane']} link {t['link']} or its T0 is bad")
        elif t["spine"] is not None and not t["ok"]:
            t0 = next(x for x in targets if x["spine"] is None and x["plane"] == t["plane"]
                      and x["link"] == t["link"])
            if t0["ok"]:
                diagnosis.append(f"T0-T1 link {leaf}{t['plane']}-s{t['plane']}{t['spine']} "
                                 f"(or spine s{t['plane']}{t['spine']}) is bad")
    return {"host": args.host, "targets": targets, "diagnosis": sorted(set(diagnosis))}


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("role", choices=["send", "serve", "stop", "clustermap"])
    parser.add_argument("--config", default="/opt/fabric.json")
    parser.add_argument("--host", required=True)
    parser.add_argument("--peer", default="h2")
    parser.add_argument("--algo", choices=["mrc", "roce"], default="mrc")
    parser.add_argument("--qp", type=int, default=1)
    parser.add_argument("--evs", type=lambda v: [int(x, 16) for x in v.split(",")],
                        help="comma-separated hex EVs (default: full EV set)")
    parser.add_argument("--active-per-plane", type=int, default=0,
                        help="active EVs per plane; the rest are backups (0 = all)")
    parser.add_argument("--rate", type=float, default=1000, help="packets per second")
    parser.add_argument("--duration", type=float, default=2.0)
    parser.add_argument("--drain", type=float, default=5.0)
    parser.add_argument("--size", type=int, default=256, help="payload bytes")
    parser.add_argument("--window", type=int, default=128,
                        help="PSN tracking window (MRC: multiple of 128)")
    parser.add_argument("--inflight", type=int, default=128,
                        help="packets in flight; a fixed window stands in for NSCC")
    parser.add_argument("--rto", type=float, default=0.02)
    parser.add_argument("--probe-interval", type=float, default=0.02)
    parser.add_argument("--probe-timeout", type=float, default=0.05)
    parser.add_argument("--resurrect-after", type=int, default=3)
    parser.add_argument("--ecn-skip", type=float, default=0.004,
                        help="seconds to avoid an EV after a CE echo")
    parser.add_argument("--no-ecn-avoid", action="store_true")
    parser.add_argument("--reverse-probe-interval", type=float, default=0.01)
    parser.add_argument("--reverse-refresh", type=float, default=0.03,
                        help="re-confirm each plane's reverse EV this often (seconds)")
    parser.add_argument("--count", type=int, default=20)
    parser.add_argument("--interval", type=float, default=0.05)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--start-at", type=float, default=0.0,
                        help="wall-clock time to start sending (aligns fault injection)")
    parser.add_argument("--output")
    args = parser.parse_args()
    config = json.loads(Path(args.config).read_text())
    if args.role == "stop":
        (RESULTS / f"stop-{args.host}").touch()
        deadline = time.time() + 5
        while (RESULTS / f"ready-{args.host}").exists():
            if time.time() > deadline:
                raise SystemExit("Responder did not stop")
            time.sleep(0.05)
        return
    if args.role == "serve":
        Responder(args, config).serve()
        return
    if args.role == "clustermap":
        result = clustermap(args, config)
    else:
        sender = (MrcSender if args.algo == "mrc" else RoceSender)(args, config)
        time.sleep(max(0.0, args.start_at - time.time()))
        sender.rec.t0 = time.time()
        result = sender.run()
        print(f"{args.algo} qp={args.qp}: delivered {result['delivered']}/"
              f"{result['sent_new']}, losses={result['losses']}, "
              f"retransmissions={result['retransmissions']}")
    text = json.dumps(result, indent=2) + "\n"
    if args.output:
        Path(args.output).write_text(text)
    else:
        print(text)


if __name__ == "__main__":
    main()
