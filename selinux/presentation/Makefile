MODULE := qdistro_presentation
DEVEL  := /usr/share/selinux/devel

.PHONY: all install remove clean

all: $(MODULE).pp

$(MODULE).pp: $(MODULE).te $(MODULE).if $(MODULE).fc
	$(MAKE) -f $(DEVEL)/Makefile $(MODULE).pp

install: $(MODULE).pp
	semodule -i $(MODULE).pp

remove:
	-semodule -r $(MODULE)

clean:
	rm -f $(MODULE).pp $(MODULE).mod $(MODULE).fc.tmp tmp
