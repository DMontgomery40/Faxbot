/*
 * Faxbot: what its changes add to spandsp 0.0.6, in one place: the two T.38 gateway steps (patches 0002 and
 * 0003), the far-end frame capture and Internet Aware Fax steps of patch 0004, patch 0005's subaddress check, and
 * patch 0008's error-correction frame size, error-shape record and T.38 protection by packet category.
 *
 * res_fax_spandsp.c includes this file (the image build copies it next to that file), and so do the replay
 * proofs asterisk/tests/t38_gateway_replay.c and t38_terminal_replay.c, so the proofs run exactly the steps
 * the patches run. Include it after <spandsp.h>, with SPANDSP_EXPOSE_INTERNAL_STRUCTURES defined.
 *
 * The steps read and change spandsp's internal gateway and terminal state, and FAXBOT_SPANDSP_FLAG_INDICATOR
 * is copied from spandsp 0.0.6's private t38_gateway.c. They were checked against the spandsp 0.0.6 in the
 * Debian package snapshot the images pin (release date 20110122), so any other spandsp stops the build.
 */
#ifndef FAXBOT_T38_GATEWAY_H
#define FAXBOT_T38_GATEWAY_H

#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <spandsp/version.h>

#if !defined(SPANDSP_RELEASE_DATE) || SPANDSP_RELEASE_DATE != 20110122
#error "Faxbot's T.38 gateway changes (asterisk/patches/0002, 0003) were checked against spandsp 0.0.6 (20110122) only; check them against this spandsp before building"
#endif

#define FAXBOT_SPANDSP_FLAG_INDICATOR 0x100	/* FLAG_INDICATOR in spandsp 0.0.6's t38_gateway.c */

/*!
 * \brief Patch 0002: end a V.21 preamble that carried no HDLC frame.
 *
 * When the next item for the fax machine is an indicator and no frame is loaded, the preamble was
 * empty: end the transmitter, cutting its remaining timed flags so it does not fall behind the T.38
 * stream. In every other state this changes nothing.
 *
 * \retval 1 an empty preamble's flags were cut (the far end sent a preamble with no frame)
 * \retval 0 otherwise
 */
static inline int faxbot_gateway_end_empty_preamble(t38_gateway_state_t *gw)
{
	t38_gateway_hdlc_state_t *queue = &gw->core.hdlc_to_modem;
	hdlc_tx_state_t *hdlc = &gw->audio.modems.hdlc_tx;
	int cut = 0;

	if (queue->in == queue->out || !(queue->buf[queue->out].contents & FAXBOT_SPANDSP_FLAG_INDICATOR) || hdlc->len) {
		return 0;
	}
	if (hdlc->flag_octets > 2) {
		hdlc->flag_octets = 2;
		cut = 1;
	}
	hdlc_tx_frame(hdlc, NULL, 0);
	return cut;
}

/*!
 * \brief Patch 0003: end a fast modem signal the far end left open.
 *
 * When the fast modem is sending the far end's data and the far end has moved on (an indicator is
 * queued behind it and no non-ECM data is arriving), the data has ended: mark it finished, as a signal
 * end does. A modem still training or already shutting down, and data that ended with its signal end,
 * are left alone.
 *
 * \retval 1 the data was marked finished
 * \retval 0 otherwise
 */
static inline int faxbot_gateway_end_empty_training(t38_gateway_state_t *gw)
{
	fax_modems_state_t *modems = &gw->audio.modems;
	t38_gateway_hdlc_state_t *queue = &gw->core.hdlc_to_modem;
	t38_non_ecm_buffer_state_t *buffer = &gw->core.non_ecm_to_modem;
	get_bit_func_t get_bit = NULL;

	if (modems->tx_handler == (span_tx_handler_t *) &v29_tx) {
		get_bit = modems->fast_modems.v29_tx.current_get_bit;
	} else if (modems->tx_handler == (span_tx_handler_t *) &v17_tx) {
		get_bit = modems->fast_modems.v17_tx.current_get_bit;
	} else if (modems->tx_handler == (span_tx_handler_t *) &v27ter_tx) {
		get_bit = modems->fast_modems.v27ter_tx.current_get_bit;
	}
	if (get_bit != t38_non_ecm_buffer_get_bit || buffer->data_finished
		|| queue->in == queue->out || !(queue->buf[queue->out].contents & FAXBOT_SPANDSP_FLAG_INDICATOR)
		|| gw->t38x.current_rx_field_class == T38_FIELD_CLASS_NON_ECM) {
		return 0;
	}
	t38_non_ecm_buffer_push(buffer);
	return 1;
}

