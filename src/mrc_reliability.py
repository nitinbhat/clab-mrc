"""Partial OCP MRC 1.0 reliability reference model, not a network endpoint."""
from __future__ import annotations

from dataclasses import dataclass, field
import json
import struct
from typing import Optional

PSN_MODULUS = 1 << 24
PSN_MASK = PSN_MODULUS - 1


def _uint(name: str, value: int, bits: int) -> None:
    if type(value) is not int or not 0 <= value < (1 << bits):
        raise ValueError(f"{name} must be an unsigned {bits}-bit integer")


def _sint16(name: str, value: int) -> None:
    if type(value) is not int or not -(1 << 15) <= value < (1 << 15):
        raise ValueError(f"{name} must be a signed 16-bit integer")


def _delta(psn: int, base: int) -> int:
    difference = (psn - base) & PSN_MASK
    if difference == PSN_MODULUS // 2:
        raise ValueError("PSNs separated by half the sequence space are ambiguous")
    return difference if difference < PSN_MODULUS // 2 else difference - PSN_MODULUS


def _window(value: int) -> None:
    if type(value) is not int or not 128 <= value <= 255 * 128 or value % 128:
        raise ValueError("window must be 128..32640 packets in multiples of 128")


@dataclass(frozen=True)
class CcState:
    """The eight-byte CC_TYPE=0 layout in MRC 1.0, Tables 7-12/7-13."""

    tx_timestamp: int = 0
    ooo_count: int = 0
    restore_cwnd: bool = False
    rcv_cwnd_pen: int = 0
    rcvd_bytes: int = 0

    def __post_init__(self) -> None:
        for name, bits in (("tx_timestamp", 16), ("ooo_count", 15),
                           ("rcv_cwnd_pen", 7), ("rcvd_bytes", 24)):
            _uint(name, getattr(self, name), bits)
        if type(self.restore_cwnd) is not bool:
            raise ValueError("restore_cwnd must be bool")

    def encode(self) -> bytes:
        last_word = (int(self.restore_cwnd) << 31 |
                     self.rcv_cwnd_pen << 24 | self.rcvd_bytes)
        return struct.pack("!HHI", self.tx_timestamp, self.ooo_count, last_word)

    @classmethod
    def decode(cls, data: bytes) -> CcState:
        if len(data) != 8:
            raise ValueError("CC_STATE must contain exactly 8 bytes")
        timestamp, out_of_order, last_word = struct.unpack("!HHI", data)
        return cls(timestamp, out_of_order & 0x7FFF, bool(last_word >> 31),
                   (last_word >> 24) & 0x7F, last_word & PSN_MASK)


@dataclass(frozen=True)
class Sack:
    """SETH plus CC_TYPE=0 CC_STATE (36 bytes), without BTH or encapsulation."""

    ack_psn_offset: int
    entropy: int
    spdcid: int
    dpdcid: int
    cack_psn: int
    sack_offset: int
    sack_bitmap: int
    cc_state: CcState = field(default_factory=CcState)
    m: int = 0
    pr: bool = False
    mpr: int = 0

    def __post_init__(self) -> None:
        for name, bits in (("entropy", 32), ("spdcid", 16), ("dpdcid", 16),
                           ("cack_psn", 24), ("sack_bitmap", 64), ("mpr", 8)):
            _uint(name, getattr(self, name), bits)
        _uint("m", self.m, 2)
        if self.m == 3:
            raise ValueError("SACK m=3 is reserved")
        if type(self.pr) is not bool:
            raise ValueError("pr must be bool")
        if self.pr:
            _uint("probe_id in ack_psn_offset", self.ack_psn_offset, 16)
        else:
            _sint16("ack_psn_offset", self.ack_psn_offset)
        _sint16("sack_offset", self.sack_offset)
        if not isinstance(self.cc_state, CcState):
            raise ValueError("cc_state must be CcState")

    @property
    def ack_psn(self) -> int:
        if self.pr:
            raise ValueError("A probe response carries an identifier, not an ACK PSN")
        return (self.cack_psn + self.ack_psn_offset) & PSN_MASK

    @property
    def sack_base(self) -> int:
        return (self.cack_psn + self.sack_offset) & PSN_MASK

    def encode(self) -> bytes:
        # Table 7-14: reserved bits are zero; M occupies byte 1 bits 6:5.
        flags = self.m << 5 | int(self.pr) << 1
        return (
            struct.pack("!H", flags)
            + self.ack_psn_offset.to_bytes(2, "big", signed=not self.pr)
            + struct.pack("!IHHI", self.entropy, self.spdcid, self.dpdcid, self.cack_psn)
            + bytes((0, self.mpr))
            + self.sack_offset.to_bytes(2, "big", signed=True)
            + self.sack_bitmap.to_bytes(8, "big")
            + self.cc_state.encode()
        )

    @classmethod
    def decode(cls, data: bytes) -> Sack:
        if len(data) != 36:
            raise ValueError("SETH plus CC_STATE must contain exactly 36 bytes")
        if data[16] >> 4:
            raise ValueError("Only CC_TYPE=0 is supported")
        pr = bool(data[1] & 2)
        entropy, spdcid, dpdcid, cack_word = struct.unpack("!IHHI", data[4:16])
        return cls(
            ack_psn_offset=int.from_bytes(data[2:4], "big", signed=not pr),
            entropy=entropy, spdcid=spdcid, dpdcid=dpdcid,
            cack_psn=cack_word & PSN_MASK,
            sack_offset=int.from_bytes(data[18:20], "big", signed=True),
            sack_bitmap=int.from_bytes(data[20:28], "big"),
            cc_state=CcState.decode(data[28:]), m=(data[1] >> 5) & 3,
            pr=pr, mpr=data[17],
        )


