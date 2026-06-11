# Build qdistro_session_manager.pp via the system selinux-policy devel Makefile.
#
# Usage:
#   make            — compile qdistro_session_manager.pp
#   make install    — semodule -i qdistro_session_manager.pp
#   make remove     — semodule -r qdistro_session_manager
#   make clean      — drop build artifacts
#
# Requires: selinux-policy-devel installed.

MODULE := qdistro_session_manager
DEVEL  := /usr/share/selinux/devel

.PHONY: all install remove clean

all: $(MODULE).pp

$(MODULE).pp: $(MODULE).te $(MODULE).if $(MODULE).fc
	$(MAKE) -f $(DEVEL)/Makefile $(MODULE).pp

install: $(MODULE).pp
	semodule -i $(MODULE).pp
	@echo "[sessmgr-policy] policy module loaded"
	@semodule -l | grep -q "^$(MODULE)" && echo "[sessmgr-policy] active" || echo "[sessmgr-policy] WARN: not listed"

remove:
	semodule -r $(MODULE) 2>/dev/null || true
	@echo "[sessmgr-policy] policy module removed"

clean:
	rm -f $(MODULE).pp $(MODULE).pp.bz2 tmp/$(MODULE).mod tmp/$(MODULE).mod.fc
	rm -rf tmp/