/*
 * Patch 0004, part 1: what the far end's fax machine said, captured as T.30 frames pass (spandsp's real-time
 * frame handler, both directions) and kept, bounded, for the channel variables set when the session ends.
 *
 * Frames are as spandsp hands them over: address (0xFF), control, FCF (spandsp's bit order, t30_fcf.h), then
 * the FIF. FCFs here are spandsp 0.0.6's: DIS 0x80, DTC 0x81, DCS 0x82, NSF 0x20, CSA 0x24, TSA 0x62, SUB 0xC2,
 * CFR 0x84, FTT 0x44; the low bit of every FCF but DIS/DTC's is the X bit (whether a DIS was received), so it is
 * masked off. Each kept frame is cut at FAXBOT_FRAME_MAX octets, so a long NSF never grows a variable or a log line.
 * A SUB fits: its FIF is at most 20 digits (T.30 5.3.6.2.4), 23 octets in all. The internet address frames (CSA,
 * TSA) are kept whole up to FAXBOT_ADDRESS_MAX: their FIF is a sequence octet, a type octet, a length octet and at
 * most 77 address octets (T.30 5.3.6.2.12, as spandsp's own decode_url_msg checks), so with the address, control
 * and FCF octets 83 in all. An SSL Fax address ("ssl://<passcode>@<address>:<port>") is often longer than 32.
 * Nothing logs these two frames; the NOTICE line names only the DIS.
 */
#define FAXBOT_FRAME_MAX 32
#define FAXBOT_ADDRESS_MAX (2 + 4 + 77)
#define FAXBOT_RATES_MAX 16

typedef struct {
	uint8_t frame[FAXBOT_ADDRESS_MAX];
	int len;
} faxbot_frame_t;

typedef struct {
	faxbot_frame_t dis;		/* the far end's last DIS (or DTC) */
	faxbot_frame_t dcs_first;	/* the session's first DCS, whichever side sent it */
	faxbot_frame_t dcs_last;	/* its last DCS: the settings the pages went with */
	faxbot_frame_t csa;		/* the far end's internet address (CSA), when it sent one */
	faxbot_frame_t tsa;		/* the sender's internet address (TSA), when it sent one */
	faxbot_frame_t sub;		/* the subaddress (SUB) the sender gave */
	faxbot_frame_t nsf;		/* the far end's non-standard facilities (NSF), first octets only */
	uint8_t rates[FAXBOT_RATES_MAX];	/* each DCS's speed code (FIF octet 2, bits 11-14), in order */
	int rates_len;
	int dcs_sent;			/* 1 when this side sent the DCS: it sent the fax */
	unsigned int dcs, cfr, ftt;	/* DCS frames (trainings), confirmations and failures to train */
} faxbot_frames_t;

static inline void faxbot_frame_keep_at_most(faxbot_frame_t *kept, const uint8_t *msg, int len, int most)
{
	kept->len = len < most ? len : most;
	memcpy(kept->frame, msg, kept->len);
}

static inline void faxbot_frame_keep(faxbot_frame_t *kept, const uint8_t *msg, int len)
{
	faxbot_frame_keep_at_most(kept, msg, len, FAXBOT_FRAME_MAX);
}

/*! \brief Patch 0004: keep one T.30 frame; \p received is 1 for a frame from the far end. */
static inline void faxbot_frames_record(faxbot_frames_t *frames, int received, const uint8_t *msg, int len)
{
	uint8_t fcf;

	if (!frames || !msg || len < 3) {
		return;
	}
	fcf = msg[2];
	if (received && (fcf == 0x80 || fcf == 0x81)) {
		faxbot_frame_keep(&frames->dis, msg, len);
		return;
	}
	switch (fcf & 0xFE) {
	case 0x82:	/* DCS */
		frames->dcs++;
		if (!frames->dcs_first.len) {
			faxbot_frame_keep(&frames->dcs_first, msg, len);
		}
		faxbot_frame_keep(&frames->dcs_last, msg, len);
		frames->dcs_sent = !received;
		if (len > 4 && frames->rates_len < FAXBOT_RATES_MAX) {
			frames->rates[frames->rates_len++] = msg[4] & 0x3C;
		}
		break;
	case 0x84:	/* CFR */
		frames->cfr++;
		break;
	case 0x44:	/* FTT */
		frames->ftt++;
		break;
	case 0x24:	/* CSA: whole, up to the longest address T.30 allows */
		if (received) {
			faxbot_frame_keep_at_most(&frames->csa, msg, len, FAXBOT_ADDRESS_MAX);
		}
		break;
	case 0x62:	/* TSA: the same */
		if (received) {
			faxbot_frame_keep_at_most(&frames->tsa, msg, len, FAXBOT_ADDRESS_MAX);
		}
		break;
	case 0xC2:	/* SUB */
		if (received) {
			faxbot_frame_keep(&frames->sub, msg, len);
		}
		break;
	case 0x20:	/* NSF */
		if (received && fcf == 0x20 && !frames->nsf.len) {
			faxbot_frame_keep(&frames->nsf, msg, len);
		}
		break;
	}
}

