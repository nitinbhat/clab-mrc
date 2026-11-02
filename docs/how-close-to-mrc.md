# What this lab is, how close it is to MRC, and what it proves

## In one paragraph

MRC (Multipath Reliable Connection) is a transport implemented **in RDMA NIC
hardware**, working with **switch features** (SRv6 uSID forwarding, ECN,
packet trimming) in a multi-plane Clos fabric. This lab rebuilds the **fabric
half for real** (Linux kernel SRv6 uSID forwarding, FRR, containerlab) and the
**NIC half as a userspace Python model** of MRC's packet-level behaviour. It
lets you watch the design decisions work, and fail, under controlled faults. It
does not reproduce MRC's hardware, speed or wire format.

## Why it is not a full MRC implementation

"No RDMA" is only part of the answer. A real MRC deployment needs things a
laptop lab cannot supply:

| Needed for real MRC | Why the lab cannot have it |
|---|---|
| RDMA NIC with MRC in silicon | Packets are produced by Python processes over UDP sockets. No verbs, no queue pairs in hardware, no memory registration, no write placement. |
| MRC wire format (BTH/RETH extensions) | The lab uses its own small header. Only the SACK body follows the OCP MRC 1.0 specification. A real MRC NIC cannot talk to this lab. |
| Microsecond reaction times | Python timers: 20 ms retransmit and probe intervals. Ratios carry over, absolute times (19 ms here) do not. |
| Packet trimming and trim NACKs | Needs switch ASIC support, the Linux kernel cannot trim. Without it, congestion loss is detected by timeout, not instantly. |
| NSCC congestion control | Replaced by a fixed window and fixed pacing rate. |
| Scale: 128-256 EVs per QP, 8 planes, hundreds of switches | 8 paths, 2 planes, 10 switches. |
| Production routing: per-T0-uplink uSIDs, templates per destination row | Simplified uSID plan for a fixed two-host topology. |

The complete list is in [walkthrough section 11](walkthrough.md#11-what-is-not-modelled).

## Fidelity, by layer

| Layer | Fidelity | Notes |
|---|---|---|
| Switch forwarding (uN uSID `End` + `next-csid`, static routes, IPv6-in-IPv6, no routing protocol) | **High**: the real Linux kernel (6.1+) forwards the packets, and `preflight` checks that it really shifts the uSID | Packet captures at four hops show the shifting |
| Baseline (eBGP + ECMP + optional BFD) | **High** for behaviour: real FRR, real kernel ECMP hashing | Timers (1 s / 3 s) are faster than common defaults, which favours the baseline |
| EV spraying, per-plane balancing, EV-to-path mapping | **Medium-high**: same idea and fields, lab-sized | EV in UDP source port and flow label |
| SACK-based selective retransmission, PSNs | **Medium**: SACK body follows the spec, surrounding header is lab-specific | |
| Failure handling: retire an EV on first loss, same-plane backup, probe, resurrect, port bitmap, reverse EVs | **Medium**: the logic of the paper, simplified, with lab-specific opcodes | Timing is Python-scale |
| ECN as a load-balancing signal | **Medium** | Real marking by Linux qdisc, reaction in the Python sender |
| Clustermapper | **Low-medium**: single-host demo | |
| Congestion control, trimming, RDMA semantics, scale | **None** | |

## What the lab proves

- **Switches can forward MRC traffic with no routing protocol.** A fabric of
  static routes plus one uSID `End` per switch forwards sprayed traffic across
  planes, with nothing running that could go wrong at runtime.
- **Some failures are invisible to routing but visible to the endpoint.** A
  silent link, a gray spine (control plane up, data plane dead), a partly lossy
  link, and a lost NIC plane all leave BGP convinced the path is healthy. An
  endpoint that watches its own packets notices within milliseconds, and the
  lab shows both behaviours on the same fault.
- **Spraying avoids flow-collision hot spots.** With random ECMP hashes, flows
  collide and starve, a sprayed flow uses all uplinks.
- **Retiring a path, probing it and bringing it back works** as a mechanism,
  including under partial loss.
- **Selective retransmission wastes less than go-back-N**: 1.0% against 4.6%
  retransmitted at 1% random loss.

## What it does not prove

- **Absolute performance.** 2,000 pkt/s over Python says nothing about 800 Gb/s
  NICs. Treat times as relative (MRC is about 100 times faster to react than the
  hold timer here, not "19 ms").
- **Interoperability or spec compliance.** The lab is not an MRC implementation
  and passes no conformance test.
- **That BGP is a poor choice in general.** The baseline flow is deliberately
  pinned onto the failing path so the failure is hit every time (a random flow
  would hit it half the time here), and the baseline is a single-path flow.
  The result is "this class of fault is invisible to routing", not "BGP always
  loses". BFD closes the gap for silent links (0.29 s) but not for a gray spine.
- **The paper's quantitative results.** The go-back-N throughput collapse at 1%
  loss does **not** appear here, only the retransmission overhead does. The
  lab shows the mechanisms, not the paper's numbers.
- **Behaviour at scale**: thousands of NICs, congestion control interplay,
  collectives such as all-reduce, multi-tenant traffic.

## How to use it

Use it to **understand the design**: why spraying, why static uSIDs, what an
endpoint can see that a router cannot. Do not use it to benchmark, to qualify
hardware or to claim MRC compliance.
