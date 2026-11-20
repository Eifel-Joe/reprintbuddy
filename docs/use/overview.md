# Overview

The **Overview** is the start page. It shows at a glance whether Reprintbuddy can work, how things stand and what is due, and what the printers do: four tiles with small pictures, each leading to the page where you do it, and below them a print plan per printer. No lists per article. Like every page it stays up to date by itself: a booking on another device, a finished print in Bambuddy or the automation shows here within a few seconds.

![The Overview with the head line, warnings, the four tiles and the print plan](../assets/images/en/overview.png)

From top to bottom:

## Head line

| Part | Meaning |
|---|---|
| **Bambuddy** | The state of the connection: **connected**, **not reachable** or **answer refused** (red), **not set up** or **not queried yet** (grey: Reprintbuddy has not reached Bambuddy since it started). |
| **Last query** | When Bambuddy last answered a regular query, or **none yet**. |
| **Automation** | The main switch of the automation (see [Automation](automation.md)). It is the same switch as under *Settings → Automation*; switching it here takes effect at once, without a save button. Off: the rule adds no entries to the queue; replacements and manual prints still work. |

**Query Bambuddy now** at the top right asks Bambuddy at once instead of waiting for the next regular query.

## Warnings

Below the head line stand the warnings and the paused articles, each as a card with the way on (for example **Lift pause**). Red means Bambuddy does not answer or refuses, a hand-over is unclear or a push message did not go through; yellow is a state you chose or that waits for you. The [Troubleshooting](../help/troubleshooting.md) page lists every warning and what to do. Without warnings this part is empty.

## Tiles

Four large tiles fill the width of the page, each with a number and a small picture. The head of a tile leads to its page; an underlined part of its small line leads to a filtered list that shows exactly what the part counts. A number is yellow when something is due, red for articles below their minimum, grey at 0. A tile with 0 stays where it is, so the page does not jump. The bars are only a picture: every part stands in words beside them.

| Tile | What it shows | Leads to |
|---|---|---|
| **Orders** | The open orders, ready to ship ones included. The bar splits them into **ready to ship** (green) and **waiting for stock** (yellow); each part leads to the orders with that status. **n new** says how many came in since you last opened the orders. | Orders |
| **Stock** | All articles with a stock, counted like the page *Articles*: one per article, an article made of parts on stock with the worst state of its variants. Articles made of parts that are built on order do not count (like sets they are deliverable or not). The bar shows **enough** (green), **low** – below the minimum, but the reprint is already running (yellow) – **below minimum** and **overbooked** (red). Each number leads to the articles with exactly that state, so the list shows as many rows as the tile counts. | Articles |
| **Filament** | The spool sorts that are **low**: they have a reorder suggestion that is not marked as ordered. Below, the three reordered sorts that last the shortest, each with **lasts n days**: what is there and on its way, less what the planned prints need, divided by the use per day of the last 30 days. Without use in that time the sort says **no usage**. While the spool stock is not known (Bambuddy not reachable and nothing recorded), the tile says **Stock unknown**. | Filament |
| **To do** | Three lines, each leading to its page: **To check** (finished prints waiting for the check, with the pieces in them), **Assembly** (pieces waiting for assembly after the check, plus pieces of open orders of articles made of parts that are built on order; variants on stock below their target do not count, filling up the stock is not due) and **Reorder suggestions** (not yet marked as ordered, with **n new** and **n on the way**). | To check, Assembly, Reorder suggestions |

!!! note "Reach and forecasts need some time"
    The reach of the filament, like the reorder suggestions, rests on the use of the last 30 days. Right after the installation Reprintbuddy knows little of it, so the numbers are rough at first and become reliable only after some time of operation. As long as less than 30 days of use are known for a sort shown, the tile says **Reach still rough: less than 30 days of usage known**.

### New

**New** means: since you last opened the page **Orders** or **Reorder suggestions**, on any device. There is one account, so opening the orders on the phone clears "new" on the PC as well. An order is new when it was created after that; a suggestion is new when the page did not show it then. Looking at the Overview clears nothing, and neither does a page that only stays open and updates itself, nor a page that Reprintbuddy loads again by itself (after an update or a restart, when updating a part of it failed, or after you were signed out): only opening the page yourself counts, by a click, its address or reloading it by hand. Until a page has been opened once, nothing is new.

## Print plan

Below the tiles, the **Print plan** shows what each printer does and what probably comes next: one lane per printer of Bambuddy, on a time axis from now to the end of the last print (at least 2, at most 24 hours). The whole plan leads to the *Queue*. It only shows: Reprintbuddy assigns nothing in Bambuddy, Bambuddy decides which printer prints what.

