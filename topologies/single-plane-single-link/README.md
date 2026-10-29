# single-plane-single-link

1 plane(s), 1 physical host link(s) per plane, 6 containers, 6 links, 2 source EVs.

Each plane has source leaf `aN`, destination leaf `bN`, and two spines `sN1`/`sN2`. Every leaf connects to both spines. The two hosts are shared across planes; the routers are not.

```mermaid
flowchart LR
  h1 ---|"eth1 - eth1"| a1
  h2 ---|"eth1 - eth1"| b1
  a1 ---|"eth2 - eth1"| s11
  a1 ---|"eth3 - eth1"| s12
  b1 ---|"eth2 - eth2"| s11
  b1 ---|"eth3 - eth2"| s12
```

From the project root:

```sh
python3 lab.py deploy --profile single-plane-single-link
python3 lab.py test --profile single-plane-single-link
python3 lab.py destroy --profile single-plane-single-link
```

`mrc.clab.yml` is JSON (valid YAML) with relative bind mounts. Deployment through `lab.py` also installs the Linux SRv6 datapath; a bare `containerlab deploy` must be followed by `python3 lab.py configure --profile single-plane-single-link`.

See the root README for protocol limitations and how these study variants differ from the article's drawings. Results and captures are written under `results/` and are excluded from version control.
