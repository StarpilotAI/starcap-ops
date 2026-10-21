/*
 * Print an RK3576 board's cpu_id on its debug UART, running from SRAM only.
 *
 * Sent in maskrom in place of the DDR blob (USB request 0x471), so it works on
 * a board whose DRAM is dead. It reads the cpu-id fuses and derives the same
 * 16-hex serial U-Boot puts in serial#, which Linux shows as
 * /proc/device-tree/serial-number and the recorder uses as cpu_id. It writes
 * nothing anywhere except the UART.
 *
 * Every register write below is copied from what the stock DDR blob
 * (rk3576_ddr_lp4_1866M v1.13, "DDR e10b4dfeaf") does on these boards; the
 * addresses in comments are that blob's, disassembled at its load address
 * 0x3ff81000.
 *
 * Output, three times:
 *   CPUID=<serial>
 *   OTP=<the 16 raw cpu-id bytes>
 */
typedef unsigned int u32;
typedef unsigned char u8;
typedef unsigned long long u64;

#define REG(a) (*(volatile u32 *)(unsigned long)(a))

#define CRU         0x27200000UL
#define IOC         0x26040000UL
#define UART0       0x2ad40000UL
#define OTPC        0x2a580000UL

/* Busy wait. No timer is set up at this point; the CPU runs at most a few GHz,
 * so this is at least `us` microseconds, which is all the callers need. */
static void delay_us(u32 us)
{
	for (volatile u32 i = 0; i < us * 1000; i++)
		;
}

/* blob 0x3ff81648: UART0 clock and pins. */
static void uart_clock_and_pins(void)
{
	if (REG(CRU + 0x3a0) & (1 << 12)) {
		/* The blob's alternate clock path (0x3ff816c4), copied verbatim. */
		REG(CRU + 0x280) = 0x000c0000;
		REG(CRU + 0x1c0) = 0x03ff00b7;
		REG(CRU + 0x1c4) = 0x01ff0042;
		REG(CRU + 0x1c8) = 0xffffc4ec;
		REG(CRU + 0x280) = 0x000c0004;
		REG(CRU + 0x354) = 0x006409a8;
		REG(CRU + 0x358) = 0x00030000;
		REG(CRU + 0x3f0) = 0x07ff0401;
	} else {
		REG(CRU + 0x3f0) = 0x07ff0300;      /* blob 0x3ff81670 */
	}
	delay_us(11);                               /* blob 0x3ff81674: delay(0xb) */
	/* Default iomux (ddrbin "uart iomux" 0), blob 0x3ff81694..0x3ff816b8. */
	REG(IOC + 0x2010) = 0x000f0009;
	REG(IOC + 0x2010) = 0x00f00090;
	REG(IOC + 0x2030) = 0x0c000c00;
}

/* blob 0x3ff913d8: 8250-style init, 24 MHz clock, divisor 1 = 1500000 baud. */
static void uart_init(void)
{
	REG(UART0 + 0x88) = 7;                      /* SRR: reset UART + FIFOs */
	delay_us(1);
	REG(UART0 + 0x88) = 0;
	REG(UART0 + 0x0c) = 0x83;                   /* LCR: DLAB, 8N1 */
	REG(UART0 + 0x00) = 1;                      /* DLL: 24 MHz / 16 / 1 */
	REG(UART0 + 0x0c) = 3;                      /* LCR: 8N1 */
	REG(UART0 + 0x98) = 1;                      /* SFE: FIFOs on */
}

static void putc_(char c)
{
	while (!(REG(UART0 + 0x7c) & 2))            /* USR.TFNF, blob 0x3ff912b8 */
		;
	REG(UART0) = (u8)c;
}

static void puts_(const char *s)
{
	while (*s)
		putc_(*s++);
}

static void puthex(u64 v, int digits)
{
	for (int i = digits - 1; i >= 0; i--)
		putc_("0123456789abcdef"[(v >> (i * 4)) & 0xf]);
}

/*
 * One 32-bit word of the non-secure OTP. U-Boot drivers/misc/rockchip-otp.c
 * rockchip_rk3588_otp_read() with rk3576_data (ns_offset 0x1c0 words); the blob
 * runs the same sequence at 0x3ff81768. Returns -1 on timeout.
 */
static int otp_word(u32 addr, u32 *out)
{
	REG(OTPC + 0x84) = 0xf;                     /* INT_ST: clear */
	REG(OTPC + 0x04) = (addr << 16) | (1 << 8); /* AUTO_CTRL: addr, burst 1 */
	REG(OTPC + 0x08) = 1;                       /* AUTO_EN */
	for (u32 t = 0; !(REG(OTPC + 0x84) & 2); t++) {   /* RD_DONE */
		if (t > 10000)
			return -1;
		delay_us(1);
	}
	REG(OTPC + 0x84) = 2;
	*out = REG(OTPC + 0x20);                    /* DOUT0 */
	return 0;
}

/* U-Boot lib/crc32.c crc32_no_comp(): reflected CRC-32, no pre/post inversion. */
static u32 crc32_no_comp(u32 crc, const u8 *p, int n)
{
	while (n--) {
		crc ^= *p++;
		for (int k = 0; k < 8; k++)
			crc = (crc >> 1) ^ (0xedb88320 & -(crc & 1));
	}
	return crc;
}

void cpuid_main(void)
{
	u8 raw[20], id[16], low[8], high[8];
	int ok = 1;

	uart_clock_and_pins();
	uart_init();

	/* cpu-id: OTP byte 0x0a, 16 bytes (rk3576.dtsi id@a; U-Boot
	 * rk3576_common_def.h CFG_CPUID_OFFSET 0xa) = words 2..6 of the NS area. */
	for (u32 w = 0; w < 5; w++) {
		u32 v = 0;
		if (otp_word(0x1c0 + 2 + w, &v))
			ok = 0;
		for (int b = 0; b < 4; b++)
			raw[w * 4 + b] = v >> (8 * b);
	}
	for (int i = 0; i < 16; i++)
		id[i] = raw[2 + i];

	/* U-Boot arch/arm/mach-rockchip/board.c rockchip_set_serialno(). */
	for (int i = 0; i < 8; i++) {
		low[i] = id[1 + (i << 1)];
		high[i] = id[i << 1];
	}
	u64 serial = crc32_no_comp(0, low, 8);
	serial |= (u64)crc32_no_comp((u32)serial, high, 8) << 32;

	for (int rep = 0; rep < 3; rep++) {
		puts_("\r\n");
		if (!ok) {
			puts_("CPUID_ERROR=otp-timeout\r\n");
			continue;
		}
		puts_("CPUID=");
		/* serial# is "%llx": no leading zeros. */
		int digits = 16;
		while (digits > 1 && !((serial >> ((digits - 1) * 4)) & 0xf))
			digits--;
		puthex(serial, digits);
		puts_("\r\nOTP=");
		for (int i = 0; i < 16; i++)
			puthex(id[i], 2);
		puts_("\r\n");
	}
	/* Let the FIFO drain before the ROM takes over again (blob 0x3ff9139c). */
	while (!(REG(UART0 + 0x14) & (1 << 6)))
		;
}
