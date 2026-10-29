# Reliability reference milestone

`mrc_reliability.py` adds **MRC-specific, deterministic software components**.
It is not a complete MRC endpoint, a transport replacement, or evidence of
interoperability. The existing containerlab traffic generator still uses JSON/UDP.

The source is the [OCP MRC specification, revision 1.0, dated 2026-03-21][spec].
The field layouts were checked against the rendered PDF tables, not only
extracted text. Specification pseudocode is informative; this model follows
the selected algorithms described below without claiming full conformance.

## Run

From the repository root, with Python 3.9+ and no additional packages:

```sh
python3 -m unittest -v test_mrc_reliability
python3 mrc_reliability.py
```

The demo issues six PSNs across `0xFFFFFF -> 0`, withholds one packet, delivers
the others out of order, and passes binary SACK bodies between the two state
machines. Feedback identifies the remaining PSN as `0xFFFFFF`. The driver
manually supplies the missing packet; the cumulative ACK advances to 3 and
the requestor has no outstanding packets. There are **no sockets, automatic
retransmission timers, payload placement, or network traffic** in this demo.

## Implemented subset

| Component | Specification basis | Behavior |
|---|---|---|
| PSN arithmetic | Section 7.5 | 24-bit modulo PSNs; ordered internal positions across rollover |
| Receiver state | Table 7-5 | Cumulative ACK, highest received PSN, outstanding out-of-order reception set, lowest unsacked position |
| SACK generation | Section 7.5.2.2 | Lower-biased, rotating 64-bit bitmap; signed offsets relative to the cumulative ACK |
| SACK body codec | Tables 7-14/7-15 and continuation on page 49 | 28-byte SETH plus 8-byte CC_STATE, network byte order |
| CC_STATE codec | Tables 7-12/7-13 | Timestamp, out-of-order count, receiver-window penalty/restore fields, received-byte units |
| Positive feedback | Section 7.4.4 | Retire cumulatively, selectively, and explicitly acknowledged packets; tolerate repeated/reordered feedback |
| Fixed tracking window | Sections 7.4.4 and 7.5.4 | Prevent new sends beyond the negotiated PSN span, even if higher packets were selectively ACKed |
| Probe-response field interpretation | Sections 7.4.6 and 7.5.5.5 | Treat `ack_psn_offset` as an unsigned probe identifier when `pr=1`, not as a packet ACK |

`Sack.encode()` emits a **36-byte body**, not a complete SACK packet. It excludes
the BTH, Ethernet/IP/UDP headers, padding, and iCRC. `CcState` is a field codec:
it does not implement a congestion controller.

### Byte layout

| Byte offsets | Contents |
|---|---|
| 0-1 | Reserved fields, M in byte 1 bits 6:5, PR in byte 1 bit 1 |
| 2-3 | Signed triggering-packet offset, or unsigned probe ID |
| 4-7 | Reflected entropy |
| 8-11 | SPDCID and DPDCID, 16 bits each |
| 12-15 | Reserved byte followed by 24-bit cumulative ACK PSN |
| 16-19 | CC type/flags, MPR, signed bitmap offset |
| 20-27 | 64-bit SACK bitmap |
| 28-35 | CC_TYPE=0 CC_STATE |

Only CC_TYPE=0 is supported. Encoders emit zero for reserved/unused fields;
decoders ignore those fields. Unsupported CC types, reserved M=3, malformed
lengths, and out-of-range values raise explicit errors rather than being
silently truncated or interpreted as valid feedback.

### Model policies and interpretation boundaries

- Bitmap bit 0 represents `sack_base`; bit `i` represents `sack_base + i`,
  modulo `2^24`. The bitmap itself is encoded most-significant byte first.
  The text identifies the starting PSN but does not explicitly settle the
  bit-to-PSN orientation in an interoperable bit-index formula. This is the
  model's stated interpretation, **not an independently verified vendor mapping**.
- SPDCID is populated from the requestor QP's low 16 bits and DPDCID from the
  responder QP's low 16 bits, following Table 7-15. The surrounding specification
  contains differing context-population wording, including section 7.5.1.1.
  Independent clarification is needed before claiming wire interoperability.
  These 16-bit fields alone are not full QP identity validation; BTH addressing
  is not implemented.
- `receive(..., emit_sack=True)` uses immediate feedback by default. Passing
  `False` models coalescing for bitmap tests. The model does not implement the
  complete AR/end-of-flow, ECN, retransmission, and threshold-trigger policy.
- Tracking capacity is fixed at connection construction: 128 through 32,640
  packets, in multiples of 128. Dynamic MPR is not negotiated. Following section
  7.5.4's disabled-mode rule, generated SACKs have MPR=0 and the requestor ignores
  received MPR fields. The codec still supports nonzero MPR values.
- The receiver accepts recent duplicates strictly less than one tracking window
  behind the cumulative ACK. Older arrivals and arrivals predating this QP are
  rejected. This is a bounded model policy, not a complete duplicate/NACK policy.
- The driver supplies `nominal_bytes`. The model counts each accepted PSN once,
  wraps its byte counter at 32 bits, and reports rounded-up 256-byte units modulo
  `2^24`. Duplicate accounting is a model choice; it is not validation of the
  complete NSCC receive-accounting path.
- Serial comparisons assume feedback does not span half the PSN sequence space.
  Exactly half-space-separated values are rejected as ambiguous.
- Invalid SACK context IDs or ACKs for unsent packets are rejected before
  mutating requestor state. A zero bitmap bit does **not** declare loss:
  delayed/reordered packets can still arrive.

## Tests and evidence

There are 21 focused reference tests, in addition to the five existing lab and
simulator unit tests. They cover:

- A manually constructed fixed 36-byte vector covering field order, signed
  offsets, bitmap endpoints, and CC bitfields; this is not just a round-trip test.
- Unsigned probe identifiers, reserved-field normalization, unsupported CC types,
  invalid lengths, and fields that cannot be represented.
- Reception holes, duplicate suppression, window edges, byte-counter rollover,
  and PSN rollover.
- The bitmap example in section 7.5.2.2: PSNs 673/704/705/680, CACK 405, and offset
  268, with prior reception/feedback explicitly set up so the example applies.
- Lost feedback, reordered/duplicate SACKs, and the difference between outstanding
  packet count and the permitted sequence-number span.
- An in-memory 300-packet scenario crossing PSN rollover, with five data packets
  withheld, most SACKs discarded, remaining SACKs reordered, and binary feedback
  used to complete the remaining reception state.

The byte vector is hand-authored from the specification layout. It has not been
compared with a vendor NIC capture or an independent MRC implementation. The
300-packet test checks internal model consistency, not network interoperability
or RDMA semantics.

## Deliberately not implemented

The milestone omits BTH/request headers, NETH/NACK processing, full data and control
packet encapsulation, automatic loss detection, linear/exponential retry timers,
retry exhaustion/QP ERROR transitions, reliability/EV probe exchanges, dynamic MPR,
AR-triggered SACK completion, RTX reflection, NSCC, packet trimming, RDMA Write
placement and semantic ACKs, QP negotiation/APIs, and transport integration.

An empty `Requestor.outstanding` means only that this model's packet-reception
state was acknowledged. It does **not** mean an RDMA operation completed.
The next integration milestone must keep these layers distinct.

[spec]: https://www.opencompute.org/documents/ocp-mrc-1-0-pdf/
