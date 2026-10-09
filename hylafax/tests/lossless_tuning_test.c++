/*
 * Faxbot: build-time test of patch 0003 (lossless tuning, faxd/LosslessTuning.h).
 *
 * 1. The MR schedule's dynamic program against every legal schedule of small
 *    random pages (minimum, legality, and the cost of what it returns).
 * 2. Four synthetic pages, defined by formula so api/tests/test_encoder_tuning.py
 *    draws the same pixels: each goes through MemoryDecoder::convertDataFormat
 *    as faxsend converts it, with and without tuning.
 *    - MR: the tuned page decodes to the same pixels, its bytes are what the
 *      cost model said, the fixed schedule's model equals the bytes of the
 *      untuned conversion, and the tuned page is never larger.
 *    - JBIG: every candidate decodes to the same pixels (so no encode changes
 *      the raster the next one reads), the tuned page is the smallest
 *      candidate with the tie-break (lower options, then lower MX), and plain
 *      JBIG is options 0, MX 0 as before.
 * 3. The bytes of every page and coding, which Faxbot's own measurement
 *    (api/app/pages/coding.py) must equal; the same numbers are in its test.
 *
 * Any failure exits non-zero and stops the image build.
 */
#include "MemoryDecoder.h"
#include "G3Encoder.h"
#include "StackBuffer.h"
#include "LosslessTuning.h"
#include "tiffio.h"
#include <setjmp.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

extern "C" {
#include "jbig.h"
}

static int failures = 0;

#define CHECK(cond, ...) do { if (!(cond)) { failures++; printf("FAIL %s:%d: ", __FILE__, __LINE__); \
    printf(__VA_ARGS__); printf("\n"); } } while (0)

static const u_int WIDTH = 1728;
static const u_int ROWS = 240;
static const u_int BYTES = WIDTH / 8;

/* ---------------------------------------------------------------- 1. the dynamic program */

static unsigned long seed = 12345;
static unsigned long nextRandom()
{
    seed = (seed * 1103515245UL + 12345UL) & 0x7fffffffUL;
    return seed >> 8;
}

static unsigned long
bruteForce(const unsigned long* a, const unsigned long* b, unsigned long rows, unsigned int k,
    unsigned long minLen)
{
    unsigned long best = (unsigned long) -1;
    for (unsigned long mask = 0; mask < (1UL << rows); mask++) {
	if (!(mask & 1))
	    continue;				// the first row is one-dimensional
	unsigned int run = 0;
	bool legal = true;
	unsigned long total = 2;
	for (unsigned long y = 0; y < rows; y++) {
	    bool one = (mask >> y) & 1;
	    run = one ? 0 : run + 1;
	    if (run >= k) { legal = false; break; }
	    total += LosslessTuning::mrRowBytes(one ? a[y] : b[y], y, rows, minLen);
	}
	if (legal && total < best)
	    best = total;
    }
    return best;
}

static void
testSchedule()
{
    u_int cases = 0;
    for (unsigned long rows = 1; rows <= 11; rows++)
	for (unsigned int k = 1; k <= 5; k++)
	    for (unsigned long minLen = 0; minLen <= 4; minLen += 2)
		for (u_int trial = 0; trial < 12; trial++) {
		    unsigned long a[16], b[16];
		    bool oneD[16];
		    for (unsigned long y = 0; y < rows; y++) {
			a[y] = 3 + nextRandom() % 60;
			b[y] = 1 + nextRandom() % 60;
		    }
		    unsigned long got = LosslessTuning::scheduleMR(a, b, rows, k, minLen, oneD);
		    unsigned long want = bruteForce(a, b, rows, k, minLen);
		    CHECK(got == want, "schedule %lu, brute force %lu (rows %lu, K %u)", got, want, rows, k);
		    unsigned int run = 0;
		    unsigned long total = 2;
		    CHECK(oneD[0], "the first row must be one-dimensional");
		    for (unsigned long y = 0; y < rows; y++) {
			run = oneD[y] ? 0 : run + 1;
			CHECK(run < k, "more than K-1 two-dimensional rows in a row");
			total += LosslessTuning::mrRowBytes(oneD[y] ? a[y] : b[y], y, rows, minLen);
		    }
		    CHECK(total == got, "the schedule returned costs %lu, not %lu", total, got);
		    cases++;
		}
    printf("schedule: %u cases match brute force\n", cases);
}

