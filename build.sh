#!/bin/bash
set -euo pipefail

# Strata build script for Linux (CUDA)
ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$ROOT"

CLEAN=0
REBASE=0
BUILD_VISION=0
JOBS=$(nproc 2>/dev/null || echo 4)
ARCH=""

usage() {
    echo "Usage: ./build.sh [OPTIONS]"
    echo ""
    echo "Options:"
    echo "  --pull, --rebase   Rebase current branch on origin/main before building"
    echo "  --clean            Clean build directory before configuring"
    echo "  --vision           Also build strata-vision image encoder"
    echo "  --arch ARCH        Target CUDA architecture (default: auto-detected, e.g. 89)"
    echo "  -j, --jobs N       Number of parallel build jobs (default: $(nproc))"
    echo "  -h, --help         Show this help message"
    exit 0
}

while [[ $# -gt 0 ]]; do
    case "$1" in
        --pull|--rebase)
            REBASE=1
            shift
            ;;
        --clean)
            CLEAN=1
            shift
            ;;
        --vision)
            BUILD_VISION=1
            shift
            ;;
        --arch)
            ARCH="$2"
            shift 2
            ;;
        -j|--jobs)
            JOBS="$2"
            shift 2
            ;;
        -j*)
            JOBS="${1#-j}"
            shift
            ;;
        -h|--help)
            usage
            ;;
        *)
            echo "Unknown option: $1"
            usage
            ;;
    esac
done

if [[ "$REBASE" -eq 1 ]]; then
    echo "=== Fetching and rebasing on origin/main ==="
    git fetch origin
    CURRENT_BRANCH=$(git branch --show-current)
    if [[ -n "$CURRENT_BRANCH" ]]; then
        echo "Rebasing $CURRENT_BRANCH onto origin/main..."
        git rebase origin/main
    else
        echo "Detached HEAD; skipping rebase."
    fi
fi

# 1. Detect NVCC
NVCC=$(command -v nvcc 2>/dev/null || true)
if [[ -z "$NVCC" ]]; then
    for cand in /usr/local/cuda-13.4/bin/nvcc /usr/local/cuda/bin/nvcc; do
        if [[ -x "$cand" ]]; then
            NVCC="$cand"
            export PATH="$(dirname "$cand"):$PATH"
            break
        fi
    done
fi

if [[ -z "$NVCC" ]]; then
    echo "Error: nvcc (CUDA compiler) not found."
    echo "Please ensure CUDA Toolkit is installed and nvcc is in your PATH."
    exit 1
fi
echo "Using CUDA compiler: $NVCC ($("$NVCC" --version | grep release | awk '{print $5}' | sed 's/,//'))"

# 2. Detect GPU Architecture
if [[ -z "$ARCH" ]]; then
    if command -v nvidia-smi >/dev/null 2>&1; then
        CC=$(nvidia-smi --query-gpu=compute_cap --format=csv,noheader,nounits 2>/dev/null | head -n1 | tr -d '.\r\n' || true)
        if [[ -n "$CC" ]]; then
            ARCH="$CC"
        fi
    fi
fi
ARCH="${ARCH:-89}"
echo "Targeting CUDA architecture: sm_$ARCH"

# 3. Ensure pinned llama.cpp dependency is present
LLAMA_DIR="$ROOT/third_party/llama.cpp"
if [[ ! -f "$LLAMA_DIR/ggml/CMakeLists.txt" ]]; then
    echo "=== Fetching pinned llama.cpp dependency ==="
    mkdir -p "$ROOT/third_party"
    LLAMA_COMMIT="3cf03257f219afbe7334045ff7c6a06ac68c627d"
    curl -sL "https://github.com/ggml-org/llama.cpp/archive/${LLAMA_COMMIT}.tar.gz" | tar -xz -C "$ROOT/third_party/"
    rm -rf "$LLAMA_DIR"
    mv "$ROOT/third_party/llama.cpp-${LLAMA_COMMIT}" "$LLAMA_DIR"
fi

# 4. Clean if requested
if [[ "$CLEAN" -eq 1 ]]; then
    echo "=== Cleaning build directory ==="
    rm -rf "$ROOT/build" "$ROOT/build-vision"