| Bar | Meaning |
|---|---|
| **Filled green** | The printer prints now, with progress and time left (for example *63 % · 1 h 10 left*). |
| **Outlined green** | An entry of Bambuddy's queue that Bambuddy has already assigned to this printer, for example one queued in Bambuddy directly on a printer. |
| **Dashed** | Probably assigned: an entry for "any printer of the model". Reprintbuddy places it the way Bambuddy's scheduler would – in the order of Bambuddy's queue (shortest first, if that is switched on in Bambuddy), on the matching printer that becomes free first (same model, not in maintenance, at the entry's location if it names one, with a nozzle that fits the file, mounted or in a nozzle rack). If Bambuddy does not know the nozzle of the file or of the printer, it counts as fitting, as in Bambuddy. Filament and the printers' hours are not taken into account, so Bambuddy may choose another printer. |
| **Dotted** | Still waiting in Reprintbuddy: a print of Reprintbuddy's own queue, not yet handed over to Bambuddy. Reprintbuddy places it when the hand-over would take it: at once while fewer of its prints are open in Bambuddy than the limit, otherwise when one of its open prints ends in the plan. Then it stands where Reprintbuddy puts it in Bambuddy's queue (at the end, or with **Insert as next print** right behind Reprintbuddy's own waiting prints, so ahead of the prints of others behind them that have not started by then; a replacement print always at the front, behind the last replacement of Reprintbuddy still waiting there) and is placed like any other entry. The time until the next query is not counted. |
| *Italic* | Not from Reprintbuddy: entries and prints of other users or started in Bambuddy directly. They occupy the printer, so they are in the plan. |

The length of a bar is the print time of the file as the slicer estimated it; between two prints of a printer there are 5 minutes. All times are estimates. For a print still waiting in Reprintbuddy, the print time of its plate and the nozzle of its file come from Bambuddy's file manager, read with the regular query. In the rare case that Bambuddy knows no print time (a file without the slicer's estimate, or one deleted in Bambuddy, or a print added to Reprintbuddy's queue since the last query), the bar says **Duration unknown** and counts one hour.

Above the lanes: **all probably done at about …** (with **at the earliest** when a duration was unknown), entries that wait in Bambuddy for a manual or later start, entries without a ready printer (also when no such printer has a fitting nozzle), and **as of …** when the printers' state is older than 5 minutes. Of Reprintbuddy's own queue it names only what is not in the lanes: prints **held** in the queue (an article paused counts too), prints that **wait for a place under the limit** because Reprintbuddy's open prints never end in the plan, and, while the queue is **stopped**, all its prints – a stopped queue hands nothing over, so none of them is placed. A print whose article is bound to a printer in maintenance stands as **waiting: …** in that printer's lane. Switching the automation off changes nothing here: the hand-over goes on.

A printer that is **offline**, in **maintenance** or in an **error** gets nothing probably assigned; entries Bambuddy assigned to it stand as **waiting: …** in its lane and do not count for "all done" (**without Printer 3**). A printer **waiting for the plate to be cleared** is planned as if you cleared it now. A **paused** print stays filled with its progress; its time left counts from now.

### Filament in the printers

Under each lane the plan says whether the filament loaded in the printer lasts for the prints of the lane. It only shows: Reprintbuddy books no filament and changes nothing in Bambuddy.

- **Reload about 14:30 · PLA Black (AMS 1, slot 2)**, with a tick on the lane: at about that time the spool runs out, estimated evenly over the print that uses it up. **Reload now**: that moment has passed already. **at the latest**: a print before it has an unknown filament need, so it may come sooner.
- **AMS 1, slot 1 empty about 13:10, continuing with AMS 1, slot 2**, with a grey tick: with **AMS filament backup** on and several slots of the same sort, one of them runs empty and the printer goes on with the next. Refill the empty slot before the last one runs out, and the print does not stop. **empty now** and **at the latest** as above.
- **Not loaded: PETG Red**: a print of the lane needs a sort that is in no slot of the printer.
- **Stock unknown: …**: Bambuddy holds no grams for the spool in that slot (no spool assigned to the slot in Bambuddy). Reprintbuddy does not use the AMS percentage: it is too coarse and missing for spools without RFID.
- **Filament need unknown** on a bar: the print's needs are not known (a print from Bambuddy's archive, or a deleted file).

The colour is named as on the *Filament* page when a spool sort has the same material and colour value and one colour name; otherwise the notes show the value, such as #FF0000.

The need of a print is the grams per slot of its print file: own prints from the article's file, prints of others from Bambuddy's file manager. The running print counts whole, because Bambuddy takes the filament used off the spool only when the print ends. The stock is the grams Bambuddy holds for the spool assigned to the slot. With **AMS filament backup** on, loaded slots of the same material and colour count together, and Reprintbuddy uses them up one after the other: with Bambuddy's setting *Prefer lowest remaining filament* (card *Filament checks*) on, the slot with the fewest grams first, then the next lowest; otherwise in slot order. A single slot of the sort without grams makes the whole sort **Stock unknown**. Reprintbuddy picks the slot as Bambuddy would without the filament preset: same material and colour, else a similar colour, else the same material. Each sort is named once per lane, the first time it runs out; at most three notes, slot notes included, then **+ n**: a reload goes first, then the notes without a time, the slot notes last. The head line adds **Reload needed: Printer 2 about 15:30** for the earliest of all printers (**and n more**), and how many entries Bambuddy held at their start for too little filament; slot notes do not count there.

Limits: Bambuddy chooses the slot only when the print starts and looks at the filament preset first, so it may take another slot. Reprintbuddy also does not follow the nozzle side of printers with two nozzles or Bambuddy's filament rules for user groups. *Prefer lowest remaining filament* counts only for the order in which the slots of one sort run empty with AMS filament backup on; that the printer goes on with the next slot in this order is an assumption, Bambuddy does not report it. For prints probably assigned, the note holds for the printer the plan assumes. There is no notification through Home Assistant for it.

The printers' state comes with every regular query, in one request for all printers, and in between with the printer messages of Bambuddy's live channel (see [Automation](automation.md)). The plan follows at most about once a minute. While Bambuddy cannot be reached it says **Printer state unknown**.

On a phone the page is one column: the head line becomes rows of label and value, the tiles stand one below the other, and each lane of the print plan becomes a list with times and the kind of each bar.

![The Overview on a phone: the tiles one below the other, the print plan as a list per printer](../assets/images/en/overview-mobile.png){ width="320" }

## A fresh installation

Right after the first setup there are no articles yet. The tiles show 0, and below them the page says so and offers **New article from print file**. Until Reprintbuddy has reached Bambuddy, the head line says **not queried yet** and the print plan **Printer state unknown**.
