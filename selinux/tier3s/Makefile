# Build qdistro_tier3s.pp.
#
# Usage:
#   make            — compile qdistro_tier3s.pp (packaging the .fc)
#   make check      — compile-only smoke test (no .pp kept); the
#                     reproducible build contract used in CI / by a
#                     fresh clone.
#   make install    — semodule -i qdistro_tier3s.pp
#   make remove     — semodule -r qdistro_tier3s
#   make clean      — drop build artifacts
#
# Same build path as tier2: kernel policy language via the base
# checkmodule + semodule_package toolchain — no selinux-policy-devel.
# Unlike tier2, the .fc is LOAD-BEARING: the exec transition keys on
# qdistro_tier3s_exec_t, which only exists if the file contexts are
# packaged into the .pp (semodule_package -f) and applied with
# restorecon (install-policy.sh does both).
#
# Requires: checkpolicy (checkmodule) + policycoreutils
# (semodule_package, semodule, restorecon).

MODULE := qdistro_tier3s

.PHONY: all check install remove clean

all: $(MODULE).pp

$(MODULE).pp: $(MODULE).te $(MODULE).fc
	checkmodule -M -m -o $(MODULE).mod $(MODULE).te
	semodule_package -o $(MODULE).pp -m $(MODULE).mod -f $(MODULE).fc
	@echo "[tier3s] built $(MODULE).pp"

# Reproducible compile contract: build the real artifacts then discard
# them, so `make check` is a pure pass/fail with no leftover files.
check: $(MODULE).te $(MODULE).fc
	@checkmodule -M -m -o $(MODULE).mod $(MODULE).te \
	  && semodule_package -o $(MODULE).pp -m $(MODULE).mod -f $(MODULE).fc \
	  && rm -f $(MODULE).mod $(MODULE).pp \
	  && echo "[tier3s] check OK — $(MODULE).te compiles" \
	  || { rm -f $(MODULE).mod $(MODULE).pp; echo "[tier3s] check FAILED" >&2; exit 1; }

install: $(MODULE).pp
	semodule -i $(MODULE).pp
	@echo "[tier3s] policy module loaded"
	@semodule -l | grep -q "^$(MODULE)" && echo "[tier3s] active" || echo "[tier3s] WARN: not listed"

remove:
	semodule -r $(MODULE) 2>/dev/null || true
	@echo "[tier3s] policy module removed"

clean:
	rm -f $(MODULE).pp $(MODULE).pp.bz2 $(MODULE).mod $(MODULE).mod.fc
