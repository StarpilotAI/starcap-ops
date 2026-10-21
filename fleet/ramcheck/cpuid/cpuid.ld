/* The boot ROM loads the 0x471 stage here (the stock DDR blob checks it runs
 * at 0x3ff81000). One flat image: no .data/.bss needed by cpuid.c. */
ENTRY(_start)
SECTIONS
{
	. = 0x3ff81000;
	.text : { *(.text.start) *(.text*) }
	.rodata : { *(.rodata*) }
	.data : { *(.data*) *(.bss*) *(COMMON) }
	/DISCARD/ : { *(.comment) *(.note*) *(.eh_frame*) }
}