/*! \brief A DCS speed code (FIF octet 2 & 0x3C, spandsp's fallback table) in bit/s; 0 when unknown. */
static inline int faxbot_dcs_rate(uint8_t code)
{
	switch (code) {
	case 0x20: return 14400;	/* V.17 */
	case 0x28: return 12000;	/* V.17 */
	case 0x24: return 9600;		/* V.17 */
	case 0x04: return 9600;		/* V.29 */
	case 0x2C: return 7200;		/* V.17 */
	case 0x0C: return 7200;		/* V.29 */
	case 0x08: return 4800;		/* V.27ter */
	case 0x00: return 2400;		/* V.27ter */
	}
	return 0;
}

/*! \brief Lower-case hex of \p len octets into \p out (at least 2 * FAXBOT_ADDRESS_MAX + 1 bytes); empty for none. */
static inline void faxbot_hex(const uint8_t *buf, int len, char *out)
{
	static const char digits[] = "0123456789abcdef";
	int i;

	for (i = 0; i < len && i < FAXBOT_ADDRESS_MAX; i++) {
		out[2 * i] = digits[buf[i] >> 4];
		out[2 * i + 1] = digits[buf[i] & 0x0F];
	}
	out[2 * i] = '\0';
}

/*! \brief The speed codes of every DCS as hex, separated by dots (at most FAXBOT_RATES_MAX entries). */
static inline void faxbot_rates_text(const faxbot_frames_t *frames, char *out, size_t size)
{
	size_t used = 0;
	int i;

	out[0] = '\0';
	for (i = 0; i < frames->rates_len && used + 4 < size; i++) {
		used += snprintf(out + used, size - used, "%s%02x", i ? "." : "", frames->rates[i]);
	}
}

/*
 * Patch 0004, part 2: T.38 Internet Aware Fax (IAF) for a call Faxbot marked FAXBOT_IAF (engine_frames.py decides,
 * from an enrolled partner or a fax server you approved; never a carrier call by guesswork): "peer" is another
 * Faxbot, "endpoint" an approved IAF fax server (such as an SR140). Both get spandsp's IAF mode (T.38, continuous
 * flow, no fill bits). The training check stays (no NO_TCF): in spandsp 0.0.6 a receiver with NO_TCF never starts
 * receiving the page (start_receiving_document waits for a check only when it is off, and starts nothing when it
 * is on), and the pair replay in asterisk/tests shows such a call failing. The check is sent ahead like the rest.
 *
 * Faster than real time: spandsp's T.38 terminal paces its data at the modem rate. Its unpaced mode
 * (T38_TERMINAL_OPTION_NO_PACING) is not used, because spandsp 0.0.6 then puts up to 300 octets in each T.38
 * packet and never applies the far end's datagram size, while Asterisk cuts any packet larger than the far end
 * accepts (ast_udptl_write; 98 octets on Faxbot's trunks). Instead faxbot_iaf_send_ahead sends the next paced
 * chunk now: the T.38 front end's clock jumps to the moment that chunk is due, and the T.30 timers do not move
 * (t38_terminal_send_timeout with 0 samples), so every protocol timeout keeps its real length and every packet
 * keeps its paced size. It never jumps while the front end is timing a receive.
 */
#define FAXBOT_IAF_PEER 1
#define FAXBOT_IAF_ENDPOINT 2
/* Chunks sent ahead after each 20 ms timer tick; each is one paced chunk (54 octets at 14,400 bit/s). */
#define FAXBOT_IAF_AHEAD 9

static inline int faxbot_iaf_kind(const char *value)
{
	if (!value) {
		return 0;
	}
	if (!strcmp(value, "peer")) {
		return FAXBOT_IAF_PEER;
	}
	if (!strcmp(value, "endpoint")) {
		return FAXBOT_IAF_ENDPOINT;
	}
	return 0;
}

/*! \brief The T.30 IAF mode bits for \p kind (0 for none). */
static inline int faxbot_iaf_t30_mode(int kind)
{
	switch (kind) {
	case FAXBOT_IAF_PEER:
	case FAXBOT_IAF_ENDPOINT:
		return T30_IAF_MODE_T38 | T30_IAF_MODE_CONTINUOUS_FLOW | T30_IAF_MODE_NO_FILL_BITS;
	}
	return 0;
}

/*!
 * \brief Send the next paced T.38 chunk now instead of when it is due.
 * \retval 1 a chunk was due later and has been sent
 * \retval 0 nothing to send ahead (nothing timed, already due, or a receive is being timed)
 */
static inline int faxbot_iaf_send_ahead(t38_terminal_state_t *terminal)
{
	t38_terminal_front_end_state_t *fe = &terminal->t38_fe;

	if (fe->timed_step == 0 || !fe->us_per_tx_chunk || fe->timeout_rx_samples || fe->samples >= fe->next_tx_samples) {
		return 0;
	}
	fe->samples = fe->next_tx_samples;
	t38_terminal_send_timeout(terminal, 0);
	return 1;
}

