# Limits

What Reprintbuddy deliberately does not do, and where it stops. Most of these are decisions, not gaps: Reprintbuddy keeps stock and hands jobs to Bambuddy, and Bambuddy stays your print system.

## Print files must lie directly in Bambuddy's file manager

Reprintbuddy works reliably only with print files stored **directly in Bambuddy's own file manager**.

!!! warning "Files from linked external folders are not safe to use"
    For files from a folder that is linked into Bambuddy from outside (an external folder), Bambuddy keeps no checksum. When Bambuddy rescans such a folder, it only refreshes the file's modification date, not the plates and the filament. Reprintbuddy would not notice a new version of such a file.

How Reprintbuddy notices a changed print file: it compares the checksum that Bambuddy keeps for the file with the one it stored when you chose the file. If they differ, or the file has another name, Reprintbuddy shows the warning, pauses the automation for that article and waits until you confirm the file or choose it again (see [Troubleshooting](troubleshooting.md)). For a file from an external folder there is no checksum, so:

- A new version of the file under the same name goes **unnoticed**: no warning, no pause. The article keeps printing, with whatever the new file contains. The plates and the filament shown in Reprintbuddy can be out of date as well.
- Only a file that is renamed or gone is still noticed.
- Reprintbuddy does not stop you from choosing such a file, but it warns you: the folder list and the plate page show a note while you choose, also when you change the print file of an article.
- You cannot upload into an external folder: the **Upload print file** button is missing there, and an attempt is refused before anything is sent to Bambuddy.

To be safe, keep your print files in Bambuddy's own file manager. If an article uses a file from an external folder, upload or move the file into Bambuddy's file manager and choose it for the article with **Change print file** (see [Articles](../use/articles.md)).

## What Reprintbuddy never does in Bambuddy

- **It never controls a printer.** No start, stop, pause or printer command. Which printer prints when is Bambuddy's decision.
- **It adds entries to Bambuddy's queue, and nothing else there.** If you switch on **Insert as next print** (*Settings → Automation*), the entry is placed ahead of the prints that do not come from Reprintbuddy when it is created; a running print is never interrupted. See [Automation](../use/automation.md).
- **It never changes, reorders, takes back or deletes** entries that it handed over. What lies in Bambuddy belongs to Bambuddy; reorder, move up or delete there.
- **It does not change, replace, move or delete existing files or folders** in Bambuddy. The only thing it writes into Bambuddy's file manager is a **new print file** that you upload from Reprintbuddy, never into an existing name. It does not create folders, and it cannot delete a file it has uploaded; that is done in Bambuddy. Because of this, it checks a file before it sends anything (see [Articles](../use/articles.md)).
- **It never writes to Spoolman**, and does not talk to Spoolman at all. It reads the filament stock through Bambuddy.

## What Reprintbuddy does not know

- **Prints you start directly in Bambuddy.** Reprintbuddy only knows the jobs it handed over itself. Book such pieces in by hand on the *Bookings* page with **Stock in without print**.
- **Which spool Bambuddy takes** for a print. Reprintbuddy counts the filament by spool sort (same brand, material, subtype and colour), not by single spool. It only reads; it never changes the filament stock. See [Filament](../use/filament.md).
- **Whether a push message reached your phone.** "Accepted" only means that Home Assistant took it. See [Push messages](../use/notifications.md).

## Articles and plates

- An article has **exactly one plate** of one print file. A product made of several different plates is not represented as one article.
- A plate belongs to **at most one article**. Reprintbuddy refuses a second article on the same plate ("This plate already belongs to the article …").
- A set or an article made of parts has **at most 100 variants**. See [Sets](../use/inspection.md#sets) and [Assembled articles](../use/assembled.md).
- Article numbers are compared **without regard to case**. Two numbers that differ only in case and come from an earlier version stay as they are and can still be ordered; one of the two can be saved again only after you rename one.
- An article, a set or an article made of parts **needs an article number** to be saved. Ones you made in an earlier version without a number keep working (ordering, printing, stock); the number is asked for the next time you save them.

## Labels

Labels are printed on a Brother label printer in your network, on the 62 × 29 mm roll (DK-11209), always landscape. See [Labels and QR codes](../use/labels.md).

## Accounts, languages, processes

- **One account.** There is a single user, the admin. There are no roles and no second sign-in.
- **English and German** are the only interface languages.
- **One process.** Reprintbuddy is built to run as one server process; it does not support several copies working on one database.

## Network

- **Do not expose Reprintbuddy to the internet.** No port forwarding, no public reverse proxy. For remote access use a VPN into your network. See [Network and security](../operate/network.md).
- **No sub-path behind a reverse proxy.** Reprintbuddy works behind a proxy only under its own address or its own port (`https://reprintbuddy.example.net/` or `https://example.net:8443/`), not under `https://example.net/reprintbuddy/`. Links and redirects would point past the proxy, and Reprintbuddy writes a warning to the system log when it is reached that way.
