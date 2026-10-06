/*
 * Faxbot: the two steps its T.38 gateway changes add to spandsp 0.0.6's gateway, in one place.
 *
 * res_fax_spandsp.c includes this file (patches 0002 and 0003; the image build copies it next to that
 * file), and so does the replay proof asterisk/tests/t38_gateway_replay.c, so the proof runs exactly the
 * steps the patches run. Include it after <spandsp.h>, with SPANDSP_EXPOSE_INTERNAL_STRUCTURES defined.
 *
 * Both steps read and change spandsp's internal gateway state, and FAXBOT_SPANDSP_FLAG_INDICATOR is
 * copied from spandsp 0.0.6's private t38_gateway.c. They were checked against the spandsp 0.0.6 in the
 * Debian package snapshot the images pin (release date 20110122), so any other spandsp stops the build.
 */
#ifndef FAXBOT_T38_GATEWAY_H
#define FAXBOT_T38_GATEWAY_H

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

#endif /* FAXBOT_T38_GATEWAY_H */
