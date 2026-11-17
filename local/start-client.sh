#!/bin/sh
# Keep interactive terminals alive when the separate game process exits.
runuser -u node -- /usr/local/bin/local-client &
exec sleep infinity
