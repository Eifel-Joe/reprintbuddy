# Overview

The **Overview** is the start page. It shows at a glance whether Reprintbuddy can work, what needs you, and how full each article is. Like every page it stays up to date by itself: a booking on another device, a finished print in Bambuddy or the automation shows here within a few seconds.

![The Overview with the head line, warnings, the four key figures and the table of articles](../assets/images/en/overview.png)

From top to bottom:

## Head line

| Part | Meaning |
|---|---|
| **Bambuddy** | The state of the connection: **connected**, **not reachable** or **answer refused** (red), **not set up** or **not queried yet** (grey, right after the start until the first regular query). |
| **Last query** | When Bambuddy last answered a regular query, or **none yet**. |
| **Automation** | The main switch of the automation (see [Automation](automation.md)). It is the same switch as under *Settings → Automation*; switching it here takes effect at once, without a save button. Off: the rule adds no entries to the queue; replacements and manual prints still work. |

**Query Bambuddy now** at the top right asks Bambuddy at once instead of waiting for the next regular query.

## Warnings

Below the head line stand the warnings and the paused articles, each as a card with the way on (for example **Lift pause**). Red means Bambuddy does not answer or refuses, a hand-over is unclear or a push message did not go through; yellow is a state you chose or that waits for you. The [Troubleshooting](../help/troubleshooting.md) page lists every warning and what to do. Without warnings this part is empty.

## Key figures

Four cards, always all four, also at 0. Each card leads to its page.

| Card | Counts | Leads to |
|---|---|---|
| **Below minimum stock** | Articles whose state is **below minimum** or **overbooked** (red). An article that is **low** (yellow) does not count: its reprint is already planned. The small line names how many of them are overbooked. | Articles |
| **Planned and in progress** | Prints in Reprintbuddy's queue (held entries too) plus prints handed over to Bambuddy and not finished yet. The small line splits the two. | Queue |
| **To check** | Pieces that wait for the check plus pieces that wait for assembly. The small line splits the two. | To check |
| **Reorder suggestions** | The open suggestions for filament and bought parts, the same number as on the page [Reorder suggestions](reorder.md). | Reorder suggestions |

## Articles

A table of all active articles, ordered by name like the article list. Archived articles are left out.

| Column | Meaning |
|---|---|
| **Name / article number** | Leads to the article page. |
| **Available** | Stock minus reserved. Red when below 0 (more ordered than there is). |
| **Reserved** | Pieces of open orders. |
| **Min / target** | Minimum stock and target quantity (minimum stock + buffer). |
| **In progress** | Pieces of the prints handed over to Bambuddy. |
| **To check** | Pieces waiting for the check or for assembly. |
| **Fill level** | Available against the target quantity, coloured like the status. |
| **Status** | **overbooked**, **below minimum**, **low** or **enough**, plus **paused** when the automation pauses for the article. **Reprint running (n)** names the prints of the article in Bambuddy. |

The statuses follow the same rules as everywhere in Reprintbuddy, see [The article list](articles.md#the-article-list).

On a phone the page is one column: the head line becomes rows of label and value, two key figures stand side by side, and each article becomes a card with its name and status on top and the numbers below.

## A fresh installation

Right after the first setup there are no articles yet. The key figures show 0, and instead of the table the page says so and offers **New article from print file**. Until the first regular query the head line says **not queried yet**.