/* ---------------------------------------------------------------- 2. synthetic pages */

static bool
pixel(u_int page, u_int x, u_int y, unsigned long& noise)
{
    switch (page) {
    case 0:	// text-like: blocks on lines of 12 rows
	return x >= 96 && x < 1632 && (y % 24) >= 4 && (y % 24) < 16
	    && ((x / 6) * 7 + (y / 24) * 3) % 5 != 0 && (x / 6) % 9 != 8;
    case 1:	// tint: a light diagonal dot screen behind a form's boxes
	return (x >= 64 && x < 1664 && y >= 20 && y < 220 && (x * 3 + y * 5) % 16 == 0)
	    || ((y == 20 || y == 120 || y == 219) && x >= 64 && x < 1664) || x == 64 || x == 863;
    case 2:	// noise: 30% black from a fixed LCG, in a band
	if (y < 40 || y >= 200 || x < 200 || x >= 1500)
	    return false;
	noise = (noise * 1103515245UL + 12345UL) & 0x7fffffffUL;
	return ((noise >> 16) % 100) < 30;
    default:	// sparse: a frame and a few rules
	return y == 10 || y == 229 || x == 40 || x == 1687 || (y % 40 == 20 && x > 300 && x < 1400);
    }
}

static void
drawPage(u_int page, u_char* raster)
{
    unsigned long noise = 4242;
    memset(raster, 0, BYTES * ROWS);
    for (u_int y = 0; y < ROWS; y++)
	for (u_int x = 0; x < WIDTH; x++)
	    if (pixel(page, x, y, noise))
		raster[y*BYTES + x/8] |= 0x80 >> (x % 8);
}

// The page as faxq hands it to faxsend: MMR, MSB first, ending in EOFB.
static u_char*
mmrPage(const u_char* raster, u_long& length)
{
    fxStackBuffer out;
    G3Encoder enc(out);
    enc.setupEncoder(FILLORDER_MSB2LSB, true, true);
    u_char ref[BYTES];
    memset(ref, 0, BYTES);
    for (u_int y = 0; y < ROWS; y++)
	enc.encode(raster + y*BYTES, WIDTH, 1, ref);
    enc.encoderCleanup();
    length = out.getLength();
    u_char* data = new u_char[length];
    memcpy(data, (const u_char*) out, length);
    return data;
}

static Class2Params
session(u_int df)
{
    Class2Params params;
    params.vr = VR_FINE;
    params.br = BR_14400;
    params.wd = WD_A4;
    params.ln = LN_A4;
    params.df = df;
    params.ec = EC_DISABLE;
    params.bf = BF_DISABLE;
    params.st = ST_0MS;
    params.jp = JP_NONE;
    return params;
}

// Decode an MR page and compare it with the raster.  The page gets the RTC that
// Class1Modem::sendRTC sends after it (six EOLs with tag bit 1).
static bool
decodesTo(u_char* page, u_long pageLength, const u_char* raster)
{
    u_long length = pageLength + 10;
    u_char* data = new u_char[length];
    memcpy(data, page, pageLength);
    memset(data + pageLength, 0, 10);
    for (u_int i = 0; i < 6; i++) {		// bits 13*i+11 and 13*i+12 are ones
	for (u_int bit = 13*i + 11; bit <= 13*i + 12; bit++)
	    data[pageLength + bit/8] |= 0x80 >> (bit % 8);
    }
    bool same = false;
    {
    MemoryDecoder dec(data, WIDTH, length, FILLORDER_MSB2LSB, true, false);
    u_char row[BYTES];
    volatile u_int y = 0;
    if (sigsetjmp(dec.jmpRTC, 0) == 0) {
	for (; y < ROWS; y++) {
	    memset(row, 0, BYTES);
	    dec.decodeRow(row, WIDTH);
	    if (memcmp(row, raster + y*BYTES, BYTES) != 0) {
		if (getenv("TUNING_DEBUG")) {
		    u_int x = 0;
		    while (x < BYTES && row[x] == raster[y*BYTES + x]) x++;
		    printf("  row %u differs from byte %u: got %02x want %02x\n", (u_int) y, x, row[x],
			raster[y*BYTES + x]);
		}
		break;
	    }
	}
    }
    same = (y == ROWS);
    }
    delete[] data;
    return same;
}

