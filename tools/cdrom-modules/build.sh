#!/bin/sh
# Builds and installs the CD-ROM drivers (cdrom, sr_mod) for the running
# kernel, so a USB CD drive shows up as /dev/sr0.
#
# The UNO Q kernel has USB storage and SCSI disks built in, but not SCSI
# CD-ROMs. Arduino publishes the matching kernel headers, so the two
# drivers can be built here from the mainline sources of the same version.
#
# Run again after a kernel update (the modules belong to one kernel).
set -e
cd "$(dirname "$0")"

KVER=$(uname -r)
# 7.0.0-g122c2c22d838 -> v7.0, 7.0.3-gabc -> v7.0.3
TAG=v$(echo "$KVER" | sed -E 's/^([0-9]+\.[0-9]+)\.0([^0-9].*)?$/\1/; s/^([0-9]+\.[0-9]+\.[0-9]+).*/\1/')
BASE=https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/plain
echo "Kernel $KVER, mainline sources $TAG"

# 1. Can this kernel take our modules at all?
CONFIG=$(zcat /proc/config.gz)
echo "$CONFIG" | grep -q '^CONFIG_MODULES=y' || { echo "This kernel cannot load modules."; exit 1; }
if echo "$CONFIG" | grep -q '^CONFIG_MODULE_SIG_FORCE=y'; then
    echo "This kernel only loads signed modules; a self-built one will be refused."; exit 1
fi

# 2. Compiler and the headers for exactly this kernel
NEED=""
command -v gcc >/dev/null || NEED="$NEED build-essential"
[ -e "/lib/modules/$KVER/build/Makefile" ] || NEED="$NEED linux-headers-$KVER"
command -v curl >/dev/null || NEED="$NEED curl"
if [ -n "$NEED" ]; then
    echo "Installing:$NEED"
    sudo apt-get update -qq
    sudo apt-get install -y $NEED
fi

# 3. The driver sources
for f in drivers/scsi/sr.c drivers/scsi/sr.h drivers/scsi/sr_ioctl.c \
         drivers/scsi/sr_vendor.c drivers/scsi/scsi_logging.h drivers/cdrom/cdrom.c; do
    echo "  fetching $f"
    curl -fsSL "$BASE/$f?h=$TAG" -o "$(basename "$f")"
done

# This kernel is built without CONFIG_CDROM, so struct gendisk has no "cdi"
# field. Only cdrom.c itself writes it; the built-in kernel code reads it
# through disk_to_cdi(), which is NULL without CONFIG_CDROM anyway. Drop the
# write (adding the field would change the struct and break the kernel).
sed -i 's|^\(\s*\)disk->cdi = cdi;|\1/* struct gendisk has no cdi field here (CONFIG_CDROM=n) */|' cdrom.c
grep -q 'disk->cdi' cdrom.c && { echo "cdrom.c still uses disk->cdi: patch did not apply"; exit 1; }

# 4. Build, install, load
make
sudo install -D -m 644 cdrom.ko  "/lib/modules/$KVER/updates/cdrom.ko"
sudo install -D -m 644 sr_mod.ko "/lib/modules/$KVER/updates/sr_mod.ko"
sudo depmod -a
sudo modprobe sr_mod                       # pulls in cdrom
echo sr_mod | sudo tee /etc/modules-load.d/cdrom.conf >/dev/null   # also after a reboot
lsmod | grep -E '^(sr_mod|cdrom)'
echo "Done. Plug in the drive: it should appear as /dev/sr0 (check with: lsblk)."
