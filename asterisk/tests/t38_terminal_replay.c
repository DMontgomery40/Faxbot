/*
 * Proof for asterisk/patches/0004 and 0005: spandsp 0.0.6's T.38 terminal (Asterisk's built-in fax engine on T.38) with the
 * steps 0004 adds, from asterisk/patches/faxbot_t38_gateway.h, the file the image build compiles into Asterisk.
 *
 * Usage:
 *   t38_terminal_replay replay FILE send|receive
 *     FILE lines: "<seconds> <sequence number> <IFP hex>", the far end's T.38 as captured. A terminal that sends
 *     (send) or receives (receive) a fax hears it, with 0004's frame handler, and prints what it kept:
 *     "dis <hex>", "dcs_first <hex>", "dcs_last <hex>", "rates <codes>", "csa <hex>", "tsa <hex>", "sub <hex>",
 *     "trainings N", "ftt N" (empty hex when not seen).
 *   t38_terminal_replay pair TIFF OUT paced|peer [SUB [nosub]]
 *     Two terminals joined back to back send TIFF to OUT over T.38, with error correction. "peer" is 0004's
 *     Internet Aware Fax between two Faxbots (both ends: IAF mode, chunks sent ahead);
 *     "paced" is spandsp as Asterisk runs it. Prints "seconds S" (simulated time from start to the end of both
 *     sessions, in 20 ms ticks as Asterisk's timer runs), "pages N", "status SEND RECEIVE" and "packets N".
 *     With SUB, the sender asks for that subaddress as 0005 does (faxbot_sub_clean, then t30_set_tx_sub_address);
 *     The receiver says it takes a subaddress as 0005 makes Asterisk's receiver do (faxbot_receive_subaddress);
 *     "nosub" leaves it as spandsp 0.0.6 is by default (no DIS bit 49). Also prints "sub_asked TEXT",
 *     "far_dis <hex>" (the receiver's DIS as the sender kept it), "sub <hex>" (the SUB frame the receiver kept) and
 *     "rx_sub TEXT" (the subaddress spandsp's receiver read).
 *   t38_terminal_replay clean VALUE
 *     Prints "[TEXT]": what 0005's faxbot_sub_clean keeps of VALUE.
 */
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <inttypes.h>
#define SPANDSP_EXPOSE_INTERNAL_STRUCTURES
#include <spandsp.h>
#include "faxbot_t38_gateway.h"

#define MAX_PACKETS 8192
#define MAX_IFP 512
#define SAMPLES 160

static faxbot_frames_t frames;

static void frame_handler(t30_state_t *s, void *user_data, int direction, const uint8_t msg[], int len)
{
	faxbot_frames_record((faxbot_frames_t *) user_data, direction, msg, len);
}

static int no_tx(t38_core_state_t *s, void *user_data, const uint8_t *buf, int len, int count)
{
	return 0;
}

static void print_frame(const char *name, const faxbot_frame_t *frame)
{
	char hex[2 * FAXBOT_ADDRESS_MAX + 1];

	faxbot_hex(frame->frame, frame->len, hex);
	printf("%s %s\n", name, hex);
}

struct packet {
	double at;
	int seq;
	int len;
	uint8_t ifp[MAX_IFP];
};

static struct packet packets[MAX_PACKETS];

static int load(const char *path)
{
	FILE *file = fopen(path, "r");
	char line[4 * MAX_IFP];
	int count = 0;

	if (!file) {
		perror(path);
		exit(2);
	}
	while (count < MAX_PACKETS && fgets(line, sizeof(line), file)) {
		char hex[2 * MAX_IFP + 1];
		int i;

		if (sscanf(line, "%lf %d %1024s", &packets[count].at, &packets[count].seq, hex) != 3) {
			continue;
		}
		packets[count].len = (int) strlen(hex) / 2;
		for (i = 0; i < packets[count].len; i++) {
			unsigned int octet;
			sscanf(hex + 2 * i, "%2x", &octet);
			packets[count].ifp[i] = (uint8_t) octet;
		}
		count++;
	}
	fclose(file);
	return count;
}

static int replay(const char *path, int calling)
{
	t38_terminal_state_t terminal;
	t30_state_t *t30;
	char rates[3 * FAXBOT_RATES_MAX + 1];
	int count = load(path), next = 0, tick;
	double now;

	memset(&frames, 0, sizeof(frames));
	t38_terminal_init(&terminal, calling, no_tx, NULL);
	t30 = t38_terminal_get_t30_state(&terminal);
	t30_set_ecm_capability(t30, TRUE);
	t30_set_supported_compressions(t30, T30_SUPPORT_T4_1D_COMPRESSION | T30_SUPPORT_T4_2D_COMPRESSION
		| T30_SUPPORT_T6_COMPRESSION);
	t30_set_real_time_frame_handler(t30, frame_handler, &frames);
	if (calling) {
		t30_set_tx_file(t30, "/data/proof.tif", -1, -1);
	} else {
		t30_set_rx_file(t30, "/tmp/replay-received.tif", -1);
	}
	for (tick = 0; next < count && tick < 360000; tick++) {
		now = tick * 0.020;
		while (next < count && packets[next].at <= now) {
			t38_core_rx_ifp_packet(t38_terminal_get_t38_core_state(&terminal), packets[next].ifp, packets[next].len,
				(uint16_t) packets[next].seq);
			next++;
		}
		t38_terminal_send_timeout(&terminal, SAMPLES);
	}
	print_frame("dis", &frames.dis);
	print_frame("dcs_first", &frames.dcs_first);
	print_frame("dcs_last", &frames.dcs_last);
	faxbot_rates_text(&frames, rates, sizeof(rates));
	printf("rates %s\n", rates);
	print_frame("csa", &frames.csa);
	print_frame("tsa", &frames.tsa);
	print_frame("sub", &frames.sub);
	printf("trainings %u\nftt %u\n", frames.dcs, frames.ftt);
	return 0;
}

