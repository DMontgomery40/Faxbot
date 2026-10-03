"""Explicit direct-loopback development listener with proxy rewriting disabled.

Production deployments use their HTTPS server / explicitly trusted proxy
configuration. Only this listener can stamp the insecure development exception.
"""
import argparse
import os

from .access.transport import _DIRECT_LOOPBACK


class _DirectLoopback:
    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        scope = dict(scope)
        if scope['type'] in {'http', 'websocket'}:
            scope['faxbot.direct_loopback'] = _DIRECT_LOOPBACK
        await self.app(scope, receive, send)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--loopback', action='store_true', required=True)
    parser.add_argument('--port', type=int, default=8080)
    arguments = parser.parse_args()
    if not 1 <= arguments.port <= 65535:
        parser.error('Port must be between 1 and 65535.')
    os.environ['FAXBOT_ALLOW_INSECURE_LOOPBACK'] = 'true'
    os.environ.setdefault('FAXBOT_CONSOLE_ORIGINS', f'http://127.0.0.1:{arguments.port},http://localhost:{arguments.port}')
    # Import only after fixing the deployment policy; no reload subprocess can
    # lose the server-owned profile. The listener never accepts remote peers.
    from .main import app
    import uvicorn
    uvicorn.run(_DirectLoopback(app), host='127.0.0.1', port=arguments.port,
        proxy_headers=False, access_log=False)


if __name__ == '__main__':
    main()
