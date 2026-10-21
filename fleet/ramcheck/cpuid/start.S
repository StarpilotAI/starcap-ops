/*
 * Entry point. The RK3576 boot ROM loads the first maskrom stage (USB request
 * 0x471) at 0x3ff81000 in on-chip SRAM and calls it, with a usable stack, the
 * same way it calls the DDR blob (whose first instructions check they run at
 * 0x3ff81000, then save x29/x30 on the ROM's stack). We do the same and return
 * to the ROM when done, so the board stays in maskrom.
 */
	.section .text.start, "ax"
	.global _start
_start:
	stp	x29, x30, [sp, #-16]!
	mov	x29, sp
	bl	cpuid_main
	ldp	x29, x30, [sp], #16
	ret
