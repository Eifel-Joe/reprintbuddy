# Reprintbuddy

**Your shelf stays stocked. Bambuddy does the printing.**

Reprintbuddy keeps the stock of 3D-printed items. It reprints what runs low by handing print jobs to Bambuddy's queue, lets you check finished prints, and tells you what to reorder. It runs as a small web application on your home network, reachable from outside only through a VPN.

!!! note "Status"
    Under development, not ready for real use yet.

Reprintbuddy is an independent project and is not affiliated with Bambuddy or Bambu Lab.

## What it is, and what it is not

- **It is** a stock book per article with minimum stock and buffer, its own reprint queue, a check list for finished prints, assembly bookings and reorder suggestions for filament and bought-in parts.
- **It never controls printers.** Bambuddy stays your print system. Reprintbuddy only adds jobs to Bambuddy's queue and reads status. It does not start, stop or pause anything, and it never writes to Spoolman.
- It contains no articles of its own: you create articles, sets, parts and rules in the web interface.

## How it works

1. You create an **article** from a print file that lies in Bambuddy's file manager, and give it a minimum stock.
2. When the stock runs below the minimum, Reprintbuddy puts a **reprint** into its own queue and hands it to Bambuddy, one print at a time.
3. Bambuddy prints. When a print is finished, it shows up under **To check**. You count the good pieces and book them into stock.
4. Reprintbuddy watches your **filament** and the **bought-in parts** and suggests what to reorder.

## Where to go next

| I want to … | Page |
|---|---|
| see what I need before I start | [Requirements](start/requirements.md) |
| install Reprintbuddy | [Installation](start/install.md) |
| connect it to Bambuddy | [First setup](start/setup.md) |
| find out what it will not do | [Limits](help/limits.md) |

## Problems and questions

Bugs, wishes and questions: [open an issue on GitHub](https://github.com/Eifel-Joe/reprintbuddy/issues). Security problems never as a public issue, see [SECURITY.md](https://github.com/Eifel-Joe/reprintbuddy/blob/main/SECURITY.md).
