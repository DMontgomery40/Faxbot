"""Encoded pages (experimental): per-recipient opt-in, fax detail lines, and encoding or decoding a file here."""
from pathlib import Path

import typer

from .. import state
from ..client import segment
from ..errors import CliError, EXIT_FAILURE
from ..output import local_time

numbers = typer.Typer(help='Encoded pages (experimental): allow recipient-approved documents that the recipient\'s '
                           'Faxbot decodes. Faxbot compares each attempt\'s route.', no_args_is_help=True)
tools = typer.Typer(help='Encode a document as payload pages, or decode payload pages from a received fax file, on '
                         'this computer (experimental).', no_args_is_help=True)

STYLES = {'dense': 'Dense pages', 'picture': 'A picture of the first page'}


def _number_human(view):
    def human(out):
        agreement = view.get('agreement') or {}
        out.line(view['state_sentence'])
        out.fields([('Fax number', view['number']), ('Encoded pages', 'on' if view['enabled'] else 'off'),
                    ('Page style', STYLES.get(view['style'], view['style'])),
                    ('Error correction', view['fec'].capitalize()),
                    ('Shared key', f"set (fingerprint {view['key_fingerprint']})" if view['has_key'] else 'none'),
                    ('Recipient agreement recorded by', agreement.get('by')),
                    ('Recorded', local_time(agreement.get('at')) if agreement else None)])
        out.line(view['limits_text'])
    return human


@numbers.command('show')
def encoded_show(number: str = typer.Argument(..., help='Fax number.')):
    """Show whether faxes to a number may go as encoded pages, and who recorded the recipient's agreement."""
    view = state.api().get('/codec/numbers/' + segment(number))
    state.out().result(view, _number_human(view))


@numbers.command('set')
def encoded_set(number: str = typer.Argument(..., help='Fax number.'),
                recipient_agreed: bool = typer.Option(False, '--recipient-agreed',
                    help="Record that this recipient agreed to receive encoded pages that their Faxbot decodes. "
                         'Needed to turn encoded pages on.'),
                style: str = typer.Option(None, '--style', metavar='dense|picture',
                    help='Dense pages (the default), or a picture of the first page with the document hidden in its '
                         'dots. With a shared key the picture is a plain pattern instead.'),
                fec: str = typer.Option(None, '--error-correction', metavar='low|medium|high',
                    help='How much damage on the line the pages survive (default medium). Higher carries less.'),
                key: str = typer.Option(None, '--shared-key', metavar='KEY',
                    help='Encrypt documents with a key you and the recipient agreed outside fax (8 to 200 '
                         'characters). Only its fingerprint is shown afterwards.'),
                clear_key: bool = typer.Option(False, '--clear-key', help='Stop encrypting with the shared key.')):
    """Turn encoded pages on for a number, or change their style, error correction or shared key."""
    if style is not None and style not in STYLES:
        raise CliError('Choose --style dense or --style picture.')
    if fec is not None and fec not in ('low', 'medium', 'high'):
        raise CliError('Choose --error-correction low, medium or high.')
    api = state.api()
    current = api.get('/codec/numbers/' + segment(number))
    body = {'enabled': True, 'recipient_agreed': recipient_agreed, 'version': current.get('version', 0),
            'clear_key': clear_key}
    for name, value in (('style', style), ('fec', fec), ('shared_key', key)):
        if value is not None:
            body[name] = value
    view = api.put('/codec/numbers/' + segment(number), json=body)
    state.out().result(view, _number_human(view))


@numbers.command('off')
def encoded_off(number: str = typer.Argument(..., help='Fax number.')):
    """Turn encoded pages off for a number; its faxes go as normal pages."""
    view = state.api().delete('/codec/numbers/' + segment(number))
    state.out().result(view, lambda out: out.line(view['state_sentence']))


def received_line(api, inbound_id):
    """The decode result of a received fax that carried encoded pages, or None."""
    try:
        return api.get('/codec/received/' + segment(inbound_id)).get('sentence')
    except CliError:
        return None


