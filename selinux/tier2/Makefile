# Build qdistro_tier2.pp.
#
# Usage:
#   make            — compile qdistro_tier2.pp
#   make check      — compile-only smoke test (no .pp kept); the
#                     reproducible build contract used in CI / by a
#                     fresh clone.
#   make install    — semodule -i qdistro_tier2.pp
#   make remove     — semodule -r qdistro_tier2
#   make clean      — drop build artifacts
#
# Build path (differs from tier1 on purpose):
#   tier1 is refpolicy m4 and builds through the selinux-policy-devel
#   Makefile. tier2 is kernel policy language and builds with the base
#   checkmodule + semodule_package toolchain, which the qdistro host
#   ships WITHOUT selinux-policy-devel (the host only carries
#   container-selinux's container.if, not the full refpolicy m4
#   headers). Requires: checkpolicy (checkmodule) + policycoreutils
#   (semodule_package, semodule).
#
# If a host DOES have /usr/share/selinux/devel/Makefile, you can still
# build via it — the .te is valid kernel language and the devel
# Makefile's checkmodule invocation accepts it — but the rules below do
# not depend on it.

MODULE := qdistro_tier2
SHIP   := /etc/selinux/$$(awk -F= '/^SELINUXTYPE=/ {print $$2}' /etc/selinux/config 2>/dev/null)/modules/active/modules

.PHONY: all check install remove clean

all: $(MODULE).pp

$(MODULE).pp: $(MODULE).te
	checkmodule -M -m -o $(MODULE).mod $(MODULE).te
	semodule_package -o $(MODULE).pp -m $(MODULE).mod
	@echo "[tier2] built $(MODULE).pp"

# Reproducible compile contract: build the real artifacts then discard
# them, so `make check` is a pure pass/fail with no leftover files.
# checkmodule insists the module name match the output base filename, so
# we build in place under the canonical name and clean up after.
check: $(MODULE).te
	@checkmodule -M -m -o $(MODULE).mod $(MODULE).te \
	  && semodule_package -o $(MODULE).pp -m $(MODULE).mod \
	  && rm -f $(MODULE).mod $(MODULE).pp \
	  && echo "[tier2] check OK — $(MODULE).te compiles" \
	  || { rm -f $(MODULE).mod $(MODULE).pp; echo "[tier2] check FAILED" >&2; exit 1; }

install: $(MODULE).pp
	semodule -i $(MODULE).pp
	@echo "[tier2] policy module loaded"
	@semodule -l | grep -q "^$(MODULE)" && echo "[tier2] active" || echo "[tier2] WARN: not listed"

remove:
	semodule -r $(MODULE) 2>/dev/null || true
	@echo "[tier2] policy module removed"

clean:
	rm -f $(MODULE).pp $(MODULE).pp.bz2 $(MODULE).mod $(MODULE).mod.fc
