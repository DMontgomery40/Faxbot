"""The faxbot command: global options, error reporting and the command groups.

Importing this module must stay cheap and side-effect free: it never imports the
API application, its database binding or the configuration store. Commands that
need them import them when they run.
"""
import os

import typer
from typer.core import TyperGroup

from . import profiles, state
from .commands import access, admin, delivery, fax, operations, settings, setup, trunk
from .errors import CliError
from .output import Output, error
from .state import State

VERSION = '1.0.0'


class FaxbotGroup(TyperGroup):
    """Report expected failures as one plain sentence; never print tracebacks or local values."""

    def invoke(self, ctx):
        try:
            return super().invoke(ctx)
        except CliError as failure:
            error(failure.message, json_mode=_json_mode(ctx), data=failure.as_dict())
            raise typer.Exit(failure.exit_code) from None
        except Exception as failure:
            # Usage errors, Exit and Abort belong to the command framework, which reports them itself.
            if type(failure).__module__.startswith('typer') or os.environ.get('FAXBOT_CLI_DEBUG') == '1':
                raise
            message = 'The command stopped unexpectedly. Run it again with FAXBOT_CLI_DEBUG=1 for technical details.'
            error(message, json_mode=_json_mode(ctx), data={'message': message, 'exit_code': 1})
            raise typer.Exit(1) from None


def _json_mode(ctx):
    return isinstance(ctx.obj, State) and ctx.obj.out.json_mode


app = typer.Typer(
    name='faxbot', cls=FaxbotGroup, no_args_is_help=True, pretty_exceptions_enable=False,
    help='Send and receive faxes and run a Faxbot installation from the command line.\n\n'
         'Commands talk to a running Faxbot server with an API key. The admin commands work on a '
         'stopped installation on this computer.',
    context_settings={'help_option_names': ['-h', '--help']},
)


def _version(value):
    if value:
        typer.echo(f'faxbot {VERSION}')
        raise typer.Exit()


@app.callback()
def main(
    ctx: typer.Context,
    url: str = typer.Option(None, '--url', envvar='FAXBOT_URL', metavar='ADDRESS',
                            help='Faxbot server address, for example https://fax.example.com. '
                                 'Defaults to your saved profile, then http://localhost:8080.'),
    key: str = typer.Option(None, '--key', envvar='FAXBOT_API_KEY', metavar='API_KEY', show_default=False,
                            help='API key to use. Defaults to your saved profile. Prefer the environment '
                                 'variable or a profile so the key stays out of your shell history.'),
    profile: str = typer.Option(None, '--profile', envvar='FAXBOT_PROFILE', metavar='NAME',
                                help='Saved profile to use (see faxbot config).'),
    json_output: bool = typer.Option(False, '--json', help='Print results as JSON, for scripts.'),
    quiet: bool = typer.Option(False, '--quiet', '-q',
                               help='Print nothing on success, except secrets shown only once.'),
    version: bool = typer.Option(False, '--version', callback=_version, is_eager=True,
                                 help='Show the version and exit.'),
):
    supplied = ctx.obj if isinstance(ctx.obj, dict) else {}
    out = Output(json_mode=json_output, quiet=quiet)
    document = profiles.load()
    name, saved = profiles.select(document, profile)
    saved_url = (saved.get('url') or profiles.DEFAULT_URL).rstrip('/')
    address = (url or saved_url).rstrip('/')
    # A saved key is sent only to the server it was saved for, never to another --url or FAXBOT_URL.
    saved_key = saved.get('key') if address == saved_url else None
    ctx.obj = state.begin(ctx, State(url=address, key=key or saved_key or None, profile=name, out=out,
                                     client_factory=supplied.get('client_factory')))


fax.register(app)
access.register(app)
settings.register(app)
trunk.register(app)
delivery.register(app)
operations.register(app)
setup.register(app)
admin.register(app)


def run():
    app(prog_name='faxbot')
