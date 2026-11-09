"""The activity log under Settings (Spec 10.7): what happened in Reprintbuddy besides the journal.

Two ways in. Changes to settings, the account and master data are taken where they are saved
(`capture`, a listener on every sessionmaker), in the same transaction. Everything else (signing in,
backups, updates, Bambuddy, push messages, labels) goes through `writer.ActivityLog.record`, which
returns at once; a background task writes it. The page is `web/activity_view.py`.
"""
