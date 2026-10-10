#!/usr/bin/env bash
# Build pinned tools in ./tools by default. Requires network access.
set -euo pipefail
experiment_dir=$(cd -- "$(dirname -- "$0")" && pwd)
tool_dir=${1:-$experiment_dir/tools}
mkdir -p "$tool_dir"
tool_dir=$(cd -- "$tool_dir" && pwd)
export UV_CACHE_DIR="$tool_dir/uv-cache"

checkout() {
    local destination=$1 remote=$2 revision=$3
    if [[ ! -d "$destination/.git" ]]; then
        git init "$destination"
        git -C "$destination" remote add origin "$remote"
        git -C "$destination" fetch --depth 1 origin "$revision"
        git -C "$destination" checkout --detach FETCH_HEAD
    fi
    [[ $(git -C "$destination" rev-parse HEAD) == "$revision" ]] || {
        echo "Unexpected revision in $destination; use a fresh tool directory." >&2
        exit 1
    }
}

apply_once() {
    local destination=$1 patch=$2
    if git -C "$destination" apply --check "$patch"; then
        git -C "$destination" apply "$patch"
    else
        git -C "$destination" apply --reverse --check "$patch"
    fi
}

if [[ ! -x "$tool_dir/venv/bin/python" ]]; then
    uv venv --python /usr/bin/python3 "$tool_dir/venv"
fi
uv pip install --python "$tool_dir/venv/bin/python" 'meson==1.2.3' pip packaging tomli
export PATH="$tool_dir/venv/bin:$PATH"

# afl-qemu-trace is the LibAFL forkserver target. The historical AFL++ custom-mutator
# adapter (afl-symqemu-adapter.patch, custom_mutators/symqemu) is no longer built.
checkout "$tool_dir/AFLplusplus" https://github.com/AFLplusplus/AFLplusplus.git dbaf11913c1b2702dee5b4d3dcfffd52f1defe50
make -C "$tool_dir/AFLplusplus" -j8 afl-fuzz afl-showmap afl-tmin
git -C "$tool_dir/AFLplusplus" submodule update --init --depth 1 qemu_mode/qemuafl
apply_once "$tool_dir/AFLplusplus/qemu_mode/qemuafl" "$experiment_dir/qemu-ijon-quiet.patch"
(cd "$tool_dir/AFLplusplus/qemu_mode" && NO_CHECKOUT=1 ./build_qemu_support.sh)

checkout "$tool_dir/symqemu" https://github.com/eurecom-s3/symqemu.git 6e37dd2c3bace02997f358a7fb5d2b23bf4a5b59
git -C "$tool_dir/symqemu" submodule update --init --depth 1 subprojects/symcc-rt
git -C "$tool_dir/symqemu/subprojects/symcc-rt" submodule update --init --depth 1 src/backends/qsym/qsym
apply_once "$tool_dir/symqemu" "$experiment_dir/symqemu-movcond.patch"
apply_once "$tool_dir/symqemu/subprojects/symcc-rt" "$experiment_dir/symcc-ptx-input.patch"
mkdir -p "$tool_dir/symqemu/build"
(cd "$tool_dir/symqemu/build" && ../configure \
    --python="$tool_dir/venv/bin/python" --audio-drv-list= \
    --disable-sdl --disable-gtk --disable-vte --disable-opengl \
    --disable-virglrenderer --disable-werror --disable-docs --disable-pixman \
    --target-list=x86_64-linux-user --symcc-rt-llvm-version=14 \
    --enable-symcc-rt-trust-system-z3 && ninja -j8 qemu-x86_64)
ln -sfn qemu-x86_64 "$tool_dir/symqemu/build/symqemu-x86_64"

# LibAFL is pinned in libafl/Cargo.toml and Cargo.lock.
cargo build --release --manifest-path "$experiment_dir/libafl/Cargo.toml" --target-dir "$tool_dir/libafl-target"