/* Two terminals back to back: what one sends, the other receives at once. */
static t38_terminal_state_t sender, receiver;
static faxbot_frames_t sender_frames, receiver_frames;
static int done_sender, done_receiver, status_sender = -1, status_receiver = -1, pages, packets_sent;

static int to_receiver(t38_core_state_t *s, void *user_data, const uint8_t *buf, int len, int count)
{
	static uint16_t seq;

	packets_sent++;
	t38_core_rx_ifp_packet(t38_terminal_get_t38_core_state(&receiver), buf, len, seq++);
	return 0;
}

static int to_sender(t38_core_state_t *s, void *user_data, const uint8_t *buf, int len, int count)
{
	static uint16_t seq;

	packets_sent++;
	t38_core_rx_ifp_packet(t38_terminal_get_t38_core_state(&sender), buf, len, seq++);
	return 0;
}

static void ended(t30_state_t *s, void *user_data, int result)
{
	t30_stats_t stats;

	if (user_data == &sender) {
		done_sender = 1;
		status_sender = result;
	} else {
		done_receiver = 1;
		status_receiver = result;
		t30_get_transfer_statistics(s, &stats);
		pages = stats.pages_rx;
	}
}

static int pair(const char *tiff, const char *out, int iaf, const char *sub, int no_sub)
{
	t30_state_t *a, *b;
	char clean[FAXBOT_SUB_MAX + 1], hex[2 * FAXBOT_ADDRESS_MAX + 1];
	int tick, i;

	t38_terminal_init(&sender, TRUE, to_receiver, NULL);
	t38_terminal_init(&receiver, FALSE, to_sender, NULL);
	a = t38_terminal_get_t30_state(&sender);
	b = t38_terminal_get_t30_state(&receiver);
	t30_set_tx_file(a, tiff, -1, -1);
	t30_set_rx_file(b, out, -1);
	t30_set_ecm_capability(a, TRUE);
	t30_set_ecm_capability(b, TRUE);
	t30_set_supported_compressions(a, T30_SUPPORT_T4_1D_COMPRESSION | T30_SUPPORT_T4_2D_COMPRESSION
		| T30_SUPPORT_T6_COMPRESSION);
	t30_set_supported_compressions(b, T30_SUPPORT_T4_1D_COMPRESSION | T30_SUPPORT_T4_2D_COMPRESSION
		| T30_SUPPORT_T6_COMPRESSION);
	t30_set_phase_e_handler(a, ended, &sender);
	t30_set_phase_e_handler(b, ended, &receiver);
	if (iaf) {
		t30_set_iaf_mode(a, faxbot_iaf_t30_mode(FAXBOT_IAF_PEER));
		t30_set_iaf_mode(b, faxbot_iaf_t30_mode(FAXBOT_IAF_PEER));
	}
	t30_set_real_time_frame_handler(a, frame_handler, &sender_frames);
	t30_set_real_time_frame_handler(b, frame_handler, &receiver_frames);
	/* What 0005 does in spandsp_fax_start for a fax that asks for a subaddress. */
	faxbot_sub_clean(sub, clean, sizeof(clean));
	if (clean[0]) {
		t30_set_tx_sub_address(a, clean);
	}
	/* The receiver as 0005 runs it (it says it takes a subaddress), or ("nosub") as spandsp 0.0.6 is by default. */
	if (!no_sub) {
		faxbot_receive_subaddress(b);
	}
	for (tick = 0; tick < 50 * 900 && !(done_sender && done_receiver); tick++) {
		t38_terminal_send_timeout(&sender, SAMPLES);
		t38_terminal_send_timeout(&receiver, SAMPLES);
		if (iaf) {
			/* What 0004's faxbot_iaf_send_queued does each tick in res_fax_spandsp. */
			for (i = 0; i < FAXBOT_IAF_AHEAD && faxbot_iaf_send_ahead(&sender); i++) {
			}
			for (i = 0; i < FAXBOT_IAF_AHEAD && faxbot_iaf_send_ahead(&receiver); i++) {
			}
		}
	}
	printf("seconds %.2f\npages %d\nstatus %d %d\npackets %d\n", tick * 0.020, pages, status_sender, status_receiver,
		packets_sent);
	printf("sub_asked %s\n", clean);
	faxbot_hex(sender_frames.dis.frame, sender_frames.dis.len, hex);
	printf("far_dis %s\n", hex);
	faxbot_hex(receiver_frames.sub.frame, receiver_frames.sub.len, hex);
	printf("sub %s\n", hex);
	printf("rx_sub %s\n", t30_get_rx_sub_address(b) ? t30_get_rx_sub_address(b) : "");
	return 0;
}

int main(int argc, char *argv[])
{
	if (argc == 4 && !strcmp(argv[1], "replay")) {
		return replay(argv[2], !strcmp(argv[3], "send"));
	}
	if (argc >= 5 && argc <= 7 && !strcmp(argv[1], "pair")) {
		return pair(argv[2], argv[3], !strcmp(argv[4], "peer"), argc >= 6 ? argv[5] : NULL,
			argc == 7 && !strcmp(argv[6], "nosub"));
	}
	if (argc == 3 && !strcmp(argv[1], "clean")) {
		char clean[FAXBOT_SUB_MAX + 1];

		faxbot_sub_clean(argv[2], clean, sizeof(clean));
		printf("[%s]\n", clean);
		return 0;
	}
	fprintf(stderr, "usage: %s replay FILE send|receive | pair TIFF OUT paced|peer [SUB [nosub]] | clean VALUE\n",
		argv[0]);
	return 2;
}
