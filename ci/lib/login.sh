#!/usr/bin/env bash
# qci module: login-shell PATH helper. SOURCED by core.sh, and directly by
# gates/gui.sh, which several self-tests source without core.sh.
# shellcheck shell=bash

# qci_login_cmd CMD — CMD for `bash -lc`, with the caller's PATH restored first.
# A login shell sources /etc/profile, and openSUSE's rebuilds PATH from scratch
# unless PROFILEREAD is already set: true in a terminal session, false under
# `systemd-run --user` (the documented way to launch a long run). Without this,
# a tool or stub the operator (or a self-test) put on PATH silently vanishes
# from every step under systemd. The profile's own additions stay after it.
qci_login_cmd() { printf 'PATH=%q${PATH:+:$PATH}; %s' "$PATH" "$1"; }
