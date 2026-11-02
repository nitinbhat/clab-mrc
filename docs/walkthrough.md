# Walkthrough: what this lab shows about MRC, and the evidence

This document maps the main claims of the OpenAI/Microsoft paper
[*Resilient AI Supercomputer Networking using MRC and SRv6*][paper] to what
this lab implements, the command that demonstrates each one, and the output
that proves it. All numbers and packet lines below come from
[`docs/evidence/multi-plane-multi-link/`](evidence/multi-plane-multi-link/README.md),
collected on Linux 6.1 with `sudo python3 lab.py evidence --fabric <baseline|mrc>`.
Re-running gives similar, not identical, numbers.

**Scope.** This is a learning lab. Endpoints are Python processes that behave
like an MRC NIC at packet level; there is no RDMA, no NIC hardware, and the
data header is a lab format. Timing resolution is milliseconds (userspace), not
the microseconds of a real NIC. Section 11 lists everything that is missing.

Contents:

1. [Setup and preflight](#1-setup-and-preflight)
2. [Topology: multi-plane, two tiers](#2-topology-multi-plane-two-tiers)
3. [Static SRv6 uSID routing instead of BGP](#3-static-srv6-usid-routing-instead-of-bgp)
4. [Packet format and per-hop shifting](#4-packet-format-and-per-hop-shifting)
5. [Spraying over an EV set](#5-spraying-over-an-ev-set)
6. [Reliability: PSNs, SACKs, selective retransmission](#6-reliability-psns-sacks-selective-retransmission)
7. [Failure handling inside the endpoint](#7-failure-handling-inside-the-endpoint)
8. [MRC versus BGP + ECMP under failures](#8-mrc-versus-bgp--ecmp-under-failures)
9. [ECN as a load-balancing signal](#9-ecn-as-a-load-balancing-signal)
10. [Clustermapper: source-routed self probes](#10-clustermapper-source-routed-self-probes)
11. [What is not modelled](#11-what-is-not-modelled)
12. [Fairness decisions and lab artefacts](#12-fairness-decisions-and-lab-artefacts)
13. [Reproduce everything](#13-reproduce-everything)

## 1. Setup and preflight

The `mrc` fabric needs native SRv6 uSID forwarding (`End` with the
`next-csid` flavor), which Linux added in **6.1**. On 5.10 the kernel accepts
the route but silently drops the flavor, so a route listing alone proves
nothing. `preflight` therefore forwards a real packet through three network
namespaces `a -> r -> b` in a throwaway container and requires `b` to receive
it with the destination already shifted:

```console
$ sudo python3 lab.py preflight --fabric mrc
preflight seg6local_end: OK
preflight seg6_encap: OK
preflight next_csid_retained: OK
preflight usid_forwarding: OK
```

`deploy` runs the same check first. Results are written to
`topologies/<lab>/results/capabilities.json`.

## 2. Topology: multi-plane, two tiers

> Paper  - 2: each NIC is split into several ports, each attached to a separate
> Clos plane; two tiers of switches (T0, T1) are enough for 100K+ GPUs.

The evidence profile `multi-plane-multi-link` has **two planes**, each with a
T0 leaf on each side (`aN` for h1, `bN` for h2) and two T1 spines (`sN1`,
`sN2`). Each host has **two NIC ports per plane** (four ports). Hosts are shared
by both planes; switches are not. Each topology directory has the exact
port-level diagram, e.g.
[topologies/multi-plane-multi-link-mrc/README.md](../topologies/multi-plane-multi-link-mrc/README.md).

| Paper | Lab |
|---|---|
| 8 planes x 100G or 4 x 200G per NIC | 2 planes, 2 ports each |
| T0 with 256 downlinks, T1 with 512 ports | 2 T0 and 2 T1 per plane |
| 128-256 EVs per QP | 8 distinct paths h1 -> h2 |

## 3. Static SRv6 uSID routing instead of BGP

> Paper  - 2.2: "We took the unusual position of disabling dynamic routing in
> the switches ... the switch left-shifts the uSID part of the address by 16
> bits ... This new address is then looked up in the switch's static
> forwarding table ... configured when the switch was installed, and is
> generally never changed."

Every switch owns one 16-bit uN uSID per role, allocated from the structure so
the addresses are readable in packet captures ([mrc_paths.py](../mrc_paths.py)):

| Node | uSID(s) | Meaning |
|---|---|---|
| a1 | `a101`, `a102` | leaf a, plane 1, NIC port 1 / 2 |
| s11, s12 | `5110`, `5120` | spine, plane 1, spine 1 / 2 |
| b1 | `b101`, `b102` | leaf b, plane 1, port 1 / 2 |
| h1 | `e111`, `e112`, `e121`, `e122` | host 1, plane, port |
| h2 | `e211`, ... | host 2, plane, port |

Installed **once at deploy**, on leaf a1 of the `mrc` fabric:

```console
$ docker exec clab-mrc-multi-plane-multi-link-mrc-a1 ip -6 -d route show | grep fcbb
unicast fcbb:0:5110::/48 nhid 46 via fd00:10:5::2 dev eth3 proto 196 ...
unicast fcbb:0:5120::/48 nhid 13 via fd00:10:6::2 dev eth4 proto 196 ...
unicast fcbb:0:a101::/48 encap seg6local action End flavors next-csid lblen 32 nflen 16 dev usid0 ...
unicast fcbb:0:a102::/48 encap seg6local action End flavors next-csid lblen 32 nflen 16 dev usid0 ...
unicast fcbb:0:e111::/48 nhid 22 via fd00:10:1::1 dev eth1 proto 196 ...
unicast fcbb:0:e112::/48 nhid 23 via fd00:10:3::1 dev eth2 proto 196 ...

$ docker exec clab-mrc-multi-plane-multi-link-mrc-a1 vtysh -c 'show ipv6 route static'
S>* fcbb:0:5110::/48 [1/0] via fd00:10:5::2, eth3, weight 1, 00:02:24
S>* fcbb:0:5120::/48 [1/0] via fd00:10:6::2, eth4, weight 1, 00:08:53
S>* fcbb:0:e111::/48 [1/0] via fd00:10:1::1, eth1, weight 1, 00:08:54
S>* fcbb:0:e112::/48 [1/0] via fd00:10:3::1, eth2, weight 1, 00:08:54

$ docker exec clab-mrc-multi-plane-multi-link-mrc-a1 sh -c 'pgrep -l bgpd || echo "no bgpd"'
no bgpd
```

That is the whole forwarding state of a1: two `End` (uN) entries for its own
uSIDs, and one static route per neighbour uSID. FRR is used only to hold
interface addresses and the static routes (`staticd` reinstalls them after a
link flap); no routing protocol runs. The `End` routes sit on a dummy device
(`usid0`) so a link failure cannot remove them.

The `baseline` fabric is the conventional design the paper replaces: eBGP on
every switch (timers 1 s / 3 s), ECMP with `maximum-paths 8`, plain IPv6:

```console
$ docker exec clab-mrc-multi-plane-multi-link-a1 vtysh -c 'show bgp ipv6 unicast summary'
Neighbor          V   AS   MsgRcvd MsgSent TblVer InQ OutQ Up/Down  State/PfxRcd PfxSnt
s11(fd00:10:5::2) 4 65013      665     701    100   0    0 00:03:07            9     14
s12(fd00:10:6::2) 4 65014      629     651    100   0    0 00:10:06            9     14

$ docker exec clab-mrc-multi-plane-multi-link-a1 ip -6 route show fc00:200::/48
fc00:200::/48 nhid 35 proto bgp metric 20 pref medium
        nexthop via fe80::a8c1:abff:fe5d:cf7 dev eth3 weight 1
        nexthop via fe80::a8c1:abff:fee9:c4b1 dev eth4 weight 1
```

## 4. Packet format and per-hop shifting

> Paper  - 2.2-2.3: "MRC packets are IPv6 in IPv6 encapsulated, with the outer
> destination address being the SRv6 path and the inner destination address
> containing the destination NIC's own address." "The 32-bit EV is striped
> across the UDP source port and IPv6 flow label."

The endpoint builds exactly that with a raw IPv6 socket (protocol 41): the
kernel adds the outer header; the endpoint writes the inner IPv6 + UDP header
(destination port 4791, as RoCEv2) with a valid checksum over the inner
addresses. From [`mrc/spray/1-h1-port1.txt`](evidence/multi-plane-multi-link/mrc/spray/1-h1-port1.txt)
(`tcpdump -v`, one data packet and one SACK, wrapped for reading):

```text
IP6 (class 0x02, flowlabel 0x4d2e3, hlim 64, next-header IPv6 (41) payload length: 320)
    fd00:10:1::1 > fcbb:0:a101:5120:b101:e211::          <- outer: the path
  IP6 (class 0x02, flowlabel 0x00001, hlim 64, next-header UDP (17) payload length: 280)
    fc00:100::1.274 > fc00:200::1.4791: UDP, length 272   <- inner: NIC to NIC
IP6 (class 0x02, flowlabel 0x873ab, hlim 61, next-header IPv6 (41) payload length: 102)
    fd00:10:2::1 > fcbb:0:e111:::                       <- SACK on its reverse EV, arrived
  IP6 (class 0x02, flowlabel 0x00001, hlim 64, next-header UDP (17) payload length: 62)
    fc00:200::1.274 > fc00:100::1.274: [udp sum ok] UDP, length 54
```

- Outer destination `fcbb:0:a101:5120:b101:e211::` reads as: leaf a1 port 1,
  spine s12, leaf b1 port 1, host h2 plane 1 port 1. There is no SRH: the whole
  path fits in one address (`fcbb:0` is the 32-bit block, then 16-bit uSIDs).
- Its EV `0x10112` is striped as in the paper: inner UDP source port `274`
  (`0x0112`: plane 1, port 1, spine 2) and inner flow label `0x00001` (the QP
  salt). The SACK echoes the same EV.
- The SACK arrives with its outer destination fully shifted to h1's own uSID
  (`fcbb:0:e111::`, hop limit 61 after three switches).
- `class 0x02` is ECT(0): the packets are ECN-capable (section 9).

Packets of EV `0x10111` (via s11) at four capture points show every switch
shifting the address by one uSID ([`mrc/spray/`](evidence/multi-plane-multi-link/mrc/spray/)):

| Capture point | Outer destination | Who shifted |
|---|---|---|
| leaving h1 port 1 | `fcbb:0:a101:5110:b101:e211::` | - |
| arriving at s11 | `fcbb:0:5110:b101:e211::` | a1 |
| b1 towards h2 | `fcbb:0:e211::` | s11, then b1 |
| arriving at h2 | `fcbb:0:e211::` | (h2's own uSID: deliver) |

The SRv6 address is erased by shifting, so (as the paper notes) the receiver
cannot echo it. It echoes the **EV** from the packet, and the sender rebuilds
the path algorithmically from the EV (`mrc_paths.ev_address`), with no
per-path state. This is the lab version of the paper's Fig. 3 template.

## 5. Spraying over an EV set

> Paper  - 2.1: "The sender then rotates through this set, using a different EV
> for each packet ... MRC chooses EVs equally split across the planes."

`spray` sends 2,000 packets from one QP with all 8 EVs active
([`mrc/spray/summary.json`](evidence/multi-plane-multi-link/mrc/spray/summary.json)):

| EV | 10111 | 10112 | 10121 | 10122 | 10211 | 10212 | 10221 | 10222 |
|---|---:|---:|---:|---:|---:|---:|---:|---:|
| packets | 254 | 250 | 252 | 248 | 249 | 251 | 247 | 250 |

Per plane: 1,004 / 997. Rotation is a shuffled round-robin, so the split is
equal by construction; the receiver counted the same numbers
(`mrc/spray/receiver.json`). Small differences come from the occasional EV
probe reply, which also uses an active EV.

For the failure experiments the sender keeps **2 active EVs per plane and 2
backups** (`--active-per-plane 2`), like CX-8 MRC's active plus backup EV
sets. A failed EV is replaced from the **same plane** (section 7), which keeps
the planes evenly loaded (paper  - 4).

## 6. Reliability: PSNs, SACKs, selective retransmission

> Paper  - 2.1: "MRC implements fast selective retransmission, using Selective
> ACK (SACK) packets to indicate precisely which packets have arrived."

The endpoint uses the specification-derived model in
[mrc_reliability.py](../mrc_reliability.py) on the wire (see
[reliability.md](reliability.md)):

- Every data packet takes a 24-bit PSN from `Requestor.send()`. Initial PSNs
  start a few hundred below 2^24, so every transfer crosses PSN rollover; the
  spray run used PSNs `fffef0` to `0006c0` (`psn_range` in its summary).
- The responder runs one `Receiver` per QP; each data packet produces the
  binary 36-byte SETH + CC_STATE SACK body (cumulative ACK, 64-bit bitmap, and
  the **echoed EV** in the `entropy` field), followed by a 2-byte lab extension
  (NIC port bitmap, CE echo).
- Loss detection is per path: a static source-routed path is FIFO, so when a
  SACK confirms packet *n* on EV *e*, any earlier packet sent on *e* that is
  still unacknowledged was lost ("gap on path"). A fixed 20 ms RTO catches
  paths that go silent entirely.
- A lost packet is retransmitted **alone**, on a different EV.

The receiver stores out-of-order packets (the paper's out-of-order placement
is reduced to a reception bitmap). The comparison baseline uses a RoCE-style
single path with **go-back-N**: the receiver drops out-of-order packets and
NAKs, and the sender resends everything from the hole. Section 8 shows the
difference: 9-13 retransmissions per fault for MRC versus 4,336-6,065 for
go-back-N whenever its path was cut.

## 7. Failure handling inside the endpoint

> Paper  - 2.1: "When a packet is not trimmed but actually lost, MRC assumes the
> path has failed and immediately stops using the corresponding EV ... MRC
> sends background path probes ... If enough probes succeed, the EV is
> resurrected."  - 3: "MRC uses a port state bitmap in SACK packets to notify
> remote QP endpoints that the port is down."

Each EV is `ACTIVE`, `BACKUP`, `ASSUMED_BAD` (retired, being probed) or
`DENIED` (behind a NIC port that is down). Event log of the `gray-switch`
run (spine s11 silently stops forwarding at t = 0, repaired at +2995 ms;
from [`mrc/gray-switch/sender.json`](evidence/multi-plane-multi-link/mrc/gray-switch/sender.json)):

```text
   +18.0 ms ev_state 10111 ACTIVE -> ASSUMED_BAD  timeout           first loss retires the EV
   +18.1 ms ev_activated 10112                                      same-plane backup promoted
 +3088.5 ms ev_state 10111 ASSUMED_BAD -> BACKUP  probes succeeded  3 probes in a row
 +3088.6 ms ev_resurrected 10111
```

The endpoint knew nothing about s11; it stopped using the broken path one
timeout after the fault and brought it back 93 ms after the repair. Which EVs
are active is random, so runs differ: when the promoted backup happens to
cross the failed spine as well, it is retired in turn about 20 ms later
(seen in development runs), as the paper
describes for startup ("packets are lost and retransmitted, and the EV is
swapped with one from the backup set"). Probes run every 20 ms on each retired
EV; three consecutive successes bring it back.

**NIC port failure and the port bitmap** (`nic-plane`: both of h2's plane-1
ports lose carrier at t = 0, recover at +1962 ms):

```text
    +6.6 ms peer_port_bitmap -> 12 (0b1100)   h2 reports ports 1 and 2 (plane 1) down in a SACK
    +6.6 ms ev_state 10111 ACTIVE -> DENIED   port down
    +6.7 ms ev_state 10112 BACKUP -> DENIED   port down
    +6.7 ms ev_state 10121 BACKUP -> DENIED   port down
    +6.7 ms ev_state 10122 ACTIVE -> DENIED   port down   (plane 1 no longer used)
 +1966.6 ms peer_port_bitmap -> 15            ports back
 +1966.7 ms ev_state 10111 DENIED -> ASSUMED_BAD  port up, probing   (all four)
 +2014.6 ms ev_state 10111 ASSUMED_BAD -> BACKUP  probes succeeded   (all four)
```

The sender never had to lose a packet on plane 1 to stop using it: h2
reported its port state. When the ports return, the EVs are **probed before
reuse**. An earlier version re-activated them immediately and lost a packet
because the leaf's static route was not reinstalled yet; "port up" is not
proof that a path works.

**Reverse paths.** ACKs, SACKs and probe replies use a small **reverse EV
set**: one confirmed EV per plane, chosen by the receiver and re-confirmed every
30 ms by EV probes that the sender answers (paper  - 2.4 "Reverse Paths"). SACKs
prefer the plane the data arrived on. A broken reverse path is dangerous: the
sender sees no SACKs and retires forward paths that work. The responder
therefore also treats a **duplicate** data packet as proof that the SACK it
sent for that PSN was lost, marks that reverse EV suspect and switches at
once. And if every forward EV ends up retired, the sender keeps trying retired
(but usable) EVs instead of going silent. Both matter mostly in small
topologies: on the single-plane profile, where a failed spine breaks one of
only two paths in both directions, they reduced the longest delivery gap from
590 ms to 10-30 ms. In the spray run both planes had a confirmed
reverse EV 2 ms after the first packet (`reverse_ev` events in
`mrc/spray/receiver.json`).

## 8. MRC versus BGP + ECMP under failures

The comparison runs the same fault on both fabrics while a sender transmits
2,000 packets/s:

- `baseline`: one RoCE-style QP. Its UDP source port (EV) is chosen with
  `ip route get` against the switches' real ECMP hash so that the flow crosses
  the fault (a flow that misses the fault would trivially see nothing).
- `mrc`: one MRC-style QP over its EV set, 2 active + 2 backup per plane.

During each run, the lab also polls what the **control plane** believes
(BGP session state, ECMP next hops) every 200 ms.

| Fault | Baseline: eBGP + ECMP | mrc: static uSID + MRC endpoint |
|---|---|---|
| `link-down`: a1-s11 administratively down | no interruption (local ECMP repair) | no interruption; EVs retired after 21 ms, back 82 ms after repair |
| `silent-link`: a1-s11 drops everything, carrier up | **nothing delivered for 2.27 s** (BGP hold timer); 4,562 retransmissions | no interruption; EVs retired after 19 ms, back 82 ms after repair |
| `gray-switch`: s11 stops forwarding, control plane up | **nothing delivered for 3.03 s, until repaired**; 6,065 retransmissions | no interruption; EVs retired after 18 ms, back 93 ms after repair |
| `nic-plane`: h2 loses both plane-1 NIC ports | **nothing delivered for 1.99 s, until repaired**; 4,336 retransmissions | no interruption; EVs denied 7 ms after the fault (port bitmap), back 53 ms after repair |

Full table and goodput charts:
[evidence README](evidence/multi-plane-multi-link/README.md).

What the control-plane monitor recorded explains each baseline result
(`control_plane_ms_after_fault` in each `baseline/*/summary.json`):

- **link-down**: the kernel removes a route's next hop the moment its own
  link goes down, and the flow rehashed to s12 without losing anything (the
  monitor, which samples only every ~0.3 s, saw the session `Idle` and one
  next hop left at its first sample after the fault, +273 ms). BGP + ECMP handles this case well, and the lab shows it honestly.
- **silent-link**: the BGP session stayed `Established` until +2315 ms (hold
  timer: 3 s after the last keepalive got through), then the s11 next hop was
  removed. Every packet hashed onto s11 was lost until then. Common default
  hold times are 90-180 s; BFD brings detection to around a second or less.
- **gray-switch**: the BGP session to s11 stayed `Established` and a1 kept
  **both** ECMP next hops for the whole fault. The capture of everything s11
  sent towards b1 during the fault
  ([`baseline/gray-switch/s11-to-b1-during-fault.txt`](evidence/multi-plane-multi-link/baseline/gray-switch/s11-to-b1-during-fault.txt))
  contains only BGP keepalives, no data:

  ```text
  11:55:08.068073 IP6 fd00:10:7::2.43938 > fd00:10:7::1.179: Flags [P.], ... length 19: BGP
  11:55:08.073523 IP6 fd00:10:7::1.179 > fd00:10:7::2.43938: Flags [P.], ... length 19: BGP
  11:55:08.073535 IP6 fd00:10:7::2.43938 > fd00:10:7::1.179: Flags [.], ack 20, ... length 0
  ```

  This is the "control plane and links are up, but the switch stops
  forwarding" case the paper calls "particularly pernicious". Routing cannot
  fix what it cannot see; an endpoint that watches its own packets can.
- **nic-plane**: plane 1 withdrew h2's prefix (a1's route vanished at +419 ms),
  but h1 still had **4** ECMP next hops (all planes) and kept hashing the flow
  into plane 1. Only the endpoint knows which plane a flow uses and can be told
  that the remote port is down.

**When is MRC better than BGP?** In this lab, for every fault the data plane
can see but the control plane cannot (silent links, gray switches, a remote
NIC port), the MRC endpoint routes around it in tens of milliseconds while the
BGP fabric loses traffic for seconds or until repair. For a clean local link
failure both work. The paper's further arguments (huge ECMP sets at 100K-GPU
scale, routing reconvergence disturbing load balancing, operational
simplicity) do not show up at this scale.

## 8b. More loss scenarios from the paper

All numbers below are in the generated
[evidence README](evidence/multi-plane-multi-link/README.md).

- **`partial-loss` (paper 5.2.4)**: one link, a1 <-> s11, drops 20% of packets
  in both directions. BGP sees nothing wrong (some keepalives still get
  through), so the single-path flow runs at about a quarter of its rate for the
  whole fault. The MRC sender retires the two EVs on that link after the first
  gaps, a few milliseconds in, and the probes later succeed (20% loss still lets
  some probes through) so they return as backups; goodput stays at 2,000 pkt/s.
- **`ecmp-collision` (paper section 1)**: four flows, each offered 700 pkt/s,
  cross 10 Mbit/s links. Baseline flows get random source ports, so two of them
  often hash onto the same uplink and drop tail loss hits them while another
  uplink sits idle. Three seeds show one lucky hash (everything fits) and two
  collisions. MRC sprays every flow over all four uplinks: the same offered load
  fits every time. The per-uplink packet counters are in each trial's
  `link_tx_packets`; `docs/img/ecmp-collision.svg` plots them.
- **`random-loss` (paper 5.2.7)**: uniform loss of 0.1% and 1% on every
  leaf-to-spine link. At this small scale the go-back-N baseline does **not**
  collapse; the visible effect is retransmission amplification (4.6% of packets
  resent at 1% loss, against 1.0% for MRC's selective retransmission). The
  paper's throughput collapse needs much longer windows and higher rates than a
  Python endpoint can generate.

## 9. ECN as a load-balancing signal

> Paper  - 2.1: "ECN now acts as a load-balancing signal. The receiver echoes the
> ECN signal back to the sender, indicating that this specific path is more
> congested than others, and the sender temporarily avoids it."  - 5.2: the
> two-flow experiment in which a competing flow forces one flow off a path.

`ecn` shapes both a1 uplinks to 20 Mbit/s with RED ECN marking (queues on
the T0 uplinks only; the paper disables ECN on the last hop). Flow A (2,400
pkt/s of 1 KB, EVs via s11 and s12) runs for 6 s; flow B (1,400 pkt/s, pinned
to the s11 path) joins after 2 s for 2.5 s. Switches mark the **outer**
header CE, visible in the capture at s11
([`mrc/ecn-ecn-avoid/s11-ingress.txt`](evidence/multi-plane-multi-link/mrc/ecn-ecn-avoid/s11-ingress.txt)):

```text
IP6 (class 0x03, ... next-header IPv6 (41) ...) fd00:10:1::1 > fcbb:0:5110:b101:e211:::
  IP6 (class 0x02, ... next-header UDP (17) ...) fc00:100::1.273 > fc00:200::...
```

(`class 0x03` = CE on the outer header; inner header still ECT(0).) The
capture is a short sample (20 packets) taken at s11 while both flows are
active, and it looks the same with and without avoidance: all packets are CE
and use the s11 path, `qp=1` is flow A and `qp=2` is flow B. It proves the
marking only; the effect of avoidance is in the table below. Decode it with
`python3 pcap_decode.py <pcap>` (see the pcap section at the end). The
responder echoes CE in its SACK for that EV; the sender skips that EV for 4 ms.

| Variant | Flow A share via s11: before / with B / after | a1->s11 queue | Flow B goodput (offered 1,400) | False loss declarations A / B |
|---|---|---|---|---|
| ECN avoidance on | 50% / 35% / 50% | 805 marked, **0 dropped** | **1,400 pkt/s** | 0 / 0 |
| ECN avoidance off | 50% / 36% / 50% | 3,355 marked, 0 dropped | **1,240 pkt/s** | 28 / 312 |

With avoidance, flow A moves part of its load to s12 while B is present and
moves back when B stops; neither flow loses a packet. Without it, the s11
queue keeps growing: the switch drops **nothing**, but queueing delay exceeds
the endpoint's fixed 20 ms timeout, so the endpoint declares 312 of B's
packets lost although every one arrived, retires B's only path (and keeps
retrying it), and B's goodput falls to 1,240 pkt/s. (Flow A's share still moves, because its s11 EV
is retired by the same false alarms.) That is the failure mode the paper avoids with two
mechanisms this lab does **not** have: NSCC congestion control (keeps queues
short) and packet trimming (turns a congestion drop into a NACK, so it is not
mistaken for a path failure).

## 10. Clustermapper: source-routed self probes

> Paper  - 3: "A Clustermapper agent on each node probes each of the ... directly
> connected T0s ... by sending probes source-routed to the T0 and back to the
> same agent ... if T0 probes show no problem but T1 probes do, we know the
> issue is the T0-T1 link."

`python3 /opt/mrc/transport.py clustermap --host h1` sends probes whose outer
destination loops back to h1, for example `fcbb:0:a101:e111::` (to a1 and
back) and `fcbb:0:a101:5110:a101:e111::` (to a1, s11, a1, back). These are
ordinary data-plane packets for the switches, so they can run at high rate.
With the a1-s11 link silently dropping traffic
([`mrc/clustermap/summary.json`](evidence/multi-plane-multi-link/mrc/clustermap/summary.json)):

| Target | Healthy | a1-s11 silently failed |
|---|---|---|
| T0 a1 via port 1 | 20/20 | 20/20 |
| T1 s11 via port 1 | 20/20 | **0/20** |
| T1 s12 via port 1 | 20/20 | 20/20 |
| T0 a1 via port 2 | 20/20 | 20/20 |
| T1 s11 via port 2 | 20/20 | **0/20** |
| all plane-2 targets | 20/20 | 20/20 |

Diagnosis: `T0-T1 link a1-s11 (or spine s11) is bad`. With one host the lab
cannot tell the link from the spine; probing s11 from b1's side would.

## 11. What is not modelled

| MRC / paper element | In this lab |
|---|---|
| RDMA verbs, QPs, memory registration, write placement | No. A "QP" is a sender/receiver pair of Python state machines; payloads are checked, not placed. |
| MRC BTH/RETH wire format | No. Lab header (opcode, QP, PSN, EV, timestamp); only the SACK body follows the spec. |
| Packet trimming and trim NACKs | No. Linux forwarding cannot trim. Section 9 shows why it matters. |
| NSCC congestion control | No. A fixed in-flight window (`--inflight`) and fixed pacing rate. |
| Microsecond reaction times | No. Python timers: 20 ms RTO, 20 ms probe interval, 2-10 ms loops. Ratios, not absolute times, carry over. |
| EV sets of 128-256 per QP; 8 planes | 8 paths total (2 planes x 2 ports x 2 spines). |
| uSID per T0 uplink number / template per destination row | Simplified: uSIDs encode plane, port and spine for a fixed two-host topology. |
| Denylist, Clustermapper at scale | A single-host Clustermapper demo only. |
| Inter-cluster forwarding, bidirectional QPs | No. |
| Reverse path EV probes | Yes, simplified (lab opcodes). |
| Port-state bitmap | Yes, as a lab extension byte after the SACK body. |

## 12. Fairness decisions and lab artefacts

These choices keep the baseline comparison fair and are worth knowing when you
modify the lab:

- **ECMP hash polarization.** Before Linux 6.11, every network namespace uses
  the same multipath hash seed, so consecutive tiers compute identical hashes.
  Unchanged, a1 sent every flow from a given h1 port to the same spine, and the
  baseline used half of its spines. Real switches use per-device seeds; the lab
  emulates that by giving leaves different hash field sets
  (`fib_multipath_hash_policy=3`). Hosts and leaves hash the UDP source port,
  so the EV still picks the path.
- **DAD disabled.** Linux keeps a re-enabled interface's addresses
  `tentative` for about 1.5 s (Duplicate Address Detection) and cannot resolve
  next hops meanwhile. Switch ASICs do not. With DAD on, both fabrics lost
  traffic for ~2 s after a link came back, which says nothing about BGP or MRC.
- **NIC failures are loss of carrier.** The `nic-plane` fault takes the
  *leaf* side down; the host sees `lowerlayerdown` and keeps its configuration,
  as a NIC would. Hosts ignore next hops without carrier
  (`ignore_routes_with_linkdown`).
- **BGP timers are 1 s / 3 s**, much faster than common defaults (FRR's
  datacenter profile uses 3/9 s, many vendors 30/90 or 60/180 s).
  The `silent-link-bfd` and `gray-switch-bfd` experiments measure BFD
  (100 ms x 3 on every eBGP session): `silent-link` drops from 2.27 s to about
  0.3 s, while `gray-switch` is unchanged because the broken spine still answers
  BFD. `nic-plane` is also unaffected: no BFD session sits on h2's NIC ports.
- **The baseline flow is pinned onto the failure.** `ip route get` predicts
  which UDP source ports hash onto a1 -> s11, and a 200-packet burst counted on
  s11's interface confirms it before the run (predictions alone proved
  unreliable after BGP rebuilt a next-hop group). A random flow would cross the
  failed element with probability 1/2 in this topology.
- **Fault timestamps** are taken inside the container right after the fault
  command, not on the host, so `docker exec` latency (~100 ms) is excluded.
  Containers share the host clock with the endpoints. Faults on two nodes (both
  directions of `silent-link`) are injected concurrently.

## 13. Reproduce everything

On Linux 6.1 or later, with Docker and containerlab:

```sh
make test                  # 40+ unit tests, no lab needed
make preflight
make up                    # deploy both fabrics
make evidence              # run everything, write docs/evidence, regenerate the SVGs
make down
```

(Add `SUDO=sudo` if containerlab needs root on your host.) The equivalent
`lab.py` commands:

```sh
python3 lab.py preflight --fabric mrc

for fabric in baseline mrc; do
    python3 lab.py deploy   --fabric "$fabric"   # multi-plane-multi-link
    python3 lab.py test     --fabric "$fabric"
    python3 lab.py evidence --fabric "$fabric"   # writes docs/evidence/...
    python3 lab.py destroy  --fabric "$fabric"
done
python3 lab.py report                      # regenerates the evidence README
python3 viz.py                             # regenerates docs/img/*.svg
```

Single experiments: `make experiment NAME=<name> FABRIC=<fabric>` (or
`python3 lab.py experiment <name> --fabric <fabric>`) with `spray`, `link-down`,
`silent-link`, `partial-loss`, `gray-switch`, `nic-plane`, `ecmp-collision`,
`random-loss`, `ecn`, `clustermap` (`spray`, `ecn` and `clustermap` need
`--fabric mrc`) and the baseline-only `silent-link-bfd` and `gray-switch-bfd`.
Results go to `topologies/<lab>/results/experiments/<name>/`; the curated copy
is in `docs/evidence/multi-plane-multi-link/{baseline,mrc}/<name>/`.

### Reading the pcaps

Each `*.pcap` has a matching `*.txt` (the `tcpdump -v` decode). Captures are
truncated to 200 bytes per packet. The traffic is UDP, so there is no stream to
follow.

| File (under `docs/evidence/multi-plane-multi-link/`) | What it shows |
|---|---|
| `mrc/spray/1-h1-port1.pcap` | Packet leaving the sender: outer destination is the uSID path, inner is the NIC address |
| `mrc/spray/2-s11-ingress.pcap` | Same packet at a spine, outer destination shifted once |
| `mrc/spray/3-b1-to-h2.pcap`, `4-h2-ingress.pcap` | Last hop and the receiver |
| `mrc/gray-switch/s11-to-b1-during-fault.pcap` | Spine output during the fault, compare with `baseline/gray-switch/` |
| `mrc/silent-link/h1-port1-around-fault.pcap` | One NIC port from 0.5 s before to 2.5 s after the fault: traffic moves between EVs, resent PSNs, probes |
| `mrc/ecn-*/s11-ingress.pcap` | ECN marking with and without ECN avoidance |

To decode the lab header (path, EV, PSN, SACK bitmap, ECN) without Wireshark:

```sh
pip install scapy        # only this optional tool needs it
python3 pcap_decode.py docs/evidence/multi-plane-multi-link/mrc/spray/1-h1-port1.pcap --limit 5
python3 pcap_decode.py FILE --only sack      # data, sack, probe, ev_probe, ...
python3 pcap_decode.py FILE --summary        # packet counts per type and per EV
```

The failure capture is the clearest packet-level view of MRC at work. Output of
`python3 pcap_decode.py docs/evidence/multi-plane-multi-link/mrc/silent-link/h1-port1-around-fault.pcap --timeline`
(abridged; the fault hits at 0.5 s):

```text
  0.4s  ev=0x10111:50
  0.5s  ev=0x10111:23  ev=0x10112:27  probes:3  resent:2
  0.6s  ev=0x10112:50  probes:4
```

EV `0x10111` is plane 1, link 1, spine 1 (the path through the failing a1-s11
link). Within one 100 ms bucket the port stops using it and sends on
`0x10112` (spine 2) instead, the packets lost on the dead path are sent again
(`resent`), and probes continue. The
delivery rate never drops.

An empty result (for example the gray-switch capture on the baseline) means no
lab packets crossed that interface.

Useful Wireshark filters:

- `ipv6.nxt == 41`: outer IPv6 headers (IPv6-in-IPv6); the outer `ipv6.dst` is the path.
- `udp.dstport == 4791`: data packets. Wireshark labels them RRoCE and flags them
  malformed, because the lab uses its own header, not a real RoCE one.
- `udp.srcport == <n>`: one EV. The EV is the UDP source port, so different
  ports are different paths.
- SACKs go back to the EV port, not 4791, so they show as plain UDP. Use
  `udp && !(udp.dstport == 4791)` to see them.
- `ipv6.tclass` (low two bits = 3 means CE) in the `ecn-*` captures.

[paper]: https://cdn.openai.com/pdf/resilient-ai-supercomputer-networking-using-mrc-and-srv6.pdf
