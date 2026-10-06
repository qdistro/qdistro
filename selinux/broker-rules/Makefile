# Build qdistro_broker_rules.pp.
#
# Usage:
#   make            — compile qdistro_broker_rules.pp (packaging the .fc)
#   make check      — compile-only smoke test (no .pp kept); the
#                     reproducible build contract used in CI / by a
#                     fresh clone.
#   make install    — semodule -i qdistro_broker_rules.pp
#   make remove     — semodule -r qdistro_broker_rules
#   make clean      — drop build artifacts
#
# Kernel policy language via the base checkmodule + semodule_package
# toolchain — no selinux-policy-devel — because the runtime-only VM
# bases carry checkpolicy but not the refpolicy include tree, and this
# module must be installable on them.
#
# Requires: checkpolicy (checkmodule) + policycoreutils
# (semodule_package, semodule, restorecon).

MODULE := qdistro_broker_rules

.PHONY: all check install remove clean

all: $(MODULE).pp

$(MODULE).pp: $(MODULE).te $(MODULE).fc
	checkmodule -M -m -o $(MODULE).mod $(MODULE).te
	semodule_package -o $(MODULE).pp -m $(MODULE).mod -f $(MODULE).fc
	@echo "[broker-rules] built $(MODULE).pp"

# Reproducible compile contract: build the real artifacts then discard
# them, so `make check` is a pure pass/fail with no leftover files.
check: $(MODULE).te $(MODULE).fc
	@checkmodule -M -m -o $(MODULE).mod $(MODULE).te \
	  && semodule_package -o $(MODULE).pp -m $(MODULE).mod -f $(MODULE).fc \
	  && rm -f $(MODULE).mod $(MODULE).pp \
	  && echo "[broker-rules] check OK — $(MODULE).te compiles" \
	  || { rm -f $(MODULE).mod $(MODULE).pp; echo "[broker-rules] check FAILED" >&2; exit 1; }

install: $(MODULE).pp
	semodule -i $(MODULE).pp
	@echo "[broker-rules] policy module loaded"
	@semodule -l | grep -q "^$(MODULE)" && echo "[broker-rules] active" || echo "[broker-rules] WARN: not listed"

remove:
	semodule -r $(MODULE) 2>/dev/null || true
	@echo "[broker-rules] policy module removed"

clean:
	rm -f $(MODULE).pp $(MODULE).pp.bz2 $(MODULE).mod $(MODULE).mod.fc
