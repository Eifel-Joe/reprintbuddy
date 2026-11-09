# Troubleshooting

When something is wrong, Reprintbuddy says so in plain words. This page lists the warnings, what they mean and what to do, and what to do when an update fails or you are locked out.

## Where warnings appear

The warnings are on the *Overview* and on the *Queue* page, each as a card. Under them stands **Last successful query of Bambuddy: …** (or "Bambuddy has not been queried successfully yet."). The button **Query Bambuddy now** asks Bambuddy at once instead of waiting for the next regular query; most warnings go away by themselves at the next query that succeeds.

A **red** card means that Bambuddy does not answer or refuses, that a hand-over is unclear, or that a push message did not go through. A **yellow** card is a state that you chose or that waits for you.

## Red warnings

| Warning | What it means | What to do |
|---|---|---|
| Bambuddy cannot be reached. Nothing is handed over and no stock changes. | The last query got no answer: Bambuddy is down, its address changed, or the network is cut. | Check that Bambuddy runs and that the Reprintbuddy system can reach it. Check the address under *Settings → Bambuddy*. When the next query succeeds, the warning goes. |
| Bambuddy refuses the status query (`…`). Finished prints are not booked until this is fixed. | Bambuddy answered, but refused the regular query. The text in brackets is Bambuddy's reason, for example a missing right, or a key that was deleted. | Under *Settings → Bambuddy* press **Check and save** (an empty key field keeps the stored key). It lists the owner of the key and each missing right. Fix it as described in [First setup](../start/setup.md#missing-rights-later). |
| Bambuddy refused a print of `…` (`…`). It is back in the work queue; the next regular query tries again. | Bambuddy did not take a job that Reprintbuddy handed over. The text in brackets is Bambuddy's reason. | Read the reason and fix what it names, for example the print target or the print file of that article in Bambuddy. The next regular query tries again. |
| `n` hand-over(s) unclear – nothing new is handed over until they are clear. | Reprintbuddy sent a job, but did not get a clear answer (timeout, connection lost). It never sends such a job again blindly. | Wait for the next regular query: Reprintbuddy looks for its entry in Bambuddy's queue. If it finds exactly one, it carries on by itself. If it finds none, the article is paused ("a hand-over to Bambuddy is unclear", see below); if it finds several, you get the warning about several entries. If the search itself fails, you get one of the two warnings after this one. |
| The search for an unclear hand-over failed (`…`). Nothing new is handed over until it is clear. | The search in Bambuddy's queue was refused. | Fix the connection to Bambuddy as for "Bambuddy refuses the status query". |
| Reprintbuddy does not know its Bambuddy user, so it cannot look for an unclear hand-over. Nothing new is handed over until it is clear. Check the connection to Bambuddy in the settings. | The name of the key's owner is not stored. | Under *Settings → Bambuddy* press **Check and save**. |
| An unclear hand-over cannot be matched for certain: Bambuddy has several entries that could be it (entries `…`). None was taken over; the article is paused. Check Bambuddy and delete the extra entries; if the print ran, book it with stock-in without print. | Several entries in Bambuddy's queue look like the lost one (same user, file and plate, created after sending). Reprintbuddy refuses to guess. | In Bambuddy delete the extra entries (Reprintbuddy never changes entries in Bambuddy). If the print ran, book the pieces on the *Bookings* page with **Stock in without print**. Then lift the pause of the article. |
| Home Assistant not reachable – the last push message did not go through: `reason` | The last push message was not accepted. The reason says why: token refused (401), service unknown or data wrong (400), sender blocked (403), error in Home Assistant (5xx), redirected to another address, not reachable, or unclear (no answer in time). | Check the address, the token and the service under *Settings → Notifications* and send a test message. The warning goes when a message is accepted, when Home Assistant answers again at a regular query, or when the messages are switched off. Stock and printing go on as before. See [Push messages](../use/notifications.md). |

## Yellow warnings