/*
 * Patch 0005: the T.33 subaddress (SUB) a fax Faxbot sends asks for, from the channel variable FAXBOT_TX_SUB
 * (ami.py sets it for a notice fax's notice ID or a subaddress your sending rules chose). spandsp 0.0.6 sends SUB
 * with its DCS only when the far end's DIS sets the subaddressing bit (DIS bit 49), so Faxbot records a subaddress
 * as requested, and as carried only when that bit was set (engine_frames.py); the replay proof in asterisk/tests
 * shows both. A SUB's field holds at most 20 characters (T.30 5.3.6.2.4).
 *
 * The other side: spandsp 0.0.6's own DIS leaves bit 49 clear (its default T.30 features have no subaddressing),
 * so a sending machine that honours the DIS never sent the built-in engine a SUB, and receiving rules by subaddress
 * and notice pairing by SUB could not work there. faxbot_receive_subaddress sets it for a fax Faxbot receives.
 */
#define FAXBOT_SUB_MAX 20

/*! \brief Patch 0005: a fax Faxbot receives says, in its DIS, that it takes a subaddress (T.30 DIS bit 49). */
static inline void faxbot_receive_subaddress(t30_state_t *t30)
{
	t30_set_supported_t30_features(t30, t30->supported_t30_features | T30_SUPPORT_SUB_ADDRESSING);
}

/*!
 * \brief The subaddress in \p value as Faxbot sends it: digits and +, # and * (spaces dropped), at most
 * FAXBOT_SUB_MAX, into \p out (at least FAXBOT_SUB_MAX + 1 bytes). Any other character, or a longer value, gives an
 * empty \p out: nothing is sent rather than part of an address. The same characters receiving_rules.py accepts.
 */
static inline void faxbot_sub_clean(const char *value, char *out, size_t size)
{
	size_t used = 0;

	if (!out || !size) {
		return;
	}
	out[0] = '\0';
	if (!value) {
		return;
	}
	for (; *value; value++) {
		if (*value == ' ') {
			continue;
		}
		if (!((*value >= '0' && *value <= '9') || *value == '+' || *value == '#' || *value == '*')
			|| used >= FAXBOT_SUB_MAX || used + 1 >= size) {
			out[0] = '\0';
			return;
		}
		out[used++] = *value;
	}
	out[used] = '\0';
}

/*
 * Patch 0006 (builder CA): the most compact coding a sent fax may use, from FAXBOT_COMPRESSION ("mh", "mr" or
 * "mmr"), measured on the fax's own pages (api/app/pages/coding.py). It is a ceiling on what the sender offers:
 * spandsp still takes the best coding the receiving machine also has, and T.6 (MMR) only with error correction
 * (t30.c, process_rx_dis_dtc), so a machine never gets a coding it lacks. Error correction is not changed.
 */

/*! \brief The T.30 compressions a sent fax offers for \p coding; 0 (keep Asterisk's own set) for anything else. */
static inline int faxbot_compressions(const char *coding)
{
	if (!coding) {
		return 0;
	}
	if (!strcmp(coding, "mh")) {
		return T30_SUPPORT_T4_1D_COMPRESSION;
	}
	if (!strcmp(coding, "mr")) {
		return T30_SUPPORT_T4_1D_COMPRESSION | T30_SUPPORT_T4_2D_COMPRESSION;
	}
	if (!strcmp(coding, "mmr")) {
		return T30_SUPPORT_T4_1D_COMPRESSION | T30_SUPPORT_T4_2D_COMPRESSION | T30_SUPPORT_T6_COMPRESSION;
	}
	return 0;
}

/*
 * Patch 0007 (builder CG): the station check and the cap on waiting for a fax answer, on a fax Faxbot sends.
 *
 * Station check (research N5). In phase B the called machine sends its CSI (its station identifier, usually its
 * own number) before the DIS, and spandsp keeps it before it calls the application's phase B handler on that DIS
 * (t30.c 0.0.6: the CSI is stored on arrival, then process_rx_dis_dtc calls the handler; any answer but
 * T30_ERR_OK sends DCN before any DCS, training or page). FAXBOT_CSI_EXPECT lists, as digits separated by dots,
 * the numbers this call may answer as: the number dialled and the stations it showed on earlier successful calls.
 * A CSI is a hint, never proof, so it is used only in the negative: it contradicts them when it carries a full
 * number (at least FAXBOT_CSI_MIN_DIGITS digits) that is none of them. A blank or short CSI is no signal. Two
 * numbers are the same when, after leading zeros, the shorter is the end of the longer (a national CSI such as
 * 020 7946 0000 against the number dialled in international form, 442079460000). api/app/routing/stations.py's
 * check() follows the same rules, with the same examples.
 *
 * T0 cap (research N9's option). spandsp 0.0.6 waits T0 = 60 s for the first fax message (t30.c DEFAULT_TIMER_T0;
 * ITU-T T.30 5.4.3.1: T0 is 60 +-5 s, counted from the end of dialling, so it includes ringing). Asterisk starts
 * SendFAX only once the call is answered, and from then on T.30 expects the two machines to identify each other
 * within T1 = 35 +-5 s (T.30 5.4.3.1, timer T1). A person who stays on the line therefore holds the call past
 * 60 s, which bills two units on 60/60 billing. FAXBOT_T0_MS (Faxbot sets 50000 on trunk calls billed by the
 * minute in steps of 60 s or more) ends a send session that has not reached phase B that long after the answer:
 * 10 s more than T1's longest value, and before the first 60 s billing step ends.
 */
