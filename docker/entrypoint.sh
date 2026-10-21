#!/bin/sh
# The container's command: `orion <arguments>` (the image and compose.yaml run `orion run --no-voice`).
# Before `run`, docker/prepare.py does what ./install.sh and `orion setup` do natively, minus the systemd units.
set -eu
case " $* " in
    *" run "*) python /app/docker/prepare.py ;;
esac
exec orion "$@"
