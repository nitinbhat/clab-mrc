# Simple front end for lab.py.  Run `make` to see the targets.
# SUDO is empty by default (works if your user is in the docker/clab groups).
# If containerlab needs root:  make up SUDO=sudo
SUDO   ?=
FABRIC ?= mrc
NAME   ?= silent-link
LAB    := $(SUDO) python3 src/lab.py

.DEFAULT_GOAL := help
.PHONY: help test preflight up up-baseline up-mrc down down-baseline down-mrc \
        experiment spray link-down silent-link partial-loss gray-switch nic-plane \
        ecmp-collision random-loss evidence report viz demo

help:
	@echo "make test                  unit tests (no containers needed)"
	@echo "make preflight             check the host can run the lab"
	@echo ""
	@echo "== Deployment =="
	@echo "make up                    deploy BOTH fabrics (baseline + mrc)"
	@echo "make up-baseline           deploy baseline fabric only"
	@echo "make up-mrc                deploy mrc fabric only"
	@echo "make down                  destroy both fabrics"
	@echo ""
	@echo "== Quick Experiments (auto-selects fabric) =="
	@echo "make spray                 multi-hop packet capture (mrc only)"
	@echo "make link-down             T0-T1 link admin down (both fabrics)"
	@echo "make silent-link           silent link failure (both fabrics)"
	@echo "make partial-loss          20% loss on one link (both fabrics)"
	@echo "make gray-switch           spine drops traffic (both fabrics)"
	@echo "make nic-plane             receiver loses plane-1 ports (both fabrics)"
	@echo "make ecmp-collision        4 flows on limited links (both fabrics)"
	@echo "make random-loss           0.1% / 1% loss on all links (both fabrics)"
	@echo ""
	@echo "== Advanced =="
	@echo "make experiment NAME=<name> FABRIC=<baseline|mrc>"
	@echo "make demo                  run silent-link on both fabrics + generate SVG"
	@echo "make viz                   regenerate the animated SVGs in docs/img/"
	@echo "make evidence              re-run everything and store in docs/evidence"
	@echo "make report                rebuild docs/evidence/*/README.md from stored data"

test:
	python3 -m unittest discover -s tests -q

preflight:
	$(LAB) preflight --fabric baseline
	$(LAB) preflight --fabric mrc

up: up-baseline up-mrc
up-baseline:
	$(LAB) deploy --fabric baseline
up-mrc:
	$(LAB) deploy --fabric mrc

down: down-baseline down-mrc
down-baseline:
	$(LAB) destroy --fabric baseline
down-mrc:
	$(LAB) destroy --fabric mrc

# The -bfd experiments only exist on the baseline fabric.
experiment:
	$(LAB) experiment $(NAME) --fabric $(if $(findstring -bfd,$(NAME)),baseline,$(FABRIC))

# Individual experiment targets for easy discovery
spray:
	$(LAB) experiment spray --fabric mrc

link-down:
	$(LAB) experiment link-down --fabric baseline
	$(LAB) experiment link-down --fabric mrc

silent-link:
	$(LAB) experiment silent-link --fabric baseline
	$(LAB) experiment silent-link --fabric mrc

partial-loss:
	$(LAB) experiment partial-loss --fabric baseline
	$(LAB) experiment partial-loss --fabric mrc

gray-switch:
	$(LAB) experiment gray-switch --fabric baseline
	$(LAB) experiment gray-switch --fabric mrc

nic-plane:
	$(LAB) experiment nic-plane --fabric mrc

ecmp-collision:
	$(LAB) experiment ecmp-collision --fabric baseline
	$(LAB) experiment ecmp-collision --fabric mrc

random-loss:
	$(LAB) experiment random-loss --fabric baseline
	$(LAB) experiment random-loss --fabric mrc

demo:
	$(LAB) experiment silent-link --fabric baseline
	$(LAB) experiment silent-link --fabric mrc
	python3 src/viz.py
	@echo "Open docs/img/silent-link.svg in a browser."

evidence:
	$(LAB) evidence all --fabric baseline
	$(LAB) evidence all --fabric mrc
	python3 src/lab.py report
	python3 src/viz.py

report:
	python3 src/lab.py report

viz:
	python3 src/viz.py
