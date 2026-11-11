# Overview

The **Overview** is the start page. It shows at a glance whether Reprintbuddy can work and what is due: no lists, only numbers, each leading to the page where you do it. Like every page it stays up to date by itself: a booking on another device, a finished print in Bambuddy or the automation shows here within a few seconds.

![The Overview with the head line, warnings and the cards of what is due](../assets/images/en/overview.png)

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

## What is due

Seven cards, always all seven. A card with nothing to do stays grey with 0; yellow means something is due, red is for articles below their minimum. The whole card leads to its page; an underlined part of the small line leads to a filtered list.

| Card | Number | Small line | Leads to |
|---|---|---|---|
| **Orders** | Open orders, ready to ship ones included. | **n new** · **n ready to ship** (leads to the open orders ready to ship) | Orders |
| **To check** | Finished prints waiting for the check. | **n pieces** in these prints | To check |
| **Assembly** | Pieces waiting for assembly after the check, plus pieces of open orders of articles made of parts that are built on order. Variants on stock below their target are not counted: filling up the stock is not due. | – | Assembly |
| **Reorder suggestions** | Suggestions for filament and bought parts not yet marked as ordered, the same number as on [Reorder suggestions](reorder.md). | **n new** · **n on the way** (marked as ordered, not arrived yet) | Reorder suggestions |
| **Prints** | Prints Bambuddy runs. A print counts as running from the moment Bambuddy sends the start command to the printer, so heating and levelling count too. | **n in the queue**: Reprintbuddy's queue (held entries and entries of paused articles too) plus prints handed over to Bambuddy that wait for their start there | Queue |
| **Filament** | Spool sorts that are **low**: they have a reorder suggestion. A sort marked as ordered is on its way and no longer low. While the spool stock is not known (Bambuddy not reachable and nothing recorded) the card says **Stock unknown**. | – | Filament |
| **Below minimum stock** | Articles whose state is **below minimum** or **overbooked** (red); each variant of an article made of parts on stock counts on its own. An article that is **low** (yellow) does not count: its reprint is already planned. | **n overbooked** (leads to the overbooked articles) | Articles, filtered to **below minimum**. The list shows one row per article and the overbooked ones under their own filter, so it can show fewer than the card counts. |

### New

**New** means: since you last opened the page **Orders** or **Reorder suggestions**, on any device. There is one account, so opening the orders on the phone clears "new" on the PC as well. An order is new when it was created after that; a suggestion is new when the page did not show it then. Looking at the Overview clears nothing, and neither does a page that only stays open and updates itself, nor a page that Reprintbuddy loads again by itself (after an update or a restart, when updating a part of it failed, or after you were signed out): only opening the page yourself counts, by a click, its address or reloading it by hand. Until a page has been opened once, nothing is new.

On a phone the page is one column: the head line becomes rows of label and value, and the cards stand one below the other.

## A fresh installation

Right after the first setup there are no articles yet. The cards show 0, and below them the page says so and offers **New article from print file**. Until Reprintbuddy has reached Bambuddy, the head line says **not queried yet**.
