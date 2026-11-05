"""Operations: restart, update, backup, export. No web dependency.

Every call of `git`, `pip` or `sudo` goes through a command runner that is passed in,
so tests replace it by a fake and never touch the network, git remotes or systemd.
"""
