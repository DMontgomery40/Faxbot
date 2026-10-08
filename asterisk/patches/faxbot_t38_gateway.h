/*
 * Faxbot: what its changes add to spandsp 0.0.6, in one place: the two T.38 gateway steps (patches 0002 and
 * 0003), and the far-end frame capture and Internet Aware Fax steps of patch 0004.
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

#endif /* FAXBOT_T38_GATEWAY_H */
