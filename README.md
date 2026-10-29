# clab-mrc

MRC-inspired FRR/containerlab learning lab.

Four small, independently deployable topologies for studying packet spraying,
out-of-order reception, selective retransmission, and per-path health tracking.
**This is an educational userspace simulation, not an interoperable MRC or RDMA
implementation.** The fabric uses real FRR eBGP and Linux full-SID SRv6 forwarding.

There is also a separate [MRC reliability reference milestone](docs/reliability.md):
24-bit PSN state, cumulative/selective feedback, and binary SETH/CC_STATE codecs.
It is tested in memory and is **not yet connected to the containerlab transport**.

## How MRC works

[Multipath Reliable Connection (MRC) 1.0][spec] extends the Reliable Connection
transport for multipath, best-effort Ethernet fabrics. The endpoint, not FRR,
implements the transport:

1. A sender rotates through an entropy-value (EV) profile to spray packets over
   multiple paths. Path encoding can use ECMP entropy, Structured EV, or SRv6 uSID.
2. The receiver tracks packet sequence numbers and out-of-order reception.
   Real MRC supports direct memory placement for RDMA Write/Write-with-Immediate.
3. Reliability SACK/NACK feedback identifies received or missing packets, enabling
   selective retransmission without restarting the whole operation.
4. Per-EV health and congestion feedback influence future selection. The spec
   defines GOOD, DENIED, SKIP, and ASSUMED_BAD states, alongside probing and recovery.
5. NSCC congestion control regulates traffic; ECN and optional packet trimming
   provide fabric congestion signals.

The live lab models steps 1-4 partially using a logical session over JSON/UDP. It
implements a shuffled EV rotation, a bounded in-flight window, per-packet ACKs,
timeout-based selective retries, GOOD/ASSUMED_BAD states, per-EV RTT samples,
duplicate detection, and a contiguous receive watermark. It does **not** implement
MRC wire headers/SACK bitmaps, NIC memory placement, libibverbs/QPs, NSCC, ECN
feedback, trimming, DENIED/SKIP states, or same-session path recovery probes.
RTT includes the return path, which uses ordinary IPv6 routing and may be asymmetric.

## MRC-specific reference components

```sh
python3 -m unittest -v test_mrc_reliability
python3 mrc_reliability.py
```

The demo crosses the 24-bit sequence rollover, identifies a withheld packet using
binary selective feedback, and closes the reception hole after a manual repair.
The reference tests cover exact byte vectors, reordering, duplicates, malformed
feedback, receive-window limits, and lost ACKs. They do not prove MRC
interoperability. See [the coverage and interpretation notes](docs/reliability.md)
before using the codecs; the original JSON simulator remains unchanged.

## Topologies

| Directory under `topologies/` | Planes | Host links per plane | Containers | Physical links | Source EVs |
|---|---:|---:|---:|---:|---:|
| [single-plane-single-link](topologies/single-plane-single-link/) | 1 | 1 | 6 | 6 | 2 |
| [single-plane-multi-link](topologies/single-plane-multi-link/) | 1 | 2 | 6 | 8 | 4 |
| [multi-plane-single-link](topologies/multi-plane-single-link/) | 2 | 1 | 10 | 12 | 4 |
| [multi-plane-multi-link](topologies/multi-plane-multi-link/) | 2 | 2 | 10 | 16 | 8 |

These are the four approved study variants, **not exact replicas of the article's
drawings**. They reduce the workload to two hosts, use two spines per plane, and
define "multi-link" as parallel host-to-leaf links per plane. The article's drawings
also illustrate parallel leaf-to-spine links; these are not reproduced here.

Each plane is a separate fabric with source leaf `aN`, destination leaf `bN`,
and spines `sN1` and `sN2`. Every leaf connects to both spines. Hosts `h1` and `h2`
are shared between planes; routers and data links are not. Each topology directory
includes its exact port-level Mermaid diagram, containerlab definition, FRR
configs, and EV/path inventory.

An EV selects a source plane, a source-host interface, and a spine. The destination
host link is selected by ordinary routing, not encoded as another EV dimension.
The aliases `fc00:200:<EV>::2` all terminate on the same receiver/session; this is a
test harness convenience, not MRC addressing or a real QP.

FRR 10.5 advertises IPv6 locators and endpoint prefixes over eBGP. Multipath-relax
allows two equal-cost spine nexthops. `lab.py` installs SRv6 policies and endpoint
actions using iproute2; FRR does not implement MRC, and does not program these
SRv6 policies itself. SRv6 segments select nodes, not strict adjacencies:
the underlay may detour after BGP reconverges.

## SRv6 versus uSID

**Native uSID is not available on the tested host's Linux 5.10 kernel.** A
`next-csid` route installation returns success but the kernel silently omits the
flavor attributes. The lab records this in `results/capabilities.json` and uses
full 128-bit SIDs with an SRH instead. MRC's specified SRv6 mode uses uSID;
this fallback is therefore a source-routing learning substitute, not that mode.

The path is:

```text
h1: H.Encaps -> aN: End -> selected spine: End -> bN: End -> h2: End.DT6
```

Three details were necessary on the tested kernel:

- Endpoint actions use a dataplane device rather than `dev lo`; read-back verifies
  the kernel actually retained the actions.
- SID addresses are not assigned local interface addresses, which would bypass
  the main-table SRv6 endpoint routes.
- The receiver's End.DT6 looks up table 255 (local), where the destination aliases
  are local-delivery routes. Table 254 alone produced ICMP unreachable errors.

Each source-host link also has a distinct first SID and an explicit route to it.
Linux resolves this outer destination separately during encapsulation; a shared
ECMP default can otherwise send an encapsulated packet into the wrong plane.

