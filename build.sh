#!/usr/bin/env bash
# Build a Firecracker microVM image set: Alpine guest + SSH + w3m + persistent disk.
# Needs no host root: staging and image creation run inside a user namespace,
# which makes the resulting ext4 images carry proper root:root ownership.
set -euo pipefail
cd "$(dirname "$(readlink -f "$0")")"

# --- knobs -------------------------------------------------------------------
ARCH="${ARCH:-$(uname -m)}"
ROOTFS_SIZE="${ROOTFS_SIZE:-2G}"      # size of the / image (sparse file on host)
PERSIST_SIZE="${PERSIST_SIZE:-4G}"    # size of /persist (only created once)
ALPINE_VERSION="${ALPINE_VERSION:-}"  # e.g. 3.24.2; empty = latest-stable
ALPINE_BRANCH="${ALPINE_BRANCH:-}"    # repo branch e.g. v3.24; empty = derived
VM_PASSWORD="${VM_PASSWORD:-microvm}" # root password for the serial console
EXTRA_PKGS="${EXTRA_PKGS:-}"          # extra Alpine packages, e.g. chromium-chromedriver
KEEP_STAGE="${KEEP_STAGE:-0}"         # 1 = keep build/stage for inspection

S3="https://s3.amazonaws.com/spec.ccfc.min"
GITHUB="https://github.com/firecracker-microvm/firecracker"
ALPINE_CDN="https://dl-cdn.alpinelinux.org/alpine"

for c in curl tar truncate mke2fs ssh-keygen openssl unshare; do
  command -v "$c" >/dev/null || { echo "error: missing required tool: $c" >&2; exit 1; }
done

# Run the whole build inside a user namespace unless we are already root.
if [ "${MICROVM_INNER:-0}" != 1 ]; then
  if [ "$(id -u)" -eq 0 ]; then
    export MICROVM_INNER=1
  elif unshare -Ur true 2>/dev/null; then
    exec unshare -Ur env MICROVM_INNER=1 bash "$0" "$@"
  else
    echo "error: user namespaces unavailable; re-run as root (sudo $0)" >&2
    exit 1
  fi
fi

mkdir -p images build keys run

# --- 1. firecracker + jailer binaries ----------------------------------------
if [ ! -x images/firecracker ]; then
  echo "==> downloading firecracker"
  tag="${FC_VERSION:-$(basename "$(curl -fsSLI -o /dev/null -w '%{url_effective}' "$GITHUB/releases/latest")")}"
  case "$tag" in v*) ;; *) tag="v$tag" ;; esac
  curl -fL --retry 3 "$GITHUB/releases/download/$tag/firecracker-$tag-$ARCH.tgz" -o build/fc.tgz
  tar -xzf build/fc.tgz -C build --no-same-owner
  mv "build/release-$tag-$ARCH/firecracker-$tag-$ARCH" images/firecracker
  [ -f "build/release-$tag-$ARCH/jailer-$tag-$ARCH" ] && \
    mv "build/release-$tag-$ARCH/jailer-$tag-$ARCH" images/jailer || true
  echo "$tag" > build/fc.version
  rm -rf "build/release-$tag-$ARCH" build/fc.tgz
else
  echo "==> firecracker binary already present ($(cat build/fc.version 2>/dev/null || echo unknown))"
fi

