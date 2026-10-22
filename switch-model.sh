#!/bin/bash
set -e

DIR="/home/alessandro/git/Strata"
CURRENT_LINK="$DIR/run-current.sh"

MODEL="${1:-}"

if [ "$MODEL" = "nvfp4" ]; then
    if [ ! -f "$DIR/run-nvfp4.sh" ]; then
        echo "Error: $DIR/run-nvfp4.sh not found. NVFP4 is still downloading or not configured yet."
        exit 1
    fi
    echo "Switching active model to NVFP4..."
    ln -sf "$DIR/run-nvfp4.sh" "$CURRENT_LINK"
elif [ "$MODEL" = "iq3_s" ] || [ "$MODEL" = "iq3" ]; then
    echo "Switching active model to IQ3_S..."
    ln -sf "$DIR/run-iq3_s.sh" "$CURRENT_LINK"
else
    ACTIVE="unknown"
    if [ -L "$CURRENT_LINK" ]; then
        ACTIVE="$(basename "$(readlink -f "$CURRENT_LINK")" | sed 's/run-//;s/\.sh//')"
    fi
    echo "Usage: strata-switch [iq3_s|nvfp4]"
    echo "Current active model: $ACTIVE"
    exit 0
fi

systemctl --user restart strata
ACTIVE="$(basename "$(readlink -f "$CURRENT_LINK")" | sed 's/run-//;s/\.sh//')"
echo "Strata restarted with $ACTIVE (lazy loaded, idle unload off)."
