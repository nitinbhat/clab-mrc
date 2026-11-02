# Contributing to clab-mrc

Thank you for your interest in contributing!

## Quick start

```sh
git clone <repo-url> && cd clab-mrc
python3 -m unittest discover -q   # should pass in <1s
```

## Before submitting a PR

1. Run `make test` — all 43+ unit tests must pass.
2. If you change the transport or reliability code, add or update tests.
3. If you change the lab topology or experiments, verify with `make up && make demo && make down` on a Linux host with kernel 6.1+ and containerlab.
4. Keep commits focused; one logical change per commit.

## Reporting issues

Please include:
- What you tried (command or code)
- What you expected
- What happened instead
- Your environment: OS, kernel version (`uname -r`), Python version, containerlab version

## Code style

- No external dependencies in the core (`lab.py`, `transport.py`, `mrc_paths.py`, `mrc_reliability.py`). The lab runs with only Python 3.9+ stdlib.
- `pcap_decode.py` and `viz.py` are optional tools and may use external packages (note them in the README).
- Keep lines under 100 characters where practical.

## License

By contributing, you agree that your contributions will be licensed under the MIT License.
