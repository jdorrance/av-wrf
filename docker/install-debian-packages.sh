#!/bin/sh
set -eu

apt-get update
apt-get install -y --no-install-recommends "$@"
rm -rf /var/lib/apt/lists/*
