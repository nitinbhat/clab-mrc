# multi-plane-single-link

2 plane(s), 1 NIC port(s) per plane, 10 containers, 12 links, 4 distinct h1 -> h2 paths.

Each plane has leaf `aN` (h1 side), leaf `bN` (h2 side), and spines `sN1`/`sN2`. Hosts are shared across planes; switches are not.

```mermaid
flowchart LR
  h1 ---|"eth1 - eth1"| a1
  h2 ---|"eth1 - eth1"| b1
  a1 ---|"eth2 - eth1"| s11
  a1 ---|"eth3 - eth1"| s12
  b1 ---|"eth2 - eth2"| s11
  b1 ---|"eth3 - eth2"| s12
  h1 ---|"eth2 - eth1"| a2
  h2 ---|"eth2 - eth1"| b2
  a2 ---|"eth2 - eth1"| s21
  a2 ---|"eth3 - eth1"| s22
  b2 ---|"eth2 - eth2"| s21
  b2 ---|"eth3 - eth2"| s22
```

Fabric `baseline`: FRR eBGP (timers 1/3) with ECMP over a 5-tuple hash. Hosts own `fc00:100::1` / `fc00:200::1` and use a static ECMP route across their NIC ports. Packets are plain IPv6/UDP; the EV is the UDP source port, and the path is whatever the hashes pick.

From the project root:

```sh
python3 lab.py deploy --profile multi-plane-single-link
python3 lab.py test --profile multi-plane-single-link
python3 lab.py experiment link-down --profile multi-plane-single-link
python3 lab.py destroy --profile multi-plane-single-link
```

`mrc.clab.yml` is JSON (valid YAML). A bare `containerlab deploy` must be followed by `python3 lab.py configure --profile multi-plane-single-link`. Results are written under `results/` (not version controlled).