# --- 2. guest kernel (Firecracker CI artifacts) ------------------------------
if [ ! -e images/vmlinux ]; then
  echo "==> downloading guest kernel"
  prefix=$(curl -fsSL --retry 3 "$S3?list-type=2&prefix=firecracker-ci/&delimiter=/" \
    | grep -o '<Prefix>firecracker-ci/[0-9]\{8\}[^<]*' | sed 's/<Prefix>//' | sort | tail -1)
  key=$(curl -fsSL --retry 3 "$S3?list-type=2&prefix=$prefix$ARCH/vmlinux-" \
    | grep -o '<Key>[^<]*' | sed 's/<Key>//' | grep -vE '\.config$|-no-acpi$' | sort -V | tail -1)
  ver=${key##*/vmlinux-}
  curl -fL --retry 3 "$S3/$key" -o "images/vmlinux-$ver"
  ln -sfn "vmlinux-$ver" images/vmlinux
fi

# --- 3. Alpine minirootfs + static apk ---------------------------------------
if [ -z "$ALPINE_VERSION" ]; then
  yaml=$(curl -fsSL --retry 3 "$ALPINE_CDN/latest-stable/releases/$ARCH/latest-releases.yaml")
  ALPINE_VERSION=$(printf '%s\n' "$yaml" | awk '/^  version:/{v=$2} /file: alpine-minirootfs-/{print v; exit}')
fi
[ -n "$ALPINE_BRANCH" ] || ALPINE_BRANCH="v${ALPINE_VERSION%.*}"
minirootfs="alpine-minirootfs-$ALPINE_VERSION-$ARCH.tar.gz"
[ -f "build/$minirootfs" ] || \
  curl -fL --retry 3 "$ALPINE_CDN/latest-stable/releases/$ARCH/$minirootfs" -o "build/$minirootfs"

apk_ver=$(curl -fsSL --retry 3 "$ALPINE_CDN/$ALPINE_BRANCH/main/$ARCH/APKINDEX.tar.gz" \
  | tar -xzO APKINDEX | awk '/^P:apk-tools-static$/{p=1} p&&/^V:/{print substr($0,3);p=0}')
[ -n "$apk_ver" ] || { echo "error: could not resolve apk-tools-static version" >&2; exit 1; }
if [ ! -x build/sbin/apk.static ] || [ "$(cat build/apk.static.version 2>/dev/null || true)" != "$apk_ver" ]; then
  echo "==> downloading apk-tools-static $apk_ver"
  rm -rf build/sbin
  mkdir -p build/sbin
  curl -fL --retry 3 "$ALPINE_CDN/$ALPINE_BRANCH/main/$ARCH/apk-tools-static-$apk_ver.apk" -o build/apk.apk
  tar -xzOf build/apk.apk sbin/apk.static > build/sbin/apk.static
  chmod +x build/sbin/apk.static
  echo "$apk_ver" > build/apk.static.version
  rm -f build/apk.apk
fi

# --- 4. SSH key --------------------------------------------------------------
if [ ! -f keys/id_ed25519 ]; then
  echo "==> generating SSH key for the guest"
  ssh-keygen -q -t ed25519 -N '' -C microvm -f keys/id_ed25519
fi

# --- 5. stage the guest rootfs ------------------------------------------------
STAGE=build/stage
echo "==> staging Alpine $ALPINE_VERSION (user namespace, no host root needed)"
rm -rf "$STAGE"
mkdir -p "$STAGE"
tar -xzf "build/$minirootfs" -C "$STAGE" --no-same-owner

pkgs="alpine-base openssh openssh-client w3m ca-certificates e2fsprogs chromium"
pkgs="$pkgs $EXTRA_PKGS"
# shellcheck disable=SC2086
build/sbin/apk.static --root "$STAGE" --arch "$ARCH" --initdb --no-cache \
  --repository "$ALPINE_CDN/$ALPINE_BRANCH/main" \
  --repository "$ALPINE_CDN/$ALPINE_BRANCH/community" \
  add $pkgs

# identity + network (host side: 172.16.0.1/24, see net.sh / vm.json)
echo microvm > "$STAGE/etc/hostname"
cat > "$STAGE/etc/network/interfaces" <<'EOF'
auto lo
iface lo inet loopback

auto eth0
iface eth0 inet static
	address 172.16.0.2/24
	gateway 172.16.0.1
EOF
printf 'nameserver 1.1.1.1\nnameserver 8.8.8.8\n' > "$STAGE/etc/resolv.conf"

# persistent disk: second virtio-blk drive (config order -> /dev/vdb)
mkdir -p "$STAGE/persist"
cat > "$STAGE/etc/fstab" <<'EOF'
# <device>	<mountpoint>	<fstype>	<options>	<dump>	<pass>
/dev/vdb	/persist	ext4	defaults,noatime	0 2
EOF

# things that live on the persistent disk
mkdir -p "$STAGE/root/.config"
ln -sfn /persist/w3m      "$STAGE/root/.w3m"
ln -sfn /persist/chromium "$STAGE/root/.config/chromium"
ln -sfn /persist/work     "$STAGE/root/work"

# make the persistent subdirs exist even on a persist.ext4 built before them
mkdir -p "$STAGE/etc/local.d"
cat > "$STAGE/etc/local.d/10-persist.start" <<'EOF'
#!/bin/sh
mkdir -p /persist/w3m /persist/work /persist/chromium
EOF
chmod 755 "$STAGE/etc/local.d/10-persist.start"

# serial console (ttyS0) + root password for it; SSH stays key-only
sed -i 's|^#ttyS0::respawn.*|ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100|' "$STAGE/etc/inittab"
grep -q '^ttyS0::' "$STAGE/etc/inittab" || \
  echo 'ttyS0::respawn:/sbin/getty -L 115200 ttyS0 vt100' >> "$STAGE/etc/inittab"
printf 'console\nttyS0\ntty1\ntty2\n' > "$STAGE/etc/securetty"
hash=$(openssl passwd -6 "$VM_PASSWORD")
awk -v h="$hash" 'BEGIN{FS=OFS=":"} $1=="root"{$2=h} {print}' "$STAGE/etc/shadow" > "$STAGE/etc/shadow.new"
mv "$STAGE/etc/shadow.new" "$STAGE/etc/shadow"
chmod 640 "$STAGE/etc/shadow"

# ssh: key-only root login, host keys generated at build time
mkdir -p "$STAGE/root/.ssh"
chmod 700 "$STAGE/root/.ssh"
cp keys/id_ed25519.pub "$STAGE/root/.ssh/authorized_keys"
chmod 600 "$STAGE/root/.ssh/authorized_keys"
ssh-keygen -q -A -f "$STAGE"
cat > "$STAGE/etc/ssh/sshd_config" <<'EOF'
Port 22
PermitRootLogin prohibit-password
PasswordAuthentication no
KbdInteractiveAuthentication no
AuthorizedKeysFile .ssh/authorized_keys
HostKey /etc/ssh/ssh_host_ed25519_key
HostKey /etc/ssh/ssh_host_rsa_key
Subsystem sftp /usr/lib/ssh/sftp-server
PrintMotd no
EOF

printf 'microvm (Alpine) - only /persist survives a rootfs rebuild.\nw3m <url> to browse; chromium --headless --no-sandbox for automation.\n' > "$STAGE/etc/motd"

# openrc services (rc-update equivalent, done as plain symlinks)
for s in devfs sysfs procfs bootmisc hostname modules hwdrivers localmount loopback; do
  [ -x "$STAGE/etc/init.d/$s" ] && ln -sf "/etc/init.d/$s" "$STAGE/etc/runlevels/boot/$s"
done
for s in networking sshd local; do
  [ -x "$STAGE/etc/init.d/$s" ] && ln -sf "/etc/init.d/$s" "$STAGE/etc/runlevels/default/$s"
done

# --- 6. build the ext4 images ------------------------------------------------
echo "==> building images/rootfs.ext4 ($ROOTFS_SIZE)"
rm -f images/rootfs.ext4
truncate -s "$ROOTFS_SIZE" images/rootfs.ext4
mke2fs -q -t ext4 -F -m 0 -L microvm-rootfs -d "$STAGE" images/rootfs.ext4

if [ ! -e images/persist.ext4 ]; then
  echo "==> creating images/persist.ext4 ($PERSIST_SIZE, never overwritten by rebuilds)"
  mkdir -p build/persist-stage/{w3m,chromium,work}
  truncate -s "$PERSIST_SIZE" images/persist.ext4
  mke2fs -q -t ext4 -F -m 0 -L persist -d build/persist-stage images/persist.ext4
else
  echo "==> keeping existing images/persist.ext4"
fi

[ "$KEEP_STAGE" = 1 ] || rm -rf "$STAGE"

cat <<EOF

==> done
  images/firecracker    $(cat build/fc.version 2>/dev/null || echo)
  images/vmlinux        -> $(readlink images/vmlinux)
  images/rootfs.ext4    Alpine $ALPINE_VERSION, ssh + w3m + headless chromium
  images/persist.ext4   mounted at /persist by the guest

  sudo ./net.sh up   # create tap + NAT (needs root once per boot)
  ./run.sh           # boot; serial console attached to this terminal
  ./ssh.sh           # from another terminal: ssh as root (key auth)
EOF
