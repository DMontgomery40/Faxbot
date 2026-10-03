"""Host terminal sessions for the admin console over an authenticated WebSocket.

The route in main.py authenticates the socket with a single-use ticket before
handing it here. This module runs the PTY and keeps rechecking that the
socket's credential still holds host:terminal: before each input batch and on
a timer while idle, closing with 1008 as soon as it does not. Terminal input
is never logged.
"""
import asyncio
import json
import logging
import os
import time
import uuid

from fastapi import WebSocket, WebSocketDisconnect
import pexpect

logger = logging.getLogger(__name__)

# Idle recheck interval; a revoked or disabled credential is closed within this
# bound (plus one authorization round trip), which must stay under 5 seconds.
RECHECK_SECONDS = 2.0
# Input arriving within this long of the last successful check belongs to the same batch.
BATCH_SECONDS = 0.5
POLICY_VIOLATION = 1008


class TerminalManager:
    """Manages terminal sessions for WebSocket connections"""

    def __init__(self):
        self.sessions = {}

    async def create_session(self, websocket: WebSocket, session_id: str):
        """Create a new terminal session"""
        try:
            shell = os.environ.get('SHELL', '/bin/bash')
            if not os.path.exists(shell):
                shell = '/bin/sh'
            cols, rows = 80, 24
            logger.info("Starting terminal session %s with shell %s", session_id, shell)

            env = os.environ.copy()
            env['TERM'] = 'xterm-256color'
            env['PS1'] = r'\[\033[01;32m\]\u@faxbot\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '

            # Force interactive mode when possible to ensure prompt + echo
            base = os.path.basename(shell)
            args = ['-i'] if base in ('bash', 'sh', 'zsh', 'ash', 'dash') else []
            process = pexpect.spawn(shell, args=args, env=env, encoding='utf-8', timeout=None, dimensions=(rows, cols))

            session = {'process': process, 'websocket': websocket, 'active': True}
            self.sessions[session_id] = session
            await websocket.send_text(json.dumps({'type': 'output', 'data': f"Faxbot Terminal - {shell}\r\n"}))
            # Ensure sane TTY settings (echo enabled, line mode)
            try:
                process.sendline('stty sane 2>/dev/null || true')
            except Exception:
                pass
            session['reader'] = asyncio.create_task(self._read_terminal_output(session_id))
            logger.info("Terminal session %s created", session_id)
            return True
        except Exception:
            logger.exception("Failed to create terminal session")
            try:
                await websocket.send_text(json.dumps({'type': 'error', 'message': 'The terminal could not start.'}))
            except Exception:
                pass
            return False

    async def _read_terminal_output(self, session_id: str):
        """Continuously read output from the terminal and send to WebSocket"""
        session = self.sessions.get(session_id)
        if not session:
            return
        process = session['process']
        websocket = session['websocket']
        try:
            while session['active']:
                try:
                    # A zero timeout never blocks the event loop.
                    output = process.read_nonblocking(size=1024, timeout=0)
                    if output:
                        await websocket.send_text(json.dumps({'type': 'output', 'data': output}))
                except pexpect.TIMEOUT:
                    await asyncio.sleep(0.02)
                except pexpect.EOF:
                    logger.info("Terminal session %s process ended", session_id)
                    await websocket.send_text(json.dumps({'type': 'exit', 'message': 'The terminal session ended.'}))
                    break
                except Exception:
                    if session['active']:
                        logger.error("Terminal output stopped for session %s", session_id)
                    break
        except Exception:
            logger.error("Terminal output reader failed for session %s", session_id)
        finally:
            await self.close_session(session_id)

    async def send_input(self, session_id: str, data: str):
        """Send input to the terminal"""
        session = self.sessions.get(session_id)
        if not session or not session['active']:
            return False
        try:
            session['process'].send(data)
            return True
        except Exception:
            logger.error("Could not send input to terminal session %s", session_id)
            return False

    async def resize_terminal(self, session_id: str, cols: int, rows: int):
        """Resize the terminal window"""
        session = self.sessions.get(session_id)
        if not session or not session['active']:
            return False
        try:
            session['process'].setwinsize(rows, cols)
            return True
        except Exception:
            # pexpect may raise if the child ended
            return False

    async def close_session(self, session_id: str):
        """Close a terminal session"""
        session = self.sessions.pop(session_id, None)
        if not session:
            return
        session['active'] = False
        try:
            if session['process'].isalive():
                session['process'].terminate(force=True)
            logger.info("Terminal session %s closed", session_id)
        except Exception:
            logger.error("Error closing terminal session %s", session_id)


# Global terminal manager instance
terminal_manager = TerminalManager()


async def _close(websocket: WebSocket, code: int):
    try:
        await websocket.close(code=code)
    except Exception:
        pass


async def handle_terminal_websocket(websocket: WebSocket, *, authorized):
    """Serve an accepted, authenticated socket until it closes or authorization lapses.

    ``authorized`` is an async callable answering whether the socket's
    credential may still use the terminal. It runs before each input batch and
    every RECHECK_SECONDS; a False answer closes the socket with 1008.
    """
    session_id = str(uuid.uuid4())
    if not await terminal_manager.create_session(websocket, session_id):
        await _close(websocket, 1011)
        return
    logger.info("Terminal WebSocket connected: %s", session_id)

    async def watch():
        while True:
            await asyncio.sleep(RECHECK_SECONDS)
            if not await authorized():
                return

    watcher = asyncio.create_task(watch())
    checked_at = time.monotonic()
    try:
        while True:
            receiver = asyncio.ensure_future(websocket.receive_text())
            done, _ = await asyncio.wait({receiver, watcher}, return_when=asyncio.FIRST_COMPLETED)
            if watcher in done:
                receiver.cancel()
                await _close(websocket, POLICY_VIOLATION)
                return
            try:
                message = receiver.result()
            except WebSocketDisconnect:
                logger.info("Terminal WebSocket disconnected: %s", session_id)
                return
            try:
                data = json.loads(message)
            except ValueError:
                continue
            kind = data.get('type') if isinstance(data, dict) else None
            if kind == 'input' and isinstance(data.get('data'), str):
                if time.monotonic() - checked_at > BATCH_SECONDS:
                    if not await authorized():
                        await _close(websocket, POLICY_VIOLATION)
                        return
                    checked_at = time.monotonic()
                await terminal_manager.send_input(session_id, data['data'])
            elif kind == 'resize':
                cols, rows = data.get('cols'), data.get('rows')
                if type(cols) is int and type(rows) is int and 0 < cols <= 1000 and 0 < rows <= 1000:
                    await terminal_manager.resize_terminal(session_id, cols, rows)
            elif kind == 'ping':
                await websocket.send_text(json.dumps({'type': 'pong'}))
    except Exception:
        logger.error("Terminal WebSocket failed: %s", session_id)
    finally:
        watcher.cancel()
        await terminal_manager.close_session(session_id)
        await _close(websocket, 1000)


def check_terminal_requirements():
    """Check if terminal requirements are met"""
    issues = []
    shell = os.environ.get('SHELL', '/bin/bash')
    if not os.path.exists(shell) and not os.path.exists('/bin/sh'):
        issues.append("No shell available (bash or sh)")
    return issues