class WindowError(ValueError):
    """A transmission or arrival is outside this model's fixed PSN window."""


@dataclass(frozen=True)
class Reception:
    is_new: bool
    sack: Optional[Sack]


class Receiver:
    """One QP's non-trimmed reception state; no RDMA semantic processing."""

    def __init__(self, initial_psn: int, requestor_qp: int, responder_qp: int,
                 window: int = 128):
        _uint("initial_psn", initial_psn, 24)
        _uint("requestor_qp", requestor_qp, 24)
        _uint("responder_qp", responder_qp, 24)
        _window(window)
        self.window = window
        self.requestor_qp = requestor_qp
        self.responder_qp = responder_qp
        # Unwrapped positions make ordinary comparisons safe across PSN rollover.
        self._initial = initial_psn
        self._cack = initial_psn - 1
        self._max_rcv = self._cack
        self._lowest_unsacked = self._cack
        self._received: set[int] = set()
        self.rx_rcvd_bytes = 0

    @property
    def cack_psn(self) -> int:
        return self._cack & PSN_MASK

    @property
    def max_rcv_psn(self) -> int:
        return self._max_rcv & PSN_MASK

    @property
    def lowest_unsacked_psn(self) -> int:
        return self._lowest_unsacked & PSN_MASK

    def receive(self, psn: int, *, nominal_bytes: int, emit_sack: bool = True,
                entropy: int = 0, tx_timestamp: int = 0, m: int = 0) -> Reception:
        _uint("psn", psn, 24)
        _uint("nominal_bytes", nominal_bytes, 32)
        _uint("entropy", entropy, 32)
        _uint("tx_timestamp", tx_timestamp, 16)
        _uint("m", m, 2)
        if m == 3:
            raise ValueError("SACK m=3 is reserved")
        if type(emit_sack) is not bool:
            raise ValueError("emit_sack must be bool")
        distance = _delta(psn, self.cack_psn)
        if not -self.window < distance <= self.window:
            raise WindowError("PSN is outside the receiver's tracking/duplicate window")
        position = self._cack + distance
        if position < self._initial:
            raise WindowError("PSN predates this QP's initial sequence number")
        previous_cack = self._cack
        is_new = position > self._cack and position not in self._received
        if is_new:
            self._received.add(position)
            self._max_rcv = max(self._max_rcv, position)
            self.rx_rcvd_bytes = (self.rx_rcvd_bytes + nominal_bytes) & 0xFFFFFFFF
            while self._cack + 1 in self._received:
                self._cack += 1
                self._received.remove(self._cack)
        if not emit_sack:
            if self._cack > previous_cack and self._cack >= self._lowest_unsacked:
                self._lowest_unsacked = self._cack
            elif position < self._lowest_unsacked:
                self._lowest_unsacked = max(position, self._cack)
            return Reception(is_new, None)

        base = max(self._lowest_unsacked, self._cack)
        if base + 64 >= self._max_rcv:
            base = max(self._max_rcv - 64, self._cack)
        bitmap = sum(
            1 << bit for bit in range(64)
            if base + bit <= self._cack or base + bit in self._received
        )
        sack = Sack(
            ack_psn_offset=position - self._cack, entropy=entropy,
            spdcid=self.requestor_qp & 0xFFFF, dpdcid=self.responder_qp & 0xFFFF,
            cack_psn=self.cack_psn, sack_offset=base - self._cack,
            sack_bitmap=bitmap, m=m,
            # Fixed MPR is negotiated out of band; dynamic MPR is disabled (7.5.4).
            mpr=0,
            cc_state=CcState(
                tx_timestamp=tx_timestamp, ooo_count=len(self._received),
                rcvd_bytes=((self.rx_rcvd_bytes + 255) // 256) & PSN_MASK),
        )
        self._lowest_unsacked = base + 64
        return Reception(is_new, sack)


class Requestor:
    """Fixed-window transmission and positive SACK processing, without timers."""

    def __init__(self, initial_psn: int, requestor_qp: int, responder_qp: int,
                 window: int = 128):
        _uint("initial_psn", initial_psn, 24)
        _uint("requestor_qp", requestor_qp, 24)
        _uint("responder_qp", responder_qp, 24)
        _window(window)
        self.window = window
        self.requestor_qp = requestor_qp
        self.responder_qp = responder_qp
        self._initial = initial_psn
        self._cack = initial_psn - 1
        self._next = initial_psn
        self._outstanding: set[int] = set()

    @property
    def cack_psn(self) -> int:
        return self._cack & PSN_MASK

    @property
    def outstanding(self) -> tuple[int, ...]:
        return tuple(p & PSN_MASK for p in sorted(self._outstanding))

    def send(self) -> int:
        if self._next > self._cack + self.window:
            raise WindowError("New transmission would exceed the fixed PSN window")
        position = self._next
        self._outstanding.add(position)
        self._next += 1
        return position & PSN_MASK

    def apply_sack(self, sack: Sack) -> tuple[int, ...]:
        if (sack.spdcid != (self.requestor_qp & 0xFFFF) or
                sack.dpdcid != (self.responder_qp & 0xFFFF)):
            raise ValueError("SACK delivery contexts do not match this QP")
        cack = self._cack + _delta(sack.cack_psn, self.cack_psn)
        if not self._initial - 1 <= cack < self._next:
            raise ValueError("SACK cumulative ACK is outside the transmitted range")
        positive = {
            cack + sack.sack_offset + bit for bit in range(64)
            if sack.sack_bitmap & (1 << bit)
        }
        if not sack.pr:
            positive.add(cack + sack.ack_psn_offset)
        if any(not self._initial - 1 <= position < self._next for position in positive):
            raise ValueError("SACK acknowledges a packet that has not been sent")
        newly_acked = {
            position for position in self._outstanding
            if position <= cack or position in positive
        }
        self._outstanding.difference_update(newly_acked)
        self._cack = max(self._cack, cack)
        # Zero bitmap bits are not loss declarations; reordered packets may arrive.
        return tuple(p & PSN_MASK for p in sorted(newly_acked))


def demo() -> None:
    requestor = Requestor(0xFFFFFE, 1, 2)
    receiver = Receiver(0xFFFFFE, 1, 2)
    psns = [requestor.send() for _ in range(6)]
    last_sack = None
    for index in (3, 0, 5, 2, 4):
        result = receiver.receive(psns[index], nominal_bytes=256, entropy=index)
        last_sack = result.sack
        if last_sack is None:
            raise RuntimeError("Demo expected immediate feedback")
        requestor.apply_sack(Sack.decode(last_sack.encode()))
    missing = requestor.outstanding
    if missing != (0xFFFFFF,):
        raise RuntimeError("Selective feedback did not isolate the missing PSN")
    repair = receiver.receive(missing[0], nominal_bytes=256).sack
    if repair is None:
        raise RuntimeError("Demo expected repair feedback")
    requestor.apply_sack(Sack.decode(repair.encode()))
    if requestor.outstanding:
        raise RuntimeError("Reference transfer did not complete")
    print(json.dumps({
        "mode": "in-memory reference model, not a network MRC endpoint",
        "sent_psns": psns, "missing_before_repair": missing,
        "sack_before_repair_hex": last_sack.encode().hex(),
        "outstanding_after_repair": requestor.outstanding,
        "final_cack_psn": receiver.cack_psn,
    }, indent=2))


if __name__ == "__main__":
    demo()
