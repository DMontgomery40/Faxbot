# Admin Console Terminal

The Admin Console includes a built‑in terminal that provides direct shell access to the Faxbot container or server environment via a secure WebSocket.

## Features

- Full TTY with `xterm-256color`, history, and standard shortcuts
- Opens for the Owner role by default. You can grant `host:terminal` through a custom role.
- Container‑aware; works in Docker and local dev

!!! tip
    Default posture is local‑only. Keep it that way unless you fully trust the network path.

## Install

- Docker images include required deps. For manual installs:

```bash
./scripts/install-terminal-deps.sh
```

## Turn it on

The terminal is off unless the installation allows host commands. Set `ENABLE_ADMIN_EXEC=true` in the deployment environment. If `ENABLE_ADMIN_EXEC` is not set, the terminal follows `ENABLE_LOCAL_ADMIN`. When it is off, or the terminal's dependencies are missing, the console reports that the terminal is not available on this server.

## Who can use it

Using the terminal needs the `host:terminal` permission. The built-in **Owner** role includes it; **Host Operator** and **Administrator** do not. You can grant it through a custom role on the **Access** screen. See [Access Control](security/access-control.md).

## Usage

1. Sign in to the admin console.
2. Open **Tools → Terminal** and start typing.

The console first asks the server for a one-time ticket that is valid for 60 seconds, then opens the terminal connection with it. While the terminal is open, Faxbot keeps checking that you still have `host:terminal` and that host commands are still allowed. If either changes, for example because your role was removed or your session ended, the terminal closes within a few seconds.

## Security

- Opening and closing the terminal is recorded on the **Logs** screen. What you type is never logged.
- The terminal runs with the same privileges as the API service.

!!! warning
    Avoid exposing the Terminal over public tunnels or shared networks. Keep SIP/AMI and internal interfaces private.

## Troubleshooting

- If the console says the terminal is not available, check `ENABLE_ADMIN_EXEC` and run `./scripts/install-terminal-deps.sh` on manual installs.
- If the terminal closes right away, check that your account still has the `host:terminal` permission and that your session has not ended.
- If it never connects, check that the API is running and look at the browser console.
