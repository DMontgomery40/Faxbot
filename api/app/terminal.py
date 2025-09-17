"""
WebSocket Terminal Handler for Admin Console

Provides a direct TTY terminal interface via WebSocket.
Requires admin authentication but no additional login.
"""
import os
import asyncio
import json
import logging
from typing import Optional
from fastapi import WebSocket, WebSocketDisconnect, HTTPException, Depends
import pexpect
import sys
import subprocess
import shutil

logger = logging.getLogger(__name__)

class TerminalManager:
    """Manages terminal sessions for WebSocket connections"""
    
    def __init__(self):
        self.sessions = {}
    
    async def create_session(self, websocket: WebSocket, session_id: str):
        """Create a new terminal session"""
        try:
            # Check if we're in a Docker container
            is_docker = os.path.exists('/.dockerenv') or os.environ.get('DOCKER_CONTAINER') == 'true'
            
            # Get the shell to use (prefer bash, fallback to sh)
            shell = os.environ.get('SHELL', '/bin/bash')
            if not os.path.exists(shell):
                shell = '/bin/sh'
            
            # Get terminal dimensions from client or use defaults
            cols = 80
            rows = 24
            
            # Spawn a pseudo-terminal
            logger.info(f"Starting terminal session {session_id} with shell: {shell}")
            
            # Set environment variables for better terminal experience
            env = os.environ.copy()
            env['TERM'] = 'xterm-256color'
            env['PS1'] = r'\[\033[01;32m\]\u@faxbot\[\033[00m\]:\[\033[01;34m\]\w\[\033[00m\]\$ '
            
            # Spawn the terminal process
            # Force interactive mode when possible to ensure prompt + echo
            args = []
            try:
                base = os.path.basename(shell)
                if base in ('bash', 'sh', 'zsh', 'ash', 'dash'):
                    args = ['-i']
            except Exception:
                args = []

            process = pexpect.spawn(
                shell,
                args=args,
                env=env,
                encoding='utf-8',
                timeout=None,
                dimensions=(rows, cols)
            )
            
            # Store the session
            self.sessions[session_id] = {
                'process': process,
                'websocket': websocket,
                'active': True
            }
            
            # Send initial prompt
            await websocket.send_text(json.dumps({
                'type': 'output',
                'data': f"Faxbot Terminal - {shell}\r\n"
            }))
            # Ensure sane TTY settings (echo enabled, line mode)
            try:
                process.sendline('stty sane 2>/dev/null || true')
            except Exception:
                pass
            
            # Start reading from the terminal
            asyncio.create_task(self._read_terminal_output(session_id))
            
            logger.info(f"Terminal session {session_id} created successfully")
            return True
            
        except Exception as e:
            logger.error(f"Failed to create terminal session: {e}")
            await websocket.send_text(json.dumps({
                'type': 'error',
                'message': f'Failed to start terminal: {str(e)}'
            }))
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
                    # Read available output from the terminal
                    output = process.read_nonblocking(size=1024, timeout=0.1)
                    if output:
                        await websocket.send_text(json.dumps({
                            'type': 'output',
                            'data': output
                        }))
                except pexpect.TIMEOUT:
                    # No output available, continue
                    await asyncio.sleep(0.01)
                except pexpect.EOF:
                    # Terminal process ended
                    logger.info(f"Terminal session {session_id} process ended")
                    await websocket.send_text(json.dumps({
                        'type': 'exit',
                        'message': 'Terminal process ended'
                    }))
                    break
                except Exception as e:
                    if session['active']:
                        logger.error(f"Error reading terminal output: {e}")
                    break
        
        except Exception as e:
            logger.error(f"Fatal error in terminal output reader: {e}")
        
        finally:
            await self.close_session(session_id)
    
    async def send_input(self, session_id: str, data: str):
        """Send input to the terminal"""
        session = self.sessions.get(session_id)
        if not session or not session['active']:
            return False
        
        try:
            process = session['process']
            # Debug: trace a small sample of input
            try:
                sample = data.replace("\r", "\\r").replace("\n", "\\n")
                logger.info(f"terminal input [{len(data)}]: {sample[:40]}")
            except Exception:
                pass
            process.send(data)
            return True
        except Exception as e:
            logger.error(f"Error sending input to terminal: {e}")
            return False
    
    async def resize_terminal(self, session_id: str, cols: int, rows: int):
        """Resize the terminal window"""
        session = self.sessions.get(session_id)
        if not session or not session['active']:
            return False
        
        try:
            process = session['process']
            try:
                process.setwinsize(rows, cols)
            except Exception:
                # pexpect may raise if the child ended; ignore silently
                return False
            return True
        except Exception as e:
            logger.error(f"Error resizing terminal: {e}")
            return False
    
    async def close_session(self, session_id: str):
        """Close a terminal session"""
        session = self.sessions.get(session_id)
        if not session:
            return
        
        try:
            session['active'] = False
            
            # Terminate the process
            if session['process'].isalive():
                session['process'].terminate(force=True)
            
            # Remove from sessions
            del self.sessions[session_id]
            
            logger.info(f"Terminal session {session_id} closed")
        except Exception as e:
            logger.error(f"Error closing terminal session: {e}")

# Global terminal manager instance
terminal_manager = TerminalManager()

async def handle_terminal_websocket(websocket: WebSocket):
    """Handle WebSocket connection for terminal"""
    import uuid
    session_id = str(uuid.uuid4())
    
    try:
        # Accept the WebSocket connection
        await websocket.accept()
        
        logger.info(f"Terminal WebSocket connected: {session_id}")
        
        # Create terminal session
        success = await terminal_manager.create_session(websocket, session_id)
        if not success:
            await websocket.close()
            return
        
        # Handle incoming messages
        while True:
            try:
                message = await websocket.receive_text()
                data = json.loads(message)
                
                if data['type'] == 'input':
                    # Send input to terminal
                    await terminal_manager.send_input(session_id, data['data'])
                
                elif data['type'] == 'resize':
                    # Resize terminal
                    await terminal_manager.resize_terminal(
                        session_id,
                        data['cols'],
                        data['rows']
                    )
                
                elif data['type'] == 'ping':
                    # Keepalive ping
                    await websocket.send_text(json.dumps({'type': 'pong'}))
                
            except WebSocketDisconnect:
                logger.info(f"Terminal WebSocket disconnected: {session_id}")
                break
            except json.JSONDecodeError as e:
                logger.error(f"Invalid JSON from terminal client: {e}")
                await websocket.send_text(json.dumps({
                    'type': 'error',
                    'message': 'Invalid message format'
                }))
            except Exception as e:
                logger.error(f"Error handling terminal message: {e}")
                break
    
    finally:
        # Clean up session
        await terminal_manager.close_session(session_id)
        try:
            await websocket.close()
        except:
            pass

def check_terminal_requirements():
    """Check if terminal requirements are met"""
    issues = []
    
    # Check for shell
    shell = os.environ.get('SHELL', '/bin/bash')
    if not os.path.exists(shell) and not os.path.exists('/bin/sh'):
        issues.append("No shell available (bash or sh)")
    
    # Check if pexpect is available
    try:
        import pexpect
    except ImportError:
        issues.append("pexpect module not installed")
    
    return issues
