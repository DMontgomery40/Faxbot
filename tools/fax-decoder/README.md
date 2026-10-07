# Faxbot Decoder (experimental)

A static page that turns Faxbot encoded fax pages back into the original document entirely in the browser; the fax is never uploaded. It reads the payload format that `api/app/codec` writes (grid, run-coded and picture pages, Reed-Solomon repair, deflate or zstd, and AES-256-GCM with a shared key) from a PDF or TIFF of the fax, or from PNG and JPEG pictures of its pages.

- Serve the folder over HTTPS or from `localhost` (for example `python3 -m http.server` in this folder): browsers load ES modules and WebCrypto only there, not from a `file://` address. The lead publishes it on faxbot.net/decode.
- Test it with `node --test test/decoder.test.mjs`. The fixtures come from `test/make_fixtures.py`, and `api/tests/test_codec_decoder.py` checks that they still match what the Python encoder makes.
- `vendor/fzstd.mjs` is fzstd 0.1.1 by Arjun Barrett (MIT, `vendor/fzstd-LICENSE`), unchanged.