def received_decoded(inbound_id: str = typer.Argument(..., help="A received fax's ID, from 'faxbot received list --ids'."),
                     output: str = typer.Option(None, '--output', '-o', help="File to write. Use '-' for standard output."),
                     force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Download the original document a received fax carried as encoded pages (experimental)."""
    from .fax import _report_saved, received_id, save_document
    api = state.api()
    response = received_id(api, inbound_id, lambda fax_id: api.get(
        f'/codec/received/{segment(fax_id)}/document', raw=True))
    _report_saved(save_document(response, output, f'decoded_{inbound_id}.pdf', force), len(response.content))


@tools.command('decode')
def codec_decode(source: Path = typer.Argument(..., exists=True, dir_okay=False,
                                               help='A received fax file: PDF, TIFF, PNG or JPEG.'),
                 output: Path = typer.Option(None, '--output', '-o', help='Where to write the original document.'),
                 key: str = typer.Option(None, '--shared-key', metavar='KEY',
                                         help='The shared key, when the document was encrypted.'),
                 force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Decode the encoded pages in a fax file on this computer, check the document's fingerprint and save it."""
    from ... import codec
    try:
        document, report = codec.decode_images(codec.read_images(source), secrets=[key] if key else [])
    except codec.CodecError as error:
        raise CliError(str(error), exit_code=EXIT_FAILURE) from None
    extension = '.txt' if document.content_type == 'text/plain' else '.pdf'
    target = output or source.with_name(source.stem + '-decoded' + extension)
    if target.exists() and not force:
        raise CliError(f'{target} already exists; add --force to replace it.')
    target.write_bytes(document.data)
    result = {'file': str(target), 'bytes': len(document.data), 'sha256': document.sha256,
              'pages_read': report['pages_read'], 'pages': report['pages_expected']}
    state.out().result(result, lambda out: out.line(
        f"Decoded {report['pages_read']} of {report['pages_expected']} encoded pages and saved the original "
        f"({len(document.data):,} bytes, fingerprint checked) to {target}."))


@tools.command('encode')
def codec_encode(source: Path = typer.Argument(..., exists=True, dir_okay=False, help='A PDF or plain-text file.'),
                 output: Path = typer.Option(..., '--output', '-o', help='The fax TIFF to write.'),
                 resolution: str = typer.Option('fine', '--resolution', metavar='standard|fine|superfine|300|400',
                                                help='The fax resolution the pages are made for.'),
                 layout: str = typer.Option('grid', '--layout', metavar='grid|runs|picture|enumerative',
                     help='grid survives resolution changes; runs carries the most but needs the exact image; '
                          'picture hides the document in a picture; enumerative needs an unchanged image '
                          'and a recipient whose Faxbot supports enumerative profile 1.'),
                 fec: str = typer.Option('medium', '--error-correction', metavar='low|medium|high',
                                         help='How much damage the pages survive.'),
                 key: str = typer.Option(None, '--shared-key', metavar='KEY', help='Encrypt with this shared key.'),
                 force: bool = typer.Option(False, '--force', help='Replace the file if it exists.')):
    """Encode a document as payload pages on this computer and save them as a fax TIFF."""
    from ... import codec
    if output.exists() and not force:
        raise CliError(f'{output} already exists; add --force to replace it.')
    data = source.read_bytes()
    content_type = 'application/pdf' if data[:5] == b'%PDF-' else 'text/plain'
    try:
        encoded = codec.encode_document(codec.Document(data, content_type, source.name), resolution=resolution,
                                        layout=layout, fec=fec, secret=key)
    except (codec.CodecError, KeyError) as error:
        raise CliError(str(error).strip("'") or 'The document could not be encoded.') from None
    codec.write_tiff(encoded.pages, output)
    result = {'file': str(output), 'pages': encoded.page_count, 'layout': layout, 'resolution': resolution}
    state.out().result(result, lambda out: out.line(
        f"Saved {encoded.page_count} encoded page{'s' if encoded.page_count != 1 else ''} to {output} "
        '(experimental).'))