fi

# 5. Build Strata Engine
echo "=== Configuring Strata ==="
GENERATOR="Ninja"
if ! command -v ninja >/dev/null 2>&1; then
    GENERATOR="Unix Makefiles"
fi

cmake -B "$ROOT/build" -G "$GENERATOR" \
  -DCMAKE_BUILD_TYPE=Release \
  -DSTRATA_ENABLE_CUDA=ON \
  -DSTRATA_BUILD_TESTS=OFF \
  -DCMAKE_CUDA_ARCHITECTURES="$ARCH" \
  -DCMAKE_CUDA_COMPILER="$NVCC" \
  -DSTRATA_GGML_DIR="$LLAMA_DIR"

echo "=== Building strata binary ($JOBS jobs) ==="
cmake --build "$ROOT/build" --target strata -j"$JOBS"

# 6. Copy binary to engine/ and update BUILD.json
# A running binary cannot be overwritten in place (cp fails with "text file
# busy"), but it can be renamed: the running engine keeps the old inode until
# it exits, and the new binary is used on the next engine load.  The .old file
# stays for rollback.
mkdir -p "$ROOT/engine"
[[ -f "$ROOT/engine/strata" ]] && mv "$ROOT/engine/strata" "$ROOT/engine/strata.old"
cp "$ROOT/build/strata" "$ROOT/engine/strata"
chmod +x "$ROOT/engine/strata"

# 7. Optional vision encoder build
if [[ "$BUILD_VISION" -eq 1 ]]; then
    echo "=== Building strata-vision ==="
    cmake -B "$ROOT/build-vision" -G "$GENERATOR" \
      -S "$ROOT/tools/vision" \
      -DCMAKE_BUILD_TYPE=Release \
      -DLLAMA_DIR="$LLAMA_DIR" \
      -DSTRATA_VISION_CUDA=ON \
      -DCMAKE_CUDA_ARCHITECTURES="$ARCH" \
      -DCMAKE_CUDA_COMPILER="$NVCC"
    cmake --build "$ROOT/build-vision" --target strata-vision -j"$JOBS"
    [[ -f "$ROOT/engine/strata-vision" ]] && mv "$ROOT/engine/strata-vision" "$ROOT/engine/strata-vision.old"
    if [[ -f "$ROOT/build-vision/bin/strata-vision" ]]; then
        cp "$ROOT/build-vision/bin/strata-vision" "$ROOT/engine/strata-vision"
    elif [[ -f "$ROOT/build-vision/strata-vision" ]]; then
        cp "$ROOT/build-vision/strata-vision" "$ROOT/engine/strata-vision"
    fi
    chmod +x "$ROOT/engine/strata-vision"
fi

CUDA_DIR="$(cd "$(dirname "$NVCC")/.." && pwd)"
CUDA_LIB_DIR="$CUDA_DIR/lib64"
VERSION=$(git describe --tags --always 2>/dev/null || echo "0.1.41")
VERSION_NUM=$(grep -oP 'project\(strata VERSION \K[0-9.]+' "$ROOT/CMakeLists.txt" 2>/dev/null || echo "0.1.41")

SRC_HASH=$(python3 -c "import setup; print(setup.source_hash(setup.ENGINE_SOURCES))" 2>/dev/null || echo "")
VSRC_HASH=$(python3 -c "import setup; print(setup.source_hash(setup.VISION_SOURCES))" 2>/dev/null || echo "")
HAS_VISION=$([[ -f "$ROOT/engine/strata-vision" ]] && echo '"gpu"' || echo '"none"')

cat << EOF > "$ROOT/engine/BUILD.json"
{
  "source": "local",
  "version": "$VERSION_NUM",
  "archs": [$ARCH],
  "vision": $HAS_VISION,
  "cuda_dirs": ["$CUDA_LIB_DIR", "/usr/local/cuda/lib64"],
  "src": "$SRC_HASH",
  "vision_src": "$VSRC_HASH"
}
EOF

echo ""
echo "=== Strata successfully built! ==="
echo "Binary:  $ROOT/engine/strata"
echo "Version: $VERSION (sm_$ARCH)"
echo "Ready to run: ./setup.sh or ./run-<model>.sh"