#define FAXBOT_CSI_MIN_DIGITS 7
#define FAXBOT_CSI_DIGITS_MAX 20
#define FAXBOT_CSI_EXPECT_MAX 200
#define FAXBOT_CSI_NO_SIGNAL 0
#define FAXBOT_CSI_MATCHES 1
#define FAXBOT_CSI_DIFFERS 2
/*! The shortest and longest cap Faxbot accepts, in milliseconds: never under T.30's longest T1, never over T0. */
#define FAXBOT_T0_MIN_MS 40000
#define FAXBOT_T0_MAX_MS 60000

/*! \brief The digits of \p text into \p out (at least FAXBOT_CSI_DIGITS_MAX + 1 bytes); returns how many. */
static inline int faxbot_digits(const char *text, char *out, size_t size)
{
	int used = 0;

	if (!out || size < FAXBOT_CSI_DIGITS_MAX + 1) {
		return 0;
	}
	out[0] = '\0';
	if (!text) {
		return 0;
	}
	for (; *text && used < FAXBOT_CSI_DIGITS_MAX; text++) {
		if (*text >= '0' && *text <= '9') {
			out[used++] = *text;
		}
	}
	out[used] = '\0';
	return used;
}

/*!
 * \brief Whether digit strings \p a and \p b (\p alen and \p blen long) name the same number: after leading zeros
 * (a national trunk prefix such as the UK's 0, or an international 00) the shorter is the end of the longer.
 */
static inline int faxbot_same_number(const char *a, int alen, const char *b, int blen)
{
	int shorter;

	for (; alen > 0 && *a == '0'; a++, alen--) {
	}
	for (; blen > 0 && *b == '0'; b++, blen--) {
	}
	shorter = alen < blen ? alen : blen;
	if (shorter < FAXBOT_CSI_MIN_DIGITS) {
		return 0;
	}
	return !memcmp(a + alen - shorter, b + blen - shorter, shorter);
}

/*!
 * \brief FAXBOT_CSI_NO_SIGNAL, FAXBOT_CSI_MATCHES or FAXBOT_CSI_DIFFERS for the far end's \p csi against
 * \p expected (digits; any other character separates two numbers; Faxbot sends dots, since a comma would end
 * the Originate variable).
 */
static inline int faxbot_csi_check(const char *csi, const char *expected)
{
	char far[FAXBOT_CSI_DIGITS_MAX + 1];
	char one[FAXBOT_CSI_DIGITS_MAX + 1];
	int farlen = faxbot_digits(csi, far, sizeof(far));
	int len = 0;
	int known = 0;

	if (farlen < FAXBOT_CSI_MIN_DIGITS || !expected) {
		return FAXBOT_CSI_NO_SIGNAL;
	}
	for (;; expected++) {
		if (*expected >= '0' && *expected <= '9') {
			if (len < FAXBOT_CSI_DIGITS_MAX) {
				one[len++] = *expected;
			}
			continue;
		}
		if (len >= FAXBOT_CSI_MIN_DIGITS) {
			known = 1;
			if (faxbot_same_number(far, farlen, one, len)) {
				return FAXBOT_CSI_MATCHES;
			}
		}
		len = 0;
		if (*expected == '\0') {
			break;
		}
	}
	return known ? FAXBOT_CSI_DIFFERS : FAXBOT_CSI_NO_SIGNAL;
}

/*! \brief The cap in milliseconds from FAXBOT_T0_MS's \p value, or 0 (spandsp's own T0) when it is not one. */
static inline int faxbot_t0_ms(const char *value)
{
	long ms;
	char *end = NULL;

	if (!value || !*value) {
		return 0;
	}
	ms = strtol(value, &end, 10);
	if (!end || *end || ms < FAXBOT_T0_MIN_MS || ms > FAXBOT_T0_MAX_MS) {
		return 0;
	}
	return (int) ms;
}

/*!
 * \brief When the call was answered, from FAXBOT_ANSWERED's \p value (epoch seconds, set by the send dialplan before
 * SendFAX), or 0 when it is not a time at or before \p now_s. The cap counts from the answer: res_fax starts a new
 * session after a T.38 attempt the far end refused, so a session's own start would let the call run longer.
 */
static inline long long faxbot_answered_at(const char *value, long long now_s)
{
	long long at;
	char *end = NULL;

	if (!value || !*value) {
		return 0;
	}
	at = strtoll(value, &end, 10);
	if (!end || *end || at <= 0 || at > now_s) {
		return 0;
	}
	return at;
}

/*! \brief Whether a send session \p elapsed_ms old that has not reached phase B has reached the cap \p cap_ms. */
static inline int faxbot_t0_reached(int cap_ms, int phase_b, long long elapsed_ms)
{
	return cap_ms > 0 && !phase_b && elapsed_ms >= cap_ms;
}

