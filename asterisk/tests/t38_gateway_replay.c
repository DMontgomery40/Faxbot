/*
 * Replays a T.38 packet sequence into a spandsp T.38 gateway set up the way Asterisk's res_fax_spandsp
 * sets one up, and decodes both directions, as the SSL Fax engine's modem and the far end see them:
 *  - the audio the gateway makes for the fax machine behind it: V.21 channel 2 HDLC frames, and V.29
 *    9600 bit/s data (the training check, TCF, and page data);
 *  - optionally, a fax machine's own signals played into the gateway (V.21 frames, a V.29 TCF), and the
 *    T.38 the gateway sends to the far end for them.
 * Proof for asterisk/patches/0002 and 0003.
 *
 * Usage: t38_gateway_replay FILE MODE [MODEM]
 *   FILE lines: "<seconds> <sequence number> <IFP hex>", the far end's T.38.
 *   MODE 0: spandsp as shipped; 1: with patch 0002 (faxbot_end_empty_preamble); 2: with patches 0002 and
 *   0003 (faxbot_end_empty_training). Each runs before each block of audio, as in the patches.
 *   MODEM lines: "<seconds> v21 <frame hex without FCS>" or "<seconds> tcf <seconds of zeros at 9600>",
 *   what the fax machine behind the gateway sends (silence otherwise).
 * Prints, in time order:
 *   "frame <seconds> <length> <first bytes>"  a V.21 frame with a good FCS heard by the fax machine;
 *   "data <seconds> <bytes> <non-zero> <longest zero run>"  each V.29 carrier the fax machine trained on,
 *     counted the way HylaFAX counts TCF (whole bytes after training until the carrier drops);
 *   "t38 frame <seconds> <length> <first bytes>"  a V.21 frame the gateway sent to the far end (FCS good);
 *   "t38 data <seconds> <bytes> <non-zero> <longest zero run>"  non-ECM data it sent, per signal;
 *   "cut <seconds>" / "ended <seconds>"  each time patch 0002 cut a preamble or 0003 ended a training;
 * then "frames N".
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#define SPANDSP_EXPOSE_INTERNAL_STRUCTURES
#include <spandsp.h>

#define FAXBOT_SPANDSP_FLAG_INDICATOR 0x100	/* FLAG_INDICATOR in spandsp 0.0.6's t38_gateway.c */
#define MAX_PACKETS 4096
#define MAX_IFP 512
#define MAX_EVENTS 64

static double now;
static int frames;

/* What a receiver heard: one V.29 carrier, or one T.38 non-ECM signal, at a time. */
struct count {
	int active;
	double started;
	int bits, octet, bytes, nonzero, zero_run, longest;
};

static struct count data;	/* V.29 at the fax machine */
static int data_restart;	/* the receiver parks after a failed training or a dropped carrier: listen again */
static struct count t38_data;	/* non-ECM data the gateway sent to the far end */
static uint8_t t38_frame[256];
static int t38_frame_len;
static t38_core_state_t *decoder;
static int decoder_seq;

static void count_octet(struct count *c, int octet)
{
	c->bytes++;
	if (octet) {
		c->nonzero++;
		c->zero_run = 0;
	} else if (++c->zero_run > c->longest) {
		c->longest = c->zero_run;
	}
}

static void count_end(const char *prefix, struct count *c)
{
	if (c->active && c->bytes) {
		printf("%sdata %.3f %d %d %d\n", prefix, c->started, c->bytes, c->nonzero, c->longest);
	}
	memset(c, 0, sizeof(*c));
}

static void print_frame(const char *prefix, const uint8_t *pkt, int len)
{
	int i;

	printf("%sframe %.3f %d", prefix, now, len);
	for (i = 0; i < len && i < 4; i++) {
		printf(" %02x", pkt[i]);
	}
	printf("\n");
}

/* The gateway's T.38 to the far end, decoded by a second T.38 core. */
static int tx_packet(t38_core_state_t *s, void *user_data, const uint8_t *buf, int len, int count)
{
	t38_core_rx_ifp_packet(decoder, buf, len, (uint16_t) decoder_seq++);
	return 0;
}

static int decoded_indicator(t38_core_state_t *s, void *user_data, int indicator)
{
	return 0;
}