`usid-check` is deliberately strict and exits nonzero here. Even on a kernel that
retains `next-csid` attributes, this project does not yet include a validated native
uSID datapath. A newer kernel alone is not evidence that the uSID PoC works.

## Run

Requirements: Linux with SRv6 support, Python 3.9+, Docker access, and containerlab
with permission to create privileged containers/veth links. No host Python packages
are required. The Dockerfile extends `quay.io/frrouting/frr:10.5.0` with tcpdump.
Image pulls/builds require network access. Tested versions: containerlab 0.73.0,
Docker 20.10.5, iproute2 6.15.0 inside the containers.

```sh
cd /home/dev/clab-mrc
python3 -m unittest -v

# Default profile: multi-plane-multi-link.
python3 lab.py deploy
python3 lab.py test
python3 lab.py destroy

# Choose another topology explicitly.
python3 lab.py deploy --profile single-plane-single-link
python3 lab.py test --profile single-plane-single-link
python3 lab.py destroy --profile single-plane-single-link
```

`deploy` generates files, builds the local image, runs containerlab, installs
the datapath, and checks end-to-end reachability for every EV. Use `generate` to
create files only. A bare `containerlab deploy -t topologies/<profile>/mrc.clab.yml`
must be followed by `python3 lab.py configure --profile <profile>`; otherwise
the Linux datapath is incomplete. `configure` can also reapply that datapath to an
existing lab. Container restarts do not automatically reapply it.

Do not run `deploy` over an existing instance: use `configure`, or destroy and
redeploy that exact profile. Destroying a lab preserves its source files and
results directory. A failed deployment/test stops with an error and leaves the
lab available for diagnosis.

Lab names and management networks are unique per profile, using
`172.31.250.0/24` through `172.31.253.0/24`. Check for conflicts on your host.
Data addresses repeat between the isolated labs. Containers share the host kernel:
this is not isolation suitable for untrusted workloads. The scripts change
network sysctls inside these containers only; containerlab also manages its own
Docker network, host entries, and SSH inventory in the usual way.

To validate all profiles sequentially, starting with no project labs deployed:

```sh
for profile in single-plane-single-link single-plane-multi-link \
               multi-plane-single-link multi-plane-multi-link; do
    python3 lab.py deploy --profile "$profile" || break
    python3 lab.py test --profile "$profile" || break
    if [ "$profile" != multi-plane-multi-link ]; then
        python3 lab.py destroy --profile "$profile" || break
    fi
done
```

This leaves only the largest lab running after success.

## Validation and results

Every integration test checks all expected eBGP sessions and two installed ECMP
nexthops per plane, then sends 400 baseline packets. Bounded captures prove SRHs
on every source-host interface and every spine. A 1,000-packet transfer runs while
`s11:eth2` is brought down, exercising timeout, EV exclusion, out-of-order receipt,
and selective retransmission. The link is restored in a `finally` block. A new
400-packet session verifies all EVs can be used after restoration.

Sender completion is cross-checked against the receiver's unique packet count and
contiguous watermark. Baseline/restored transfers must have no retries; every EV
must receive ACKs. Only then is a PASS summary written. This is a fabric-link
failure test, not a complete host-port/whole-plane failure matrix or a performance
benchmark. A restored session starts with fresh GOOD states; this does not prove
automatic recovery of ASSUMED_BAD EVs within an existing session.

Clean deployments tested on **2026-10-29**, Linux **5.10.0-22-amd64**:

| Profile | Baseline | During failure | Retransmissions | After restoration |
|---|---:|---:|---:|---:|
| single-plane-single-link | 400/400 | 1,000/1,000 | 23 | 400/400 |
| single-plane-multi-link | 400/400 | 1,000/1,000 | 29 | 400/400 |
| multi-plane-single-link | 400/400 | 1,000/1,000 | 10 | 400/400 |
| multi-plane-multi-link | 400/400 | 1,000/1,000 | 7 | 400/400 |

Retry counts depend on timing and are not performance comparisons. The original
lab has five unit tests; the separate reliability milestone adds 21 reference
tests. Each topology's ignored `results/` directory contains
`summary.json`, detailed sender/receiver JSON, control-plane evidence,
capability checks, and PCAPs with readable capture extracts.

For the currently deployed largest lab:

```sh
cat topologies/multi-plane-multi-link/results/summary.json
docker exec clab-mrc-multi-plane-multi-link-a1 \
    vtysh -c 'show bgp ipv6 unicast summary'
docker exec clab-mrc-multi-plane-multi-link-h1 ip -6 route show
docker exec clab-mrc-multi-plane-multi-link-s11 \
    tcpdump -nn -r /results/s11.pcap
```

## Publication boundaries

No license has been selected. Choose a license before publishing this PoC.
Runtime lab directories, captures/results, Python
cache files, and the supplied article screenshots are ignored by `.gitignore`.
The screenshots remain local reference material; do not redistribute them without
the necessary permission. The README diagrams are generated from this lab's own
link inventory. Review any files you add before publication; this is not a
production transport or a conformance/security certification.

## References

- [OCP MRC 1.0 specification][spec], especially sections 5, 7, 9.2-9.3, and 11.3-11.5.
- [Devang Patel's containerlab article][article] (the supplied excerpt and drawings
  motivated the study).
- [Containerlab](https://containerlab.dev/).
- [FRR documentation](https://docs.frrouting.org/en/stable-10.5/).

[spec]: https://www.opencompute.org/documents/ocp-mrc-1-0-pdf/
[article]: https://www.linkedin.com/pulse/container-lab-based-topologies-learning-mrc-devang-patel-fpirc/
