# Build qdistro_qsu.pp via the system selinux-policy devel Makefile.
#
# Usage:
#   make            — compile qdistro_qsu.pp
#   make install    — semodule -i qdistro_qsu.pp
#   make remove     — semodule -r qdistro_qsu
#   make clean      — drop build artifacts
#
# Requires: selinux-policy-devel installed
# (zypper install selinux-policy-devel on Tumbleweed,
#  dnf install selinux-policy-devel on Fedora).
#
# Dependency: qdistro_qsu.te calls qdistro_broker_dbus_chat() and
# gen_requires qdistro_broker_t, so the qdistro_broker module must be in
# the active policy store before this module loads (install-policy.sh
# handles that ordering).

MODULE := qdistro_qsu
DEVEL  := /usr/share/selinux/devel

.PHONY: all install remove clean

all: $(MODULE).pp

$(MODULE).pp: $(MODULE).te $(MODULE).if $(MODULE).fc
	$(MAKE) -f $(DEVEL)/Makefile $(MODULE).pp

install: $(MODULE).pp
	semodule -i $(MODULE).pp
	@echo "[qsu] policy module loaded"
	@semodule -l | grep -q "^$(MODULE)" && echo "[qsu] active" || echo "[qsu] WARN: not listed"

remove:
	semodule -r $(MODULE) 2>/dev/null || true
	@echo "[qsu] policy module removed"

clean:
	rm -f $(MODULE).pp $(MODULE).pp.bz2 tmp/$(MODULE).mod tmp/$(MODULE).mod.fc
	rm -rf tmp/
