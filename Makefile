# Simple front end for lab.py.  Run `make` to see the targets.
# SUDO is empty by default (works if your user is in the docker/clab groups).
# If containerlab needs root:  make up SUDO=sudo
SUDO   ?=
FABRIC ?= mrc
NAME   ?= silent-link
LAB    := $(SUDO) python3 src/lab.py

.DEFAULT_GOAL := help
.PHONY: help test preflight up up-baseline up-mrc down down-baseline down-mrc experiment evidence report viz demo

help:
	@echo "make test                  unit tests (no containers needed)"
	@echo "make preflight             check the host can run the lab"
	@echo "make up                    deploy BOTH fabrics (baseline + mrc)"
	@echo "make experiment NAME=silent-link FABRIC=mrc"
	@echo "                           NAME: link-down silent-link partial-loss gray-switch nic-plane"
	@echo "                                 ecmp-collision random-loss silent-link-bfd gray-switch-bfd all"
	@echo "make demo                  run silent-link on both fabrics and print the comparison"
	@echo "make viz                   regenerate the animated SVGs in docs/img/"
	@echo "make evidence              re-run everything and store results in docs/evidence"
	@echo "make report                rebuild docs/evidence/*/README.md from stored data"
	@echo "make down                  destroy both fabrics"

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
