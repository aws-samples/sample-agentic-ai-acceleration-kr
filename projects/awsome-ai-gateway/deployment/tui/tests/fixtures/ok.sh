#!/usr/bin/env bash
echo "hello"
if [ -n "${GREETING:-}" ]; then echo "$GREETING"; fi
exit 0
