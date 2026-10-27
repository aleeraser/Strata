#!/bin/bash
set -e

DIR="/home/alessandro/git/Strata"
CURRENT_LINK="$DIR/run-current.sh"

MODEL="${1:-}"

case "$MODEL" in
    nvfp4|nvfp4-512k)
        TARGET="run-nvfp4-512k.sh"
        ;;
    nvfp4-256k)
        TARGET="run-nvfp4-256k.sh"
        ;;
    iq3_s|iq3|iq3_s-1m)
        TARGET="run-iq3_s-1m.sh"
        ;;
    iq3_s-512k)
        TARGET="run-iq3_s-512k.sh"
        ;;
    iq3_s-256k)
        TARGET="run-iq3_s-256k.sh"
        ;;
    *)
        ACTIVE="unknown"
        if [ -L "$CURRENT_LINK" ]; then
            ACTIVE="$(basename "$(readlink -f "$CURRENT_LINK")" | sed 's/run-//;s/\.sh//')"
        fi
        echo "Usage: strata-switch [nvfp4-512k | nvfp4-256k | iq3_s-1m | iq3_s-512k | iq3_s-256k]"
        echo "Current active model: $ACTIVE"
        exit 0
        ;;
esac

if [ ! -f "$DIR/$TARGET" ]; then
    echo "Error: $DIR/$TARGET not found."
    exit 1
fi

echo "Switching active model to $TARGET..."
ln -sf "$DIR/$TARGET" "$CURRENT_LINK"

systemctl --user restart strata
ACTIVE="$(basename "$(readlink -f "$CURRENT_LINK")" | sed 's/run-//;s/\.sh//')"
echo "Strata restarted with $ACTIVE (lazy loaded, idle unload off)."
