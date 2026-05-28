# Standalone build for qsu C binary.
# Also built by the meson project in ../daemons/ — this Makefile is a
# convenience for development and for install.sh-style deployment.

CC      ?= cc
CFLAGS  ?= -O2 -Wall -Wextra -Wformat=2 -Werror=format-security
PREFIX  ?= /usr/local

qsu: qsu.c
	$(CC) $(CFLAGS) -o $@ $<

install: qsu
	install -D -m 0755 qsu $(DESTDIR)$(PREFIX)/bin/qsu

clean:
	rm -f qsu

.PHONY: install clean