static int decoded_data(t38_core_state_t *s, void *user_data, int data_type, int field_type, const uint8_t *buf, int len)
{
	int i;

	switch (field_type) {
	case T38_FIELD_HDLC_DATA:
		for (i = 0; i < len && t38_frame_len < (int) sizeof(t38_frame); i++) {
			t38_frame[t38_frame_len++] = bit_reverse8(buf[i]);
		}
		break;
	case T38_FIELD_HDLC_FCS_OK:
	case T38_FIELD_HDLC_FCS_OK_SIG_END:
		print_frame("t38 ", t38_frame, t38_frame_len);
		t38_frame_len = 0;
		break;
	case T38_FIELD_HDLC_FCS_BAD:
	case T38_FIELD_HDLC_FCS_BAD_SIG_END:
	case T38_FIELD_HDLC_SIG_END:
		t38_frame_len = 0;
		break;
	case T38_FIELD_T4_NON_ECM_DATA:
	case T38_FIELD_T4_NON_ECM_SIG_END:
		if (!t38_data.active) {
			t38_data.active = 1;
			t38_data.started = now;
		}
		for (i = 0; i < len; i++) {
			count_octet(&t38_data, buf[i]);
		}
		if (field_type == T38_FIELD_T4_NON_ECM_SIG_END) {
			count_end("t38 ", &t38_data);
		}
		break;
	}
	return 0;
}

static int decoded_missing(t38_core_state_t *s, void *user_data, int rx_seq_no, int expected_seq_no)
{
	return 0;
}

static void frame(void *user_data, const uint8_t *pkt, int len, int ok)
{
	if (len < 0 || !ok) {
		return;
	}
	frames++;
	print_frame("", pkt, len);
}

static void put_bit(void *user_data, int bit)
{
	hdlc_rx_put_bit((hdlc_rx_state_t *) user_data, bit);
}

static void data_bit(void *user_data, int bit)
{
	if (bit < 0) {
		if (bit == SIG_STATUS_TRAINING_SUCCEEDED) {
			memset(&data, 0, sizeof(data));
			data.active = 1;
			data.started = now;
		} else if (bit == SIG_STATUS_CARRIER_DOWN || bit == SIG_STATUS_TRAINING_FAILED) {
			count_end("", &data);
			data_restart = 1;
		}
		return;
	}
	if (!data.active) {
		return;
	}
	data.octet |= (bit & 1) << data.bits;
	if (++data.bits == 8) {
		count_octet(&data, data.octet);
		data.bits = data.octet = 0;
	}
}

/* The fax machine behind the gateway: one signal at a time from the MODEM file. */
static struct {
	double at[MAX_EVENTS];
	int tcf_bits[MAX_EVENTS];
	uint8_t frame[MAX_EVENTS][64];
	int frame_len[MAX_EVENTS];
	int count, next;
	int sending;	/* 0 silence, 1 V.21, 2 V.29 */
	int bits_left;
	hdlc_tx_state_t hdlc;
	fsk_tx_state_t fsk;
	v29_tx_state_t v29;
} modem;

static int tcf_bit(void *user_data)
{
	if (modem.bits_left <= 0) {
		return SIG_STATUS_END_OF_DATA;
	}
	modem.bits_left--;
	return 0;
}

static void modem_load(const char *path)
{
	FILE *in = fopen(path, "r");
	char kind[8], value[129];
	int i;

	if (!in) {
		exit(2);
	}
	while (modem.count < MAX_EVENTS && fscanf(in, "%lf %7s %128s", &modem.at[modem.count], kind, value) == 3) {
		if (!strcmp(kind, "tcf")) {
			modem.tcf_bits[modem.count] = (int) (atof(value) * 9600);
		} else {
			modem.frame_len[modem.count] = (int) strlen(value) / 2;
			for (i = 0; i < modem.frame_len[modem.count]; i++) {
				sscanf(value + 2 * i, "%2hhx", &modem.frame[modem.count][i]);
			}
		}
		modem.count++;
	}
	fclose(in);
}

static void modem_tx(int16_t amp[], int len)
{
	int n = 0, e;

	memset(amp, 0, len * sizeof(int16_t));
	if (!modem.sending && modem.next < modem.count && modem.at[modem.next] <= now) {
		e = modem.next++;
		if (modem.tcf_bits[e]) {
			modem.bits_left = modem.tcf_bits[e];
			v29_tx_init(&modem.v29, 9600, 0, tcf_bit, NULL);
			modem.sending = 2;
		} else {
			/* A V.21 frame after a one-second preamble of flags, as a fax machine sends one. */
			hdlc_tx_init(&modem.hdlc, 0, 2, 0, NULL, NULL);
			hdlc_tx_flags(&modem.hdlc, 37);
			hdlc_tx_frame(&modem.hdlc, modem.frame[e], modem.frame_len[e]);
			hdlc_tx_frame(&modem.hdlc, NULL, 0);
			fsk_tx_init(&modem.fsk, &preset_fsk_specs[FSK_V21CH2], (get_bit_func_t) hdlc_tx_get_bit, &modem.hdlc);
			modem.sending = 1;
		}
	}
	if (modem.sending == 1) {
		n = fsk_tx(&modem.fsk, amp, len);
	} else if (modem.sending == 2) {
		n = v29_tx(&modem.v29, amp, len);
	}
	if (modem.sending && n < len) {
		modem.sending = 0;
	}
}

