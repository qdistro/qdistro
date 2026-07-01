# Build qdistro_tier1.pp via the system selinux-policy devel Makefile.
#
# Usage:
#   make            — compile qdistro_tier1.pp
#   make install    — semodule -i qdistro_tier1.pp
#   make remove     — semodule -r qdistro_tier1
#   make clean      — drop build artifacts
#
# Requires: selinux-policy-devel installed
# (zypper install selinux-policy-devel on Tumbleweed,
#  dnf install selinux-policy-devel on Fedora).

MODULE := qdistro_tier1
DEVEL  := /usr/share/selinux/devel
LOCAL_DEVEL_ROOT := $(CURDIR)/tmp/selinux-devel
LOCAL_INCLUDE := $(LOCAL_DEVEL_ROOT)/devel/include
SHIP   := /etc/selinux/$$(awk -F= '/^SELINUXTYPE=/ {print $$2}' /etc/selinux/config)/modules/active/modules

.PHONY: all install remove clean

all: $(MODULE).pp

$(LOCAL_INCLUDE)/contrib/qdistro_broker.if: ../broker/qdistro_broker.if
	rm -rf "$(LOCAL_DEVEL_ROOT)"
	mkdir -p "$(LOCAL_DEVEL_ROOT)/devel"
	cp -a "$(DEVEL)/include" "$(LOCAL_DEVEL_ROOT)/devel/"
	install -m 0644 ../broker/qdistro_broker.if "$@"

$(MODULE).pp: $(MODULE).te $(MODULE).if $(MODULE).fc $(LOCAL_INCLUDE)/contrib/qdistro_broker.if
	$(MAKE) -f $(DEVEL)/Makefile SHAREDIR="$(LOCAL_DEVEL_ROOT)" $(MODULE).pp

install: $(MODULE).pp
	semodule -i $(MODULE).pp
	@echo "[tier1] policy module loaded"
	@semodule -l | grep -q "^$(MODULE)" && echo "[tier1] active" || echo "[tier1] WARN: not listed"

remove:
	semodule -r $(MODULE) 2>/dev/null || true
	@echo "[tier1] policy module removed"

clean:
	rm -f $(MODULE).pp $(MODULE).pp.bz2 tmp/$(MODULE).mod tmp/$(MODULE).mod.fc
	rm -rf tmp/
