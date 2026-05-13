#!/bin/bash
ps -eo args --no-headers | grep 'qdistro-forward' | grep -v grep | head -1
