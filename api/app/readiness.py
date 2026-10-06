"""Whether Faxbot is ready for what it is set up to do, from the readiness answer (``GET /health/ready``).

One rule for ``/health/ready``'s HTTP status and ``faxbot system health``'s exit code. It reads only
the answer's public fields, so the command line applies it to any server's answer.
"""


def ready_for_setup(ready):
    """``(set up to send, set up to receive, ready for each of those)`` from ``/health/ready``.

    An install with no sending provider is judged on receiving alone, one with sending and no
    receiving on sending alone, and one set up for both must be ready for both, as the console's
    Overview judges it; one set up for neither is not ready.
    """
    ready = ready or {}
    inbound = (ready.get('checks') or {}).get('inbound') or {}
    sends = bool(ready.get('backend'))
    receives = bool(inbound.get('enabled') and inbound.get('backend'))
    ok = ((sends or receives) and (not sends or ready.get('status') == 'ready')
          and (not receives or bool(ready.get('ready_to_receive'))))
    return sends, receives, bool(ok)
