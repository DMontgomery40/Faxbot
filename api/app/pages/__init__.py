"""Dense pages: several original pages stacked onto long fax pages, as long as the receiving machine allows.

- ``marks``: the faint boundary rule (a machine-readable dotted line) and the small "page 2 of 5" tag.
- ``packing``: the deterministic layout and rendering of packed pages, cut at the receiver's limit.
- ``unpack``: a receiving Faxbot splits marked long pages back into the original pages.
- ``capability``: what each receiving machine said it accepts (T.30 DIS), and the long-page settings.
- ``decision``: whether packing saves anything on a route's billing model.
- ``sending``: the attempt-time hook that prepares a packed image for one send and records it.
- ``friendly``: fax-friendly pages, light shading lightened and specks removed (migration 0042).
"""
