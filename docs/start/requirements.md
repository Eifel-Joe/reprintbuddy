# Requirements

What you need before you install Reprintbuddy: a Bambuddy that fits, print files in the right place, a system to run it on, and a few network connections.

## Bambuddy

- **Bambuddy 1.3 or newer.** Bambuddy can keep the spools itself (its own spool management) or in Spoolman. **Spoolman is optional.** Reprintbuddy reads the filament stock through Bambuddy and never talks to Spoolman itself.
- **Print files stored directly in Bambuddy's file manager**, uploaded and sliced (`.3mf` or `.gcode.3mf`).

    !!! warning "Files in linked external folders are not enough"
        For files from a folder that is linked into Bambuddy from outside (an external folder), Bambuddy keeps no checksum, and when it rescans such a folder it does not re-read plates and filament. Reprintbuddy would not notice a new version of such a file. Use files that lie directly in Bambuddy's own file manager. More under [Limits](../help/limits.md).

    You can also upload new print files from Reprintbuddy (optional, see [Articles](../use/articles.md)), but not into an external folder.
- **A dedicated Bambuddy user and group.** Reprintbuddy needs a user `reprintbuddy` in its own Bambuddy group `Reprintbuddy`, with the rights listed in [First setup](setup.md), and an **API key** of that user. The own user makes Reprintbuddy's jobs recognisable in Bambuddy. The setup guides you through it step by step.

## The system for Reprintbuddy

- A computer, a VM or a container (for example a Proxmox LXC, unprivileged is fine) with **Debian 13** or newer. The installer refuses other systems.
- One CPU core, 1 GB RAM and 8 GB disk are enough.
- No Docker.

Reprintbuddy needs Python 3.13 or newer. Debian 13 brings it; Debian 12 only has Python 3.11, so the installer refuses to run there. How to move an older system to Debian 13 without losing anything is described on [Installation](install.md).

## Network

- **Outgoing:** from the Reprintbuddy system to the Debian package sources (`apt`), `github.com` and `raw.githubusercontent.com` (installation and updates), `api.github.com` (the release notes on the update card), `pypi.org` and `files.pythonhosted.org` (Python packages, at installation and at every update), and to your Bambuddy.
- **Incoming:** the browsers you use must reach Reprintbuddy on its port (8080 by default). Do not expose it to the internet; for remote access use a VPN. See [Network and security](../operate/network.md).

## Optional: Home Assistant

For push messages to your phone, Reprintbuddy can report to [Home Assistant](https://www.home-assistant.io/) and its phone app. Then Reprintbuddy also needs access to Home Assistant. Without it nothing is sent and Home Assistant is never contacted. See [Push messages](../use/notifications.md).
