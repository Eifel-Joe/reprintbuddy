# Network and security

Reprintbuddy is meant for your home network. This page says how it should be reached, how to set the address, the port and HTTPS, how to run it behind a reverse proxy, what to do when you lock yourself out, and how your keys and data are protected.

## Home network and VPN

Plain HTTP is fine inside your home network and over a VPN. Reprintbuddy needs nothing more.

!!! warning "Never expose Reprintbuddy directly to the internet"
    No port forwarding, no public reverse proxy. For access from outside, use a VPN into your network, for example your router's VPN, WireGuard or Tailscale. Over the VPN it behaves as in your own network.

## Address and port

Under *Settings → General*, the card **Server** has two fields:

- **Listen address**: the address Reprintbuddy listens on. The default `0.0.0.0` means every address of the machine. Reprintbuddy only saves an address this machine can really listen on, because a wrong one would lock you out after the restart.
- **Port**: the port of the web interface, 1 to 65535, default `8080`. Ports below 1024 work with the service set up by the installer; a service started another way may not be allowed to use them.

Changes to address, port and HTTPS take effect **after a restart**, see [Restart](update-backup.md#restart). Until then the page keeps saying so. After the restart, the restart page names the new address; open Reprintbuddy there and update your bookmarks.

A printed QR code keeps the address it had when it was printed. After you change the address or the port, print the labels again. Reprintbuddy finds its own network address for the codes without sending anything; if it is reached through a name or a proxy, enter that address under *Settings → General → QR codes*, see [Labels and QR codes](../use/labels.md).

## HTTPS

HTTPS is optional. You can give Reprintbuddy a certificate itself, or run it behind a reverse proxy that does TLS.

To use a certificate:

1. Open *Settings → General*, card **Server**, section **HTTPS (optional)**.
2. Paste the certificate into **Certificate (PEM)** and the key into **Private key (PEM)**, or choose the files. Each is at most 64 KB.
3. Press **Save**. Reprintbuddy checks that certificate and key belong together and can be loaded. If not, nothing is saved and the page says why. It does not check the expiry date or the name in the certificate.
4. Restart Reprintbuddy. It then answers with HTTPS.

Both parts are needed, and the private key must not have a password ("Password-protected private keys are not supported."). Certificate and key are stored in the database. The key is never shown again: the field shows `********`, and leaving it empty keeps the stored key.

To go back to HTTP, tick **Remove certificate and private key (serve plain HTTP)**, press **Save** and restart. The tick box is shown only while a certificate is stored.

## Reverse proxy

You can also run Reprintbuddy behind a reverse proxy you already have. Reprintbuddy stays on HTTP and the proxy does TLS.

- If the proxy runs on the **same system**, set the listen address to `127.0.0.1`, so Reprintbuddy cannot be reached around the proxy.
- Give Reprintbuddy **its own address or its own port** on the proxy, for example `https://reprintbuddy.example.net/` or `https://example.net:8443/`.
- A **sub-path** such as `https://example.net/reprintbuddy/` does **not** work: links and redirects would point past the proxy. Reprintbuddy writes a warning to the system log, once, when it is reached that way (a root path or an `X-Forwarded-Prefix` header). Read the system log with `journalctl -u reprintbuddy`.
- Pages update themselves over a stream at `/live` (server-sent events). The proxy must pass it on without buffering; Reprintbuddy sends the header `X-Accel-Buffering: no`, which nginx respects.
- Enter the address of the proxy under *Settings → General → QR codes*, so that the QR codes carry it.

## First setup and names from public domains

Until the admin account exists, anyone who reaches the page could create it. A web page on the internet could make a visitor's browser talk to your network by pointing its own name at a private address (DNS rebinding). So during the first setup Reprintbuddy accepts only these addresses in the browser's address bar:

- an IP address (with or without a port),
- `localhost`,
- a name without a dot,
- a name ending in `.local`, `.lan`, `.home`, `.internal`, `.localdomain` or `.home.arpa`.

Any other name, for example a name from a public domain such as a VPN DNS name, gets the page "Address not allowed": "While Reprintbuddy is being set up, open it by its IP address …". Open Reprintbuddy by its IP address, finish the setup, and use the name afterwards. This applies to a name on a reverse proxy too: set up through the IP address first.

## If you are locked out

A certificate that stopped working (expired, wrong name), a wrong port or a wrong listen address can lock you out. On the system, as root:

```bash
reprintbuddy disable-https && systemctl restart reprintbuddy
reprintbuddy set-port 8080 && systemctl restart reprintbuddy
reprintbuddy set-host 0.0.0.0 && systemctl restart reprintbuddy
```

The first turns HTTPS off, the second sets the port, the third the listen address. Use the one you need. See [Command line](cli.md) for all commands. A forgotten password is fixed there too.

## Security

- **Not on the internet.** See above: no port forwarding, no public reverse proxy; use a VPN.
- **Keys, passwords and tokens.** API keys, the private key of an HTTPS certificate, the Home Assistant token and passwords are visible only while you type them. Once saved, the interface shows `********`. An empty field keeps the stored value, a new entry replaces it. Passwords are stored only as hashes.
- **Home Assistant token.** It acts with all rights of its Home Assistant user, so create it for a user without admin rights. It goes only to the address it was entered with, path included; for a new address, enter it again. Push messages to a phone pass through the push service of Google or Apple, and they name articles, spool sorts and parts as long as **Name things in messages** is on.
- **Every change needs a session and a token.** A change is accepted only from a signed-in session and with a CSRF token.
- **The database file** is readable only by the service user. Anyone who is root on the machine can read it. The same goes for the copies in `backups/` and for a full backup without a password, which hold the keys.

Report vulnerabilities as described in [SECURITY.md](https://github.com/Eifel-Joe/reprintbuddy/blob/main/SECURITY.md): privately through GitHub, never as a public issue. Only the latest release receives security fixes.

## Related pages

- [Settings](settings.md): all settings.
- [Update and backup](update-backup.md): restart, backups.
- [Command line](cli.md): emergency commands.