/*
 * Patch 0008 (builder ENG, brief 98): error correction's frame size and the shape of its errors, and T.38 protection
 * by packet category.
 *
 * Frame size. With error correction (ITU-T T.30 Annex A) the sender chooses 256- or 64-octet frames in its DCS (bit
 * 28: set for 64; spandsp's own decoder, t30_logging.c, reads it so). spandsp 0.0.6 always sends 256:
 * process_rx_dis_dtc sets octets_per_ecm_frame to 256 and build_dcs never sets bit 28. With FAXBOT_ECM_FRAME=64
 * (engine_learning.py asks for it for a number whose errors smaller frames repair more cheaply),
 * faxbot_ecm_small_frames sets both as the sender's DCS goes out: spandsp's real-time frame handler sees the DCS
 * (t30->dcs_frame, after prune_dcs) before it is sent, so the DCS and the frames agree. A DCS without error
 * correction is left alone. spandsp's own receiver keeps frames by their length, so it takes either size.
 *
 * Error shape. faxbot_ecm_record keeps, from the frames the handler sees in both directions: partial page signals
 * (PPS), blocks (each page and block number once) and the frames in them, partial page requests (PPR) and the frames
 * each asked for again, the runs of neighbouring frames a block's first request asked for (a burst of line errors
 * spoils neighbours; scattered errors spoil single frames), the longest such run, continue-to-correct requests (CTC),
 * and the milliseconds from each PPS to the next frame of image data: a block edge (PPS-NULL answered with MCF) or a
 * round of resent frames (a PPS answered with PPR). The milliseconds are the caller's clock: Asterisk's wall clock,
 * or the replay proof's simulated one. A PPR's map holds frame 8i+j in bit j of octet i (spandsp's process_rx_ppr).
 */
#define FAXBOT_DCS_BIT_ECM 27
#define FAXBOT_DCS_BIT_64_OCTETS 28
#define FAXBOT_FCF_FCD 0x06
#define FAXBOT_ECM_EDGE 1
#define FAXBOT_ECM_ROUND 2

typedef struct {
	int frame_octets;		/* 64 or 256, from the session's last DCS with error correction; 0 without */
	int small_frames;		/* 1 when Faxbot asked for 64-octet frames on its own DCS */
	unsigned int pps, blocks, frames, ppr, bad, runs, longest, resent, ctc;
	unsigned int pages;		/* pages with error correction: page numbers the PPS frames named */
	unsigned int edges, rounds;	/* timed block edges and rounds of resent frames */
	long long edge_ms, round_ms;	/* their total milliseconds */
	int page, block;		/* the last PPS's page and block numbers; -1 before the first */
	int block_frames;		/* frames in the current block, from its first PPS */
	int pps_from;			/* who sent the PPS, in spandsp's direction (1: received) */
	int waiting;			/* 1 after a PPS until its answer */
	int first_answer;		/* 1 until the current block's first answer */
	int pps_null;			/* the last PPS was PPS-NULL (more blocks follow on the page) */
	int pending;			/* FAXBOT_ECM_EDGE or FAXBOT_ECM_ROUND: the next image frame ends a turnaround */
	long long pps_at;		/* when the last PPS went, in the caller's milliseconds */
} faxbot_ecm_t;

static inline void faxbot_ecm_init(faxbot_ecm_t *ecm)
{
	memset(ecm, 0, sizeof(*ecm));
	ecm->page = -1;
	ecm->block = -1;
}

/*! \brief Whether T.30 control bit \p bit (1-based, as T.30 Table 2 numbers it) is set in a DIS/DTC/DCS frame. */
static inline int faxbot_ctrl_bit(const uint8_t *msg, int len, int bit)
{
	int octet = 3 + (bit - 1) / 8;

	return octet < len && ((msg[octet] >> ((bit - 1) & 7)) & 1);
}

/*!
 * \brief Patch 0008: fill the current page's first partial page again at the frame size now set, as spandsp 0.0.6's
 * own get_partial_ecm_page (t30.c, static) does: up to 256 frames of address, control, FCD, the frame number and the
 * image data, the last one padded to a whole frame. The page image is coded in memory, so it is rewound first
 * (t4_tx_restart_page).
 */