static bool
jbigDecodesTo(const u_char* bie, u_long length, const u_char* raster)
{
    struct jbg_dec_state state;
    jbg_dec_init(&state);
    size_t used = 0;
    int result = jbg_dec_in(&state, (unsigned char*) bie, length, &used);
    bool same = result == JBG_EOK && jbg_dec_getwidth(&state) == WIDTH && jbg_dec_getheight(&state) == ROWS
	&& memcmp(jbg_dec_getimage(&state, 0), raster, BYTES * ROWS) == 0;
    jbg_dec_free(&state);
    return same;
}

static void
collect(unsigned char* start, size_t len, void* file)
{
    ((fxStackBuffer*) file)->put((const char*) start, len);
}

static u_long
jbigBytes(const u_char* raster, int options, int mx, fxStackBuffer& out)
{
    u_char* copy = new u_char[BYTES * ROWS];
    memcpy(copy, raster, BYTES * ROWS);
    unsigned char* planes[1] = { copy };
    struct jbg_enc_state state;
    out.reset();
    jbg_enc_init(&state, WIDTH, ROWS, 1, planes, collect, &out);
    jbg_enc_options(&state, 0, options, 128, mx, 0);
    jbg_enc_out(&state);
    jbg_enc_free(&state);
    delete[] copy;
    return out.getLength();
}

// Faxbot's measurement must equal these (api/tests/test_encoder_tuning.py, GOLDEN).
struct Golden {
    const char* name;
    u_long mrFixed, mrTuned, mrFixedPadded, mrTunedPadded, jbigPlain, jbigTuned;
    int options, mx;
};
static const Golden GOLDEN[4] = {
    { "text",   5173,  5173,  7034,  7034,  1126,   849, 72, 0 },
    { "tint",  34092, 23462, 34655, 24035, 14155, 12606, 64, 0 },
    { "noise", 49300, 41518, 50541, 42759, 24967, 24964, 64, 0 },
    { "sparse",  997,   993,  4322,  4322,    72,    72,  0, 0 },
};
static const u_int PADDED = 18;		// 10 ms a line at 14,400 bit/s (Class2Params::minScanlineSize)

static u_char*
convert(const u_char* raster, u_int df, bool mr, bool jbig, u_int minLen, MemoryDecoder** keep, u_long& length)
{
    u_long sourceLength;
    u_char* source = mmrPage(raster, sourceLength);
    MemoryDecoder* dec = new MemoryDecoder(source, WIDTH, sourceLength, FILLORDER_MSB2LSB, true, true);
    dec->setTuning(mr, jbig, minLen);
    u_char* out = dec->convertDataFormat(session(df));
    length = dec->getCC();
    *keep = dec;
    delete[] source;
    return out;
}