| Warning | What it means | What to do |
|---|---|---|
| Bambuddy is not set up yet. Reprintbuddy queues reprints but hands nothing over. | No Bambuddy connection is stored. | Connect Bambuddy, see [First setup](../start/setup.md). |
| Bambuddy reports a state Reprintbuddy does not know (`…`). Those prints stay as they are. | A print has a state that this version of Reprintbuddy does not know, for example because Bambuddy is newer. | Look at those prints in Bambuddy. Reprintbuddy leaves them as they are. If Bambuddy is newer than your Reprintbuddy, update Reprintbuddy (see [Update and backup](../operate/update-backup.md)). |
| `n` print(s) wait for approval in Bambuddy. Start them in Bambuddy. So that later prints do not wait, the group of the user who owns Reprintbuddy's API key needs the right “Print Without Review” (queue:start_unreviewed) or “Update All Queue” (queue:update_all) there. | Prints that Reprintbuddy handed over are held in Bambuddy until someone starts them. | Start them in Bambuddy. Give the group `Reprintbuddy` the right **Print Without Review**, see [First setup](../start/setup.md). |
| The automation stopped with an error (`…`). It tries again with the next run. | An unexpected error in the automation run. | Wait for the next run. If the warning stays, look at the system log (`journalctl -u reprintbuddy`) and report it as an issue on [GitHub](https://github.com/Eifel-Joe/reprintbuddy/issues), without keys and passwords. |
| An extension failed during the last query: `filament`. | The part that keeps the daily record of the filament stock and checks for delivered spools failed in the last regular query. | Check that Bambuddy (and Spoolman, if you use it) can be reached and shows your spools. The next query tries again. See [Filament](../use/filament.md). |
| An update is running. The automation waits until it is finished. | An update is in progress; every change is paused until it is done. | Wait. See [Update and backup](../operate/update-backup.md). |
| The work queue is stopped. Reprints are queued but not handed over. | The switch **Stop the queue (nothing is handed over to Bambuddy)** is on. | Switch it off under *Settings → Automation* when you want Reprintbuddy to hand over again. See [Automation](../use/automation.md). |
| Limit of `n` open prints in Bambuddy reached; the rest waits in Reprintbuddy. | Reprintbuddy has as many prints open in Bambuddy (queued or printing) as the limit allows. This is normal. | Nothing; the next entry is handed over when a print finishes. To allow more, raise **Limit of open prints in Bambuddy** under *Settings → Automation*. |
| Waiting for a free printer (maintenance in Bambuddy): `articles`. | The print target of these articles is the printer of the print target is in maintenance mode, or all printers of its model are in Bambuddy. | End the maintenance mode in Bambuddy, or choose another print target for the article. The article is not paused. |

## Paused articles

A paused article has its own card: the name of the article, then the reason. While an article is paused, the automation creates and hands over nothing for it. Each card offers the way on:

| Reason shown | What happened | What to do |
|---|---|---|
| Paused: a reprint entry was reduced or deleted. Lower the minimum stock or buffer if you want fewer. | You reduced or deleted an entry that the automation made in Reprintbuddy's queue. Otherwise the rule would create it again at once. | For less in the long run, lower the minimum stock or the buffer (**Edit settings**). Then press **Lift pause**. |
| Paused: a print by Reprintbuddy was cancelled in Bambuddy. | You cancelled a print of this article in Bambuddy. The pause keeps Reprintbuddy from working against you. | Press **Lift pause** when the automation should carry on. |
| Paused: a print by Reprintbuddy is gone from Bambuddy (deleted, or the history was cleared). If it finished, book it with stock-in without print. | Bambuddy no longer knows a print that Reprintbuddy handed over. | If it finished, book the pieces on the *Bookings* page with **Stock in without print**. Then press **Lift pause**. |
| Paused: two prints in a row failed. | Two prints of this article failed one after the other. | Look for the cause in Bambuddy (printer, file, filament). Then press **Lift pause**; this also resets the failure counter. |
| Paused: the print file was changed in Bambuddy. Confirm it or choose it again. | The checksum or the name of the print file differs from what Reprintbuddy stored. | **Confirm file** (or **Confirm print file** on the article page) takes the new version as it is now, including its plates and filament. **Choose another file** (**Choose print file again**) picks a file again. If material or colour changed, tick the spool sorts of the named slots again. The pause is lifted by either. Files in external folders are not checked, see [Limits](limits.md). |
| Paused: the print file is gone from Bambuddy. Choose a file again. | The file was deleted in Bambuddy. | **Choose another file** and pick a new file and plate. This also lifts the pause. |
| Paused: the article has no print target. Set one. | The article has no printer model or printer, or the printer was removed from Bambuddy. | **Edit settings** and set a print target. **Lift pause** is not possible before. |
| Paused: a hand-over to Bambuddy is unclear. Check Bambuddy's queue and history; if the print ran, book it with stock-in without print. | Reprintbuddy could not find out whether Bambuddy took a job. | Look in Bambuddy's queue and history. If the print ran, book the pieces with **Stock in without print**. Then press **Lift pause**. |

**Open article** takes you to the article, where the same buttons are in the section **Automation paused**.

## Other notices

- **Bambuddy right missing** (a dialog, or a warning line at the top after **Later**): the stored key lacks a right that this version needs. See [First setup](../start/setup.md#missing-rights-later).
- **Filament not assigned** (on the article), or "These filament slots have no ticked spool sort yet, so their demand is not counted" (on the filament page): a filament slot of the article has no spool sort ticked. Assign it on the article, see [Filament](../use/filament.md). Such a slot does not count for stock and demand.
- **Stock as of …  (Bambuddy cannot be reached)**: the filament numbers are the last known ones, because Bambuddy cannot be reached right now.

## An update failed or was interrupted

Under *Settings → Update and backup* the update card names the state and the step that failed. Details are in the log of the update: `journalctl -u reprintbuddy-update`.

- **Update failed, nothing was changed** and **Update failed, previous version restored:** the running version is as before, and the database was restored if needed. Fix the cause named on the page (often the network: GitHub, PyPI) and start the update again.
- **Update was interrupted** or **Update failed and the previous version could not be restored:** run the installer again as root (the command from [Installation](../start/install.md); if it stops because it does not match the installed version, use the tag it names). It repairs packages, units and the service and keeps the database. If Reprintbuddy still does not start, restore the backup named on the page and run the installer again. Restoring is described under [Update and backup](../operate/update-backup.md); without a working interface use the command `reprintbuddy restore-backup` ([Command line](../operate/cli.md)).

## Locked out

If you cannot sign in or reach the page, use the command line on the Reprintbuddy system. All commands are described under [Command line](../operate/cli.md).

- **Forgotten password or user name:** `reprintbuddy set-password --username <name>` sets a new password and renames the account to the given name.
- **Wrong port or listen address:** `reprintbuddy set-port 8080` or `reprintbuddy set-host 0.0.0.0`, then `systemctl restart reprintbuddy`.
- **A certificate that stopped working** (expired, wrong name): `reprintbuddy disable-https && systemctl restart reprintbuddy`.

See also [Network and security](../operate/network.md).
