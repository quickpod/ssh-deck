# SSHDeck

A fast, **offline**, **100% open-source** SSH & SFTP client for Windows. Nothing is uploaded anywhere. Built entirely by AI with human testing and guidance, and published on [QuickOpen](https://quickopen.ai/projects/ssh-deck).

> **100% AI-built and open source.** Apache-2.0.

## What it does

Connect to servers over SSH with saved session profiles and key management, run an interactive shell, transfer files with a dual-pane SFTP browser, and set up local/remote port forwards. A friendly PuTTY/Termius alternative that keeps all credentials on your machine.

## Install

Download **`SSHDeck-Setup.exe`** from the [QuickOpen page](https://quickopen.ai/projects/ssh-deck) or the [GitHub release](https://github.com/quickpod/ssh-deck/releases/latest) and double-click it. It installs per-user, adds Desktop and Start Menu shortcuts, and can optionally trust the QuickOpen Root CA. Authenticode-signed by the QuickOpen Code Signing CA — verify at [quickopen.ai/trust](https://quickopen.ai/trust).

## Run from source

```sh
pip install -r requirements.txt
python ssh_deck_app.py          # GUI
python -m sshdeck --help    # CLI
```


## Features

- **Session profiles** — save host, port, user, auth method (key / password / agent), private-key path and an optional jump host. Profiles are plain JSON at `%LOCALAPPDATA%\SSHDeck\config.json`. **No secret is ever written to disk:** passwords and key passphrases are prompted for at connect time.
- **Interactive terminal** — a real login shell over the connection, in a threaded read loop so the UI never freezes.
- **Dual-pane SFTP browser** — local files on the left, remote on the right; upload, download, make folders and delete, with background transfers.
- **Key management** — generate `ed25519` / `rsa` / `ecdsa` key pairs (optionally passphrase-encrypted), and copy the `authorized_keys` public line to the clipboard.
- **Port forwards** — describe and set up local (`-L`) and remote (`-R`) tunnels over the current connection.
- **Jump hosts** — connect through a bastion with an OpenSSH-style `ProxyJump` (`user@bastion:22`).
- **Clean errors everywhere** — auth rejections, unreachable hosts and bad input surface as a one-line message, never a traceback. Dark mode included.

Everything the GUI does is available on the command line, and the core logic (`sshdeck` package) is a small, importable library.

## CLI examples

```sh
# Saved sessions (host/user/key only — never a password)
python -m sshdeck session add web example.com --user deploy --auth key --key ~/.ssh/id_ed25519
python -m sshdeck session add db 203.0.113.5 --user admin --auth password --jump ops@bastion:22
python -m sshdeck session list
python -m sshdeck session remove web

# Run a command (prompts for a password/passphrase only if the session needs one)
python -m sshdeck run web "uptime"

# SFTP: list, download, upload, mkdir, remove
python -m sshdeck sftp web ls /var/log
python -m sshdeck sftp web get /etc/hostname ./hostname
python -m sshdeck sftp web put ./deploy.tar.gz /tmp/deploy.tar.gz

# Keys
python -m sshdeck keygen --type ed25519 --out ~/.ssh/id_ed25519 --comment me@laptop
python -m sshdeck pubkey ~/.ssh/id_ed25519

# Describe a port-forward spec
python -m sshdeck forward 8080:localhost:80
python -m sshdeck forward 0.0.0.0:5432:db.internal:5432 --kind remote
```

## License

Apache-2.0 — see [LICENSE](LICENSE). A 100% AI-built project published on QuickOpen.
