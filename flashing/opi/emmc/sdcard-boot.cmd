# StarCap: hand boot straight to the eMMC, from the SD card.
#
# WHY THIS FILE EXISTS, ON THE SD CARD OF ALL PLACES
#
# This board's U-Boot ships boot_targets with the SD BEFORE the eMMC:
#     boot_targets = usb0 mmc1 nvme0 nvme1 scsi0 mmc0 mtd2 mtd1 mtd0 pxe dhcp
#     mmc0 = /mmc@fe2e0000  (8-bit, non-removable, hs400)  = eMMC
#     mmc1 = /mmc@fe2c0000  (4-bit, cd-gpios)              = SD slot
# so with a card inserted, U-Boot works the 119 GB recording card over before it
# ever looks at the eMMC the OS actually lives on.
#
# That order cannot be changed from the image:
#   - boot_targets is compiled into u-boot.itb, and that FIT carries sha256
#     hashes on every image plus a signature on /configurations/conf. Patching
#     the string breaks the hash and the board stops booting entirely.
#   - U-Boot's env backend is ENV_IS_NOWHERE, so `setenv` + `saveenv` has
#     nowhere to persist to. There is no saved environment to edit.
#
# What CAN be changed is the SD card, which is ours and is not signed. U-Boot
# scans each device for boot_scripts (boot.scr.uimg, boot.scr) under
# boot_prefixes (/ then /boot/). It reaches the SD second — so a script here runs
# EARLY, and all it has to do is point the scan at the eMMC and re-run it.
#
# setenv only has to survive in RAM for the next few milliseconds, so
# ENV_IS_NOWHERE costs us nothing here.
echo "StarCap: SD card present - handing boot to eMMC (mmc0)"
setenv boot_targets mmc0
run distro_bootcmd