static inline void faxbot_ecm_refill(t30_state_t *t30)
{
	int i, len;

	t4_tx_restart_page(&t30->t4.tx);
	t30->ppr_count = 0;
	t30->ecm_progress = 0;
	t30->ecm_block = 0;
	for (i = 3; i < 32 + 3; i++) {
		t30->ecm_frame_map[i] = 0xFF;
	}
	for (i = 0; i < 256; i++) {
		t30->ecm_len[i] = -1;
		t30->ecm_data[i][0] = 0xFF;	/* ADDRESS_FIELD */
		t30->ecm_data[i][1] = 0x03;	/* CONTROL_FIELD_NON_FINAL_FRAME */
		t30->ecm_data[i][2] = FAXBOT_FCF_FCD;
		t30->ecm_data[i][3] = (uint8_t) i;
		if ((len = t4_tx_get_chunk(&t30->t4.tx, &t30->ecm_data[i][4], t30->octets_per_ecm_frame))
			< t30->octets_per_ecm_frame) {
			if (len > 0) {
				memset(&t30->ecm_data[i][4 + len], 0, t30->octets_per_ecm_frame - len);
				t30->ecm_len[i++] = (int16_t) (t30->octets_per_ecm_frame + 4);
			}
			t30->ecm_frames = i;
			t30->ecm_at_page_end = TRUE;
			return;
		}
		t30->ecm_len[i] = (int16_t) (4 + len);
	}
	t30->ecm_frames = 256;
	t30->ecm_at_page_end = ((t4_tx_check_bit(&t30->t4.tx) & 2) != 0);
}

/*!
 * \brief Patch 0008: 64-octet error-correction frames on the DCS this side sends now.
 *
 * spandsp fills the page's first partial page when it starts sending the document, before it builds the DCS, at 256
 * octets a frame; so the first time a DCS asks for 64, that partial page is filled again at 64 (faxbot_ecm_refill).
 * A later DCS (after a failed training) finds the frames already 64 octets and changes nothing more.
 * \retval 1 the DCS (and the frames that follow it) now use 64-octet frames
 * \retval 0 not this side's DCS, or no error correction on it: nothing changed
 */
static inline int faxbot_ecm_small_frames(t30_state_t *t30, int received, const uint8_t *msg, int len)
{
	if (received || !t30 || msg != t30->dcs_frame || len < 7 || (msg[2] & 0xFE) != 0x82
		|| !faxbot_ctrl_bit(msg, len, FAXBOT_DCS_BIT_ECM)) {
		return 0;
	}
	t30->dcs_frame[3 + (FAXBOT_DCS_BIT_64_OCTETS - 1) / 8] |= (uint8_t) (1 << ((FAXBOT_DCS_BIT_64_OCTETS - 1) & 7));
	if (t30->octets_per_ecm_frame != 64) {
		t30->octets_per_ecm_frame = 64;
		faxbot_ecm_refill(t30);
	}
	return 1;
}

/*! \brief Patch 0008: keep what one T.30 frame says about error correction; \p received is 1 for the far end's. */
static inline void faxbot_ecm_record(faxbot_ecm_t *ecm, int received, const uint8_t *msg, int len, long long now_ms)
{
	int i, page, block, count, bad = 0, run = 0, runs = 0, longest = 0;

	if (!ecm || !msg || len < 3) {
		return;
	}
	if (msg[2] == FAXBOT_FCF_FCD) {
		if (ecm->pending && received == ecm->pps_from) {
			if (ecm->pending == FAXBOT_ECM_EDGE) {
				ecm->edges++;
				ecm->edge_ms += now_ms - ecm->pps_at;
			} else {
				ecm->rounds++;
				ecm->round_ms += now_ms - ecm->pps_at;
			}
			ecm->pending = 0;
		}
		return;
	}
	switch (msg[2] & 0xFE) {
	case 0x82:	/* DCS */
		ecm->frame_octets = !faxbot_ctrl_bit(msg, len, FAXBOT_DCS_BIT_ECM) ? 0
			: faxbot_ctrl_bit(msg, len, FAXBOT_DCS_BIT_64_OCTETS) ? 64 : 256;
		break;
	case 0xBE:	/* PPS: FCF2, page, block, frames in this burst less one */
		if (len < 7) {
			break;
		}
		page = msg[4];
		block = msg[5];
		count = msg[6] + 1;
		ecm->pps++;
		if (page != ecm->page) {
			ecm->pages++;
		}
		if (page != ecm->page || block != ecm->block) {
			ecm->blocks++;
			ecm->frames += count;
			ecm->block_frames = count;
			ecm->page = page;
			ecm->block = block;
			ecm->first_answer = 1;
		}
		ecm->pps_from = received;
		ecm->waiting = 1;
		ecm->pps_at = now_ms;
		ecm->pps_null = (msg[3] & 0xFE) == 0x00;
		ecm->pending = 0;
		break;
	case 0xBC:	/* PPR: the frames to send again, a 256-bit map */
		if (!ecm->waiting || received == ecm->pps_from || len < 3 + 32) {
			break;
		}
		ecm->ppr++;
		for (i = 0; i < ecm->block_frames && i < 256; i++) {
			if ((msg[3 + i / 8] >> (i & 7)) & 1) {
				bad++;
				if (!run) {
					runs++;
				}
				if (++run > longest) {
					longest = run;
				}
			} else {
				run = 0;
			}
		}
		ecm->resent += bad;
		if (ecm->first_answer) {
			ecm->bad += bad;
			ecm->runs += runs;
			if ((unsigned int) longest > ecm->longest) {
				ecm->longest = longest;
			}
		}
		ecm->first_answer = 0;
		ecm->waiting = 0;
		ecm->pending = FAXBOT_ECM_ROUND;
		break;
	case 0x8C:	/* MCF: the block arrived whole */
		if (!ecm->waiting || received == ecm->pps_from) {
			break;
		}
		ecm->waiting = 0;
		ecm->first_answer = 0;
		ecm->pending = ecm->pps_null ? FAXBOT_ECM_EDGE : 0;
		break;
	case 0x12:	/* CTC: after four requests for one block, the sender asks to go on (and may train down) */
		if (received == ecm->pps_from) {
			ecm->ctc++;
		}
		break;
	}
}

