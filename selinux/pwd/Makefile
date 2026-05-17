# Build qdistro_pwd.pp via the system selinux-policy devel Makefile.
#
# Usage:
#   make            — compile qdistro_pwd.pp
#   make install    — semodule -i qdistro_pwd.pp
#   make remove     — semodule -r qdistro_pwd
#   make clean      — drop build artifacts
#
# Requires: selinux-policy-devel installed.

MODULE := qdistro_pwd
DEVEL  := /usr/share/selinux/devel

.PHONY: all install remove clean

all: $(MODULE).pp

$(MODULE).pp: $(MODULE).te $(MODULE).if $(MODULE).fc
	$(MAKE) -f $(DEVEL)/Makefile $(MODULE).pp

install: $(MODULE).pp
	semodule -i $(MODULE).pp
	@echo "[pwd-policy] policy module loaded"
	@semodule -l | grep -q "^$(MODULE)" && echo "[pwd-policy] active" || echo "[pwd-policy] WARN: not listed"

remove:
	semodule -r $(MODULE) 2>/dev/null || true
	@echo "[pwd-policy] policy module removed"

clean:
	rm -f $(MODULE).pp $(MODULE).pp.bz2 tmp/$(MODULE).mod tmp/$(MODULE).mod.fc
	rm -rf tmp/