static void
testPage(u_int page)
{
    const Golden& gold = GOLDEN[page];
    u_char* raster = new u_char[BYTES * ROWS];
    drawPage(page, raster);
    MemoryDecoder* dec;
    u_long length;

    // MR: untuned (HylaFAX's fixed schedule) and tuned, with ECM (no line minimum).
    u_char* fixed = convert(raster, DF_2DMR, false, false, 0, &dec, length);
    u_long fixedLength = length;
    CHECK(dec->getRows() == ROWS, "%s: fixed MR converted %u rows", gold.name, (u_int) dec->getRows());
    CHECK(decodesTo(fixed, fixedLength, raster), "%s: fixed MR does not decode to the page", gold.name);
    delete dec;
    u_char* tuned = convert(raster, DF_2DMR, true, false, 0, &dec, length);
    CHECK(decodesTo(tuned, length, raster), "%s: tuned MR does not decode to the page", gold.name);
    CHECK(dec->getTunedBytes() == length, "%s: tuned MR model %lu bytes, encoded %lu", gold.name,
	dec->getTunedBytes(), length);
    CHECK(dec->getPlainBytes() == fixedLength, "%s: fixed MR model %lu bytes, encoded %lu", gold.name,
	dec->getPlainBytes(), fixedLength);
    CHECK(length <= fixedLength, "%s: tuned MR %lu bytes, larger than fixed %lu", gold.name, length, fixedLength);
    CHECK(strlen(dec->getRasterDigest()) == 64, "%s: no raster digest", gold.name);
    u_long mrTuned = length, mrFixed = fixedLength;
    uint32_t oneD = dec->getOneDRows();
    delete dec;
    delete[] fixed;
    delete[] tuned;

    // MR without ECM: the model with the line minimum.
    tuned = convert(raster, DF_2DMR, true, false, PADDED, &dec, length);
    CHECK(decodesTo(tuned, length, raster), "%s: padded tuned MR does not decode to the page", gold.name);
    CHECK(dec->getTunedBytes() <= dec->getPlainBytes(), "%s: padded tuned MR larger than fixed", gold.name);
    u_long mrTunedPadded = dec->getTunedBytes(), mrFixedPadded = dec->getPlainBytes();
    delete dec;
    delete[] tuned;

    // JBIG: plain, then tuned against every candidate encoded on its own.
    u_char* plain = convert(raster, DF_JBIG, false, false, 0, &dec, length);
    fxStackBuffer bie;
    CHECK(length == jbigBytes(raster, 0, 0, bie) && memcmp(plain, (const u_char*) bie, length) == 0,
	"%s: plain JBIG is not options 0, MX 0", gold.name);
    CHECK(jbigDecodesTo(plain, length, raster), "%s: plain JBIG does not decode to the page", gold.name);
    u_long jbigPlain = length;
    delete dec;
    delete[] plain;
    u_char* best = convert(raster, DF_JBIG, false, true, 0, &dec, length);
    CHECK(jbigDecodesTo(best, length, raster), "%s: tuned JBIG does not decode to the page", gold.name);
    u_long smallest = (u_long) -1;
    int options = -1, mx = -1;
    for (u_int i = 0; i < LosslessTuning::JBIG_CANDIDATES; i++) {
	u_long bytes = jbigBytes(raster, LosslessTuning::jbigOptions(i), LosslessTuning::jbigMX(i), bie);
	CHECK(jbigDecodesTo((const u_char*) bie, bytes, raster), "%s: JBIG options %d MX %d does not decode",
	    gold.name, LosslessTuning::jbigOptions(i), LosslessTuning::jbigMX(i));
	if (bytes < smallest) {
	    smallest = bytes;
	    options = LosslessTuning::jbigOptions(i);
	    mx = LosslessTuning::jbigMX(i);
	}
    }
    CHECK(length == smallest && dec->getTunedOptions() == options && dec->getTunedMX() == mx,
	"%s: tuned JBIG options %d MX %d %lu bytes; smallest is options %d MX %d %lu bytes", gold.name,
	dec->getTunedOptions(), dec->getTunedMX(), length, options, mx, smallest);
    CHECK(dec->getPlainBytes() == jbigPlain, "%s: plain JBIG bytes differ when tuning", gold.name);
    u_long jbigTuned = length;
    delete dec;
    delete[] best;

    printf("%-8s MR fixed %lu tuned %lu (%u one-dimensional rows); without ECM fixed %lu tuned %lu; "
	"JBIG plain %lu tuned %lu (options %d, MX %d)\n", gold.name, mrFixed, mrTuned, (u_int) oneD,
	mrFixedPadded, mrTunedPadded, jbigPlain, jbigTuned, options, mx);
    if (gold.mrFixed) {
	CHECK(mrFixed == gold.mrFixed && mrTuned == gold.mrTuned && mrFixedPadded == gold.mrFixedPadded
	    && mrTunedPadded == gold.mrTunedPadded && jbigPlain == gold.jbigPlain && jbigTuned == gold.jbigTuned
	    && options == gold.options && mx == gold.mx, "%s: differs from the golden numbers", gold.name);
    } else
	CHECK(false, "%s: golden numbers are not filled in", gold.name);
    delete[] raster;
}

int
main()
{
    testSchedule();
    for (u_int page = 0; page < 4; page++)
	testPage(page);
    if (failures) {
	printf("lossless tuning: %d failures\n", failures);
	return 1;
    }
    printf("lossless tuning: all checks passed\n");
    return 0;
}