/*! \brief Patch 0008: the record as FAXBOT_ECM: "frame.pps.blocks.frames.ppr.bad.runs.longest.resent.ctc.edges.
 * edge_ms.rounds.round_ms.small.pages", whole numbers separated by dots (no comma: the value goes in a manager
 * event; engine_ecm.py reads it). */
static inline void faxbot_ecm_text(const faxbot_ecm_t *ecm, char *out, size_t size)
{
	snprintf(out, size, "%d.%u.%u.%u.%u.%u.%u.%u.%u.%u.%u.%lld.%u.%lld.%d.%u", ecm->frame_octets, ecm->pps,
		ecm->blocks, ecm->frames, ecm->ppr, ecm->bad, ecm->runs, ecm->longest, ecm->resent, ecm->ctc, ecm->edges,
		ecm->edge_ms, ecm->rounds, ecm->round_ms, ecm->small_frames, ecm->pages);
}

/*
 * T.38 protection by packet category. spandsp's T.38 core gives every packet it sends a category (indicator,
 * low-speed control data, the end of control data, image data, the end of image data) and asks for it to be sent
 * as many times as its category's setting (t38_set_redundancy_control, the count passed to the packet handler).
 * Asterisk ignores that count ("count is ignored at the moment", res_fax_spandsp.c), so every packet goes once, and
 * only UDPTL redundancy (each packet carrying the previous ones, udptl.conf udptlfecentries) protects it; a burst
 * longer than that, or the last packets of a control exchange (no later packet carries them), is lost, and T.30
 * waits seconds and tries again. With FAXBOT_T38_PROTECT ("I.C.CE.IM.IME": each category's sends, 1 to
 * FAXBOT_T38_SEND_MAX; engine_learning.py sets it from the loss measured on the trunk), the session sets those
 * counts and every packet goes that many times with the same UDPTL sequence number (ast_udptl_write), as spandsp's
 * own UDPTL test harness sends repeats; a receiver drops the copies it already has (T.38 sequence numbers; Asterisk's
 * udptl_rx_packet and spandsp's t38_core_rx_ifp_packet both do). Unset or malformed keeps Asterisk's behaviour.
 */
#define FAXBOT_T38_CATEGORIES 5
#define FAXBOT_T38_SEND_MAX 4

/*! \brief The five counts in \p value ("3.2.3.1.3") into \p counts; 1 when every one is a count from 1 to 4. */
static inline int faxbot_t38_protect_parse(const char *value, int counts[FAXBOT_T38_CATEGORIES])
{
	int i;

	if (!value) {
		return 0;
	}
	for (i = 0; i < FAXBOT_T38_CATEGORIES; i++) {
		if (*value < '1' || *value > '0' + FAXBOT_T38_SEND_MAX) {
			return 0;
		}
		counts[i] = *value++ - '0';
		if (*value != (i == FAXBOT_T38_CATEGORIES - 1 ? '\0' : '.')) {
			return 0;
		}
		if (*value) {
			value++;
		}
	}
	return 1;
}

/*! \brief Set spandsp's per-category counts on \p core (the T.38 core of a terminal or a gateway). */
static inline void faxbot_t38_protect_apply(t38_core_state_t *core, const int counts[FAXBOT_T38_CATEGORIES])
{
	int i;

	for (i = 0; i < FAXBOT_T38_CATEGORIES; i++) {
		t38_set_redundancy_control(core, i, counts[i]);
	}
}

/*! \brief How many times a packet goes for spandsp's \p count while Faxbot's protection is on (1 to 4). */
static inline int faxbot_t38_sends(int count)
{
	return count < 1 ? 1 : count > FAXBOT_T38_SEND_MAX ? FAXBOT_T38_SEND_MAX : count;
}

/* The gateway's T.38 receive counts go on the channel after each of the first FAXBOT_T38_RX_FIRST packets (the
 * handshake, and every reply on a sent fax), then after every FAXBOT_T38_RX_EVERY-th (a received page's data). */
#define FAXBOT_T38_RX_FIRST 400
#define FAXBOT_T38_RX_EVERY 25

/*! \brief Whether the gateway writes FAXBOT_T38_RX after its \p received -th T.38 packet. */
static inline int faxbot_t38_rx_due(unsigned int received)
{
	return received <= FAXBOT_T38_RX_FIRST || received % FAXBOT_T38_RX_EVERY == 0;
}

#endif /* FAXBOT_T38_GATEWAY_H */