/* The same steps as faxbot_end_empty_preamble in asterisk/patches/0002-t38-gateway-empty-preamble.patch. */
static void end_empty_preamble(t38_gateway_state_t *gw)
{
	t38_gateway_hdlc_state_t *queue = &gw->core.hdlc_to_modem;
	hdlc_tx_state_t *hdlc = &gw->audio.modems.hdlc_tx;

	if (queue->in == queue->out || !(queue->buf[queue->out].contents & FAXBOT_SPANDSP_FLAG_INDICATOR) || hdlc->len) {
		return;
	}
	if (hdlc->flag_octets > 2) {
		hdlc->flag_octets = 2;
		printf("cut %.3f\n", now);
	}
	hdlc_tx_frame(hdlc, NULL, 0);
}

/* The same steps as faxbot_end_empty_training in asterisk/patches/0003-t38-gateway-open-fast-modem-signal.patch:
 * only while the fast modem sends the far end's data (not while it trains or shuts down). */
static void end_empty_training(t38_gateway_state_t *gw)
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
		return;
	}
	t38_non_ecm_buffer_push(buffer);
	printf("ended %.3f\n", now);
}

int main(int argc, char *argv[])
{
	static double times[MAX_PACKETS];
	static int seqs[MAX_PACKETS], lens[MAX_PACKETS];
	static uint8_t ifps[MAX_PACKETS][MAX_IFP];
	char hex[2 * MAX_IFP + 1];
	int count = 0, next = 0, mode, i;
	double end;
	FILE *in;
	t38_gateway_state_t *gw;
	t38_core_state_t *core;
	hdlc_rx_state_t *hdlc;
	fsk_rx_state_t *fsk;
	v29_rx_state_t *v29;
	int16_t amp[160], heard[160];

	if (argc < 3 || !(in = fopen(argv[1], "r"))) {
		return 2;
	}
	mode = atoi(argv[2]);
	while (count < MAX_PACKETS && fscanf(in, "%lf %d %1024s", &times[count], &seqs[count], hex) == 3) {
		lens[count] = (int) strlen(hex) / 2;
		for (i = 0; i < lens[count]; i++) {
			sscanf(hex + 2 * i, "%2hhx", &ifps[count][i]);
		}
		count++;
	}
	fclose(in);
	if (!count) {
		return 2;
	}
	if (argc > 3) {
		modem_load(argv[3]);
	}

	decoder = t38_core_init(NULL, decoded_indicator, decoded_data, decoded_missing, NULL, NULL, NULL);
	t38_set_t38_version(decoder, 0);
	gw = t38_gateway_init(NULL, tx_packet, NULL);
	core = t38_gateway_get_t38_core_state(gw);
	/* As res_fax_spandsp's spandsp_fax_gateway_start, with the far end's parameters (T.38 version 0,
	 * 400-byte datagrams, transferred TCF) and res_fax.conf's ecm=yes, modems=v17,v27,v29. */
	t38_set_t38_version(core, 0);
	t38_gateway_set_ecm_capability(gw, 1);
	t38_set_max_datagram_size(core, 400);
	t38_set_fill_bit_removal(core, 0);
	t38_set_mmr_transcoding(core, 0);
	t38_set_jbig_transcoding(core, 0);
	t38_set_data_rate_management_method(core, 1);
	t38_gateway_set_transmit_on_idle(gw, 1);
	t38_set_sequence_number_handling(core, 1);
	t38_gateway_set_supported_modems(gw, T30_SUPPORT_V27TER | T30_SUPPORT_V29 | T30_SUPPORT_V17);

	hdlc = hdlc_rx_init(NULL, 0, 0, 5, frame, NULL);
	fsk = fsk_rx_init(NULL, &preset_fsk_specs[FSK_V21CH2], FSK_FRAME_MODE_SYNC, put_bit, hdlc);
	v29 = v29_rx_init(NULL, 9600, data_bit, NULL);

	end = times[count - 1];
	for (i = 0; i < modem.count; i++) {
		if (modem.at[i] + 2.0 + modem.tcf_bits[i] / 9600.0 > end) {
			end = modem.at[i] + 2.0 + modem.tcf_bits[i] / 9600.0;
		}
	}
	for (now = 0.0; now < end + 3.0; now += 0.020) {
		while (next < count && times[next] <= now) {
			t38_core_rx_ifp_packet(core, ifps[next], lens[next], (uint16_t) seqs[next]);
			next++;
		}
		modem_tx(heard, 160);
		t38_gateway_rx(gw, heard, 160);
		if (mode >= 1) {
			end_empty_preamble(gw);
		}
		if (mode >= 2) {
			end_empty_training(gw);
		}
		memset(amp, 0, sizeof(amp));
		t38_gateway_tx(gw, amp, 160);
		fsk_rx(fsk, amp, 160);
		v29_rx(v29, amp, 160);
		if (data_restart) {
			data_restart = 0;
			v29_rx_restart(v29, 9600, 0);
		}
	}
	count_end("", &data);
	count_end("t38 ", &t38_data);
	printf("frames %d\n", frames);
	return 0;
}
