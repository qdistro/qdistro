#!/bin/bash
find /usr/bin /usr/libexec -maxdepth 2 -name 'weston-*' -executable 2>/dev/null | sort
echo ---
rpm -qa | grep -E '^weston' 2>/dev/null
