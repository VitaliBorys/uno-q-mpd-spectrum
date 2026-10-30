# Out-of-tree build of the CD-ROM drivers the UNO Q kernel leaves out
# (CONFIG_CDROM, CONFIG_BLK_DEV_SR). Run build.sh, not make directly.
obj-m := cdrom.o sr_mod.o
sr_mod-y := sr.o sr_ioctl.o sr_vendor.o

KDIR ?= /lib/modules/$(shell uname -r)/build

all:
	$(MAKE) -C $(KDIR) M=$(CURDIR) modules

clean:
	$(MAKE) -C $(KDIR) M=$(CURDIR) clean
	rm -f sr.c sr.h sr_ioctl.c sr_vendor.c scsi_logging.h cdrom.c
