/*
 * Replays a T.38 packet sequence into a spandsp T.38 gateway set up the way Asterisk's res_fax_spandsp
 * sets one up, and decodes the audio the gateway makes for the fax machine behind it (V.21 channel 2,
 * HDLC), as the SSL Fax engine's modem hears it. Proof for asterisk/patches/0002.
 *
 * Usage: t38_gateway_replay FILE MODE
 *   FILE lines: "<seconds> <sequence number> <IFP hex>"
 *   MODE 0: spandsp as shipped; MODE 1: with Faxbot's fix (the same steps as the patch's
 *   faxbot_end_empty_preamble, run before each block of audio).
 * Prints "frame <seconds> <length> <first bytes>" for each frame with a good FCS, then "frames N".
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#define SPANDSP_EXPOSE_INTERNAL_STRUCTURES
#include <spandsp.h>

#define FAXBOT_SPANDSP_FLAG_INDICATOR 0x100	/* FLAG_INDICATOR in spandsp 0.0.6's t38_gateway.c */
#define MAX_PACKETS 4096

static double now;
static int frames;

static int tx_packet(t38_core_state_t *s, void *user_data, const uint8_t *buf, int len, int count)
{
	return 0;	/* what the gateway sends to the far end is not needed here */
}

static void frame(void *user_data, const uint8_t *pkt, int len, int ok)
{
	int i;

	if (len < 0 || !ok) {
		return;
	}
	frames++;
	printf("frame %.3f %d", now, len);
	for (i = 0; i < len && i < 4; i++) {
		printf(" %02x", pkt[i]);
	}
	printf("\n");
}

static void put_bit(void *user_data, int bit)
{
	hdlc_rx_put_bit((hdlc_rx_state_t *) user_data, bit);
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
	}
	hdlc_tx_frame(hdlc, NULL, 0);
}

int main(int argc, char *argv[])
{
	static double times[MAX_PACKETS];
	static int seqs[MAX_PACKETS], lens[MAX_PACKETS];
	static uint8_t ifps[MAX_PACKETS][64];
	char hex[129];
	int count = 0, next = 0, mode, i;
	FILE *in;
	t38_gateway_state_t *gw;
	t38_core_state_t *core;
	hdlc_rx_state_t *hdlc;
	fsk_rx_state_t *fsk;
	int16_t amp[160], silence[160];

	if (argc < 3 || !(in = fopen(argv[1], "r"))) {
		return 2;
	}
	mode = atoi(argv[2]);
	while (count < MAX_PACKETS && fscanf(in, "%lf %d %128s", &times[count], &seqs[count], hex) == 3) {
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
	memset(silence, 0, sizeof(silence));

	for (now = 0.0; now < times[count - 1] + 3.0; now += 0.020) {
		while (next < count && times[next] <= now) {
			t38_core_rx_ifp_packet(core, ifps[next], lens[next], (uint16_t) seqs[next]);
			next++;
		}
		t38_gateway_rx(gw, silence, 160);
		if (mode == 1) {
			end_empty_preamble(gw);
		}
		memset(amp, 0, sizeof(amp));
		t38_gateway_tx(gw, amp, 160);
		fsk_rx(fsk, amp, 160);
	}
	printf("frames %d\n", frames);
	return 0;
}
