#!/usr/bin/env bash
#
# The forced command for the GitHub Actions SSH key. In authorized_keys:
#
#   command="/mnt/volume_nyc3_01/opt/courseops/deploy/ssh-deploy-command.sh",\
#   no-agent-forwarding,no-port-forwarding,no-pty,no-X11-forwarding ssh-ed25519 AAAA... github-deploy
#
# Whatever the client asked to run arrives in SSH_ORIGINAL_COMMAND, and the
# only thing this key can do with it is deploy a ref that looks like a tag or
# a branch name. A leaked repository secret therefore cannot open a shell,
# read .env, or run anything but deploy.sh.
#
# This replaces `command="deploy.sh ${SSH_ORIGINAL_COMMAND##* }"` written
# straight into authorized_keys. That passed the last word of anything
# through unexamined; deploy.sh quotes it, so it was not an injection, but
# nothing refused a bad ref before the script started and nothing could test
# it. This can be run under pytest with a hostile SSH_ORIGINAL_COMMAND.
set -euo pipefail

APP_DIR="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)"
cmd="${SSH_ORIGINAL_COMMAND:-}"

# Accept "<anything>/deploy/deploy.sh <ref>" or a bare "<ref>": the last word.
# A ref is letters, digits, dot, underscore, slash and dash, never leading
# with a dash (so it cannot become an option to git) and never containing a
# space, semicolon, quote or anything else a shell would read.
ref="${cmd##* }"
if [[ ! "$ref" =~ ^[A-Za-z0-9][A-Za-z0-9._/-]{0,60}$ ]]; then
    echo "refused: '$cmd'" >&2
    exit 2
fi
exec "$APP_DIR/deploy/deploy.sh" "$ref"
