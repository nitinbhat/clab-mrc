#!/usr/bin/env python3
"""Educational JSON/UDP transport, NOT an interoperable MRC implementation."""
import argparse
import hashlib
import json
import os
from pathlib import Path
import random
import socket
import time
import uuid

RESULTS = Path("/results")
PORT = 49000


def payload(seq):
    return hashlib.sha256(f"mrc-poc:{seq}".encode()).hexdigest()


class Selector:
    def __init__(self, paths, seed):
        self.paths = paths
        self.random = random.Random(seed)
        self.order = []
        self.stats = {p["ev"]: {"state": "GOOD", "sent": 0, "acked": 0,
                                "timeouts": 0, "rtt_ms": []} for p in paths}

    def choose(self):
        self.order = [ev for ev in self.order if self.stats[ev]["state"] == "GOOD"]
        if not self.order:
            self.order = [ev for ev, s in self.stats.items() if s["state"] == "GOOD"]
            self.random.shuffle(self.order)
        if not self.order:
            raise RuntimeError("No healthy paths remain")
        return self.order.pop()

    def timeout(self, ev):
        self.stats[ev]["timeouts"] += 1
        self.stats[ev]["state"] = "ASSUMED_BAD"


def send(args):
    paths = json.loads(Path(args.paths).read_text())
    selector = Selector(paths, args.seed)
    destinations = {p["ev"]: p["destination"] for p in paths}
    session = uuid.uuid4().hex
    pending = {}
    delivered = set()
    next_seq = 0
    retransmissions = 0
    next_send = 0
    started = time.monotonic()
    deadline = started + args.deadline
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.bind(("fc00:100::1", 0))
        sock.settimeout(0.001)

        def transmit(seq, attempts=1):
            ev = selector.choose()
            packet = {"session": session, "seq": seq, "ev": ev,
                      "attempt": attempts, "payload": payload(seq)}
            sock.sendto(json.dumps(packet).encode(), (destinations[ev], PORT))
            pending[seq] = (ev, time.monotonic(), attempts)
            selector.stats[ev]["sent"] += 1

        while len(delivered) < args.count:
            now = time.monotonic()
            if now > deadline:
                raise RuntimeError(f"Transfer expired: {len(delivered)}/{args.count} delivered")
            if next_seq < args.count and len(pending) < args.window and now >= next_send:
                transmit(next_seq)
                next_seq += 1
                next_send = now + args.interval
            try:
                raw, _ = sock.recvfrom(8192)
            except socket.timeout:
                pass
            else:
                ack = json.loads(raw)
                seq = ack["seq"]
                if ack["session"] == session and seq in pending:
                    if ack["payload"] != payload(seq):
                        raise RuntimeError("Receiver reported corrupt payload")
                    ev, sent, attempt = pending[seq]
                    # Ignore late ACKs from older attempts: EV attribution must be exact.
                    if ack["attempt"] == attempt and ack["ev"] == ev:
                        del pending[seq]
                        delivered.add(seq)
                        selector.stats[ev]["acked"] += 1
                        selector.stats[ev]["rtt_ms"].append(
                            round((time.monotonic() - sent) * 1000, 3))
            for seq, (ev, sent, attempts) in list(pending.items()):
                if time.monotonic() - sent >= args.timeout:
                    selector.timeout(ev)
                    if attempts >= args.retries:
                        raise RuntimeError(f"Retry limit reached for packet {seq}")
                    transmit(seq, attempts + 1)
                    retransmissions += 1
    result = {"session": session, "count": args.count, "delivered": len(delivered),
              "retransmissions": retransmissions,
              "duration_seconds": round(time.monotonic() - started, 3),
              "paths": selector.stats}
    Path(args.output).write_text(json.dumps(result, indent=2) + "\n")
    print(f"{args.output}: {len(delivered)}/{args.count}, retries={retransmissions}")


def receive(args):
    if args.reset:
        if (RESULTS / "receiver.pid").exists():
            raise RuntimeError("Receiver PID file exists; stop the receiver before resetting")
        for name in ("receiver-ready", "receiver-stop"):
            (RESULTS / name).unlink(missing_ok=True)
        return
    stop_file = RESULTS / "receiver-stop"
    sessions = {}
    with socket.socket(socket.AF_INET6, socket.SOCK_DGRAM) as sock:
        sock.bind(("::", PORT))
        sock.settimeout(0.1)
        (RESULTS / "receiver.pid").write_text(str(os.getpid()))
        (RESULTS / "receiver-ready").write_text("ready")
        try:
            while not stop_file.exists():
                try:
                    raw, peer = sock.recvfrom(8192)
                except socket.timeout:
                    continue
                data = json.loads(raw)
                if data["payload"] != payload(data["seq"]):
                    raise RuntimeError("Corrupt data received")
                state = sessions.setdefault(data["session"], {
                    "received": set(), "duplicates": 0, "out_of_order": 0, "next": 0})
                seq = data["seq"]
                if seq in state["received"]:
                    state["duplicates"] += 1
                else:
                    if seq != state["next"]:
                        state["out_of_order"] += 1
                    state["received"].add(seq)
                    while state["next"] in state["received"]:
                        state["next"] += 1
                # Per-packet ACK models selective feedback, not the MRC SACK wire format.
                sock.sendto(json.dumps(data).encode(), peer)
        finally:
            report = {session: {**state, "received": len(state["received"])}
                      for session, state in sessions.items()}
            (RESULTS / "receiver.json").write_text(json.dumps(report, indent=2) + "\n")
            (RESULTS / "receiver-ready").unlink(missing_ok=True)
            (RESULTS / "receiver.pid").unlink(missing_ok=True)


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("role", choices=["send", "receive", "stop"])
    parser.add_argument("--paths", default="/opt/paths.json")
    parser.add_argument("--output", default="/results/transfer.json")
    parser.add_argument("--count", type=int, default=400)
    parser.add_argument("--window", type=int, default=32)
    parser.add_argument("--timeout", type=float, default=0.2)
    parser.add_argument("--retries", type=int, default=8)
    parser.add_argument("--deadline", type=float, default=30)
    parser.add_argument("--interval", type=float, default=0.001)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--reset", action="store_true")
    args = parser.parse_args()
    if min(args.count, args.window, args.retries) <= 0:
        parser.error("count, window, and retries must be positive")
    if args.timeout <= 0 or args.deadline <= 0 or args.interval < 0:
        parser.error("timeout/deadline must be positive and interval nonnegative")
    if args.role == "send":
        send(args)
    elif args.role == "receive":
        receive(args)
    else:
        (RESULTS / "receiver-stop").touch()
        deadline = time.monotonic() + 5
        while (RESULTS / "receiver.pid").exists():
            if time.monotonic() > deadline:
                raise RuntimeError("Receiver did not stop")
            time.sleep(0.1)


if __name__ == "__main__":
    main()
