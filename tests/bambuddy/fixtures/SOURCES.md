# Sources of the Bambuddy fixtures

The JSON files in this folder are answers in the shape Bambuddy gives them. They were not
recorded from a running Bambuddy: each one is built from Bambuddy's response schema and the
route code that fills it. All names, addresses and texts are neutral placeholders.

Paths are relative to Bambuddy's repository. Tags:

- **D4** = `v1.3.1b1-daily.20261104`
- **D5** = `v1.3.1b1-daily.20261105` (for files the D4 copy lacks; `api/routes/print_queue.py`,
  `api/routes/library.py` and `schemas/print_queue.py` are identical in both tags)

Times follow the serializers: queue and batch times (`UTCDatetime`) end in `Z`
(`backend/app/schemas/print_queue.py:12-19`); library, printer, spool and API-key times are
plain `datetime` values without a zone. Error bodies are FastAPI's `{"detail": ...}`.

| Fixture | Answer to | Source | Choices where the source leaves room |
|---|---|---|---|
| `auth_me.json` | `GET /api/v1/auth/me` with a valid key | D5 `backend/app/api/routes/auth.py:100-142` (key answer), `:722-800`; `backend/app/schemas/auth.py:26-33`, `:83-96`; `backend/app/core/auth.py:350-375` (effective rights, sorted) | `groups` is `[]`: a key never lists its owner's groups (`auth.py:137-139`). `permissions` = exactly the seven rights Reprintbuddy needs, assuming the owner's group grants nothing more. `created_at` is the key's creation time, `isoformat()` without zone (`:141`). |
| `auth_401.json` | `GET /api/v1/auth/me` with an unknown key | D5 `backend/app/api/routes/auth.py:802-807` | – |
| `printers.json` | `GET /api/v1/printers/` | D5 `backend/app/api/routes/printers.py:156-175`; `backend/app/schemas/printer.py:29-63`, `:113-136`; `backend/app/models/printer.py:9-22` | A key gets `PrinterResponse` without `access_code` (`printers.py:162-164`). Sorted by name. `supports_nozzle_flow_type` left at its default `true`; `plate_detection_roi` `null`. |
| `printers_maintenance.json` | `GET /api/v1/printers/` while "Printer 2" is in maintenance mode | as `printers.json`; maintenance mode: D5 `frontend/src/api/client.ts:775-778`, `frontend/src/__tests__/pages/PrintersPageDeletePowerWiring.test.tsx:331-343`, `backend/app/api/routes/printers.py:473-476` | Maintenance mode is no field of its own: the printer page sends `PATCH /printers/{id}` with `{"is_active": false}`, and leaving it sends `true`. So the printer in maintenance has `is_active: false` (and a newer `updated_at`); everything else is unchanged. |
| `folders.json` | `GET /api/v1/library/folders/` | D4 `backend/app/api/routes/library.py:1063-1182` (tree `:1135-1158`), `:224-232`; D5 `backend/app/schemas/library.py:93-119` | Top folders with sub-folders in `children`, sorted by name. `can_write`/`can_rename`/`can_delete` `false` for a key that may only read (not checked by Reprintbuddy). |
| `files_root.json` | `GET /api/v1/library/files/` | D4 `backend/app/api/routes/library.py:2340-2506` (top level only `:2424-2425`); D5 `backend/app/schemas/library.py:220-268` | No `file_hash` in the list. `print_name` `null`: library files store their metadata without it (`library.py:683-697`). |
| `files_folder.json` | `GET /api/v1/library/files/?folder_id=1` | D4 `backend/app/api/routes/library.py:2340-2506` (`:2418-2419`); D5 `backend/app/schemas/library.py:220-268` | as `files_root.json` |
| `file.json` | `GET /api/v1/library/files/5` | D4 `backend/app/api/routes/library.py:5468-5563`, hash `:326-332`; D5 `backend/app/schemas/library.py:160-207`; `file_type` D5 `backend/app/api/routes/library.py:250-275` | `metadata` holds a subset of the 3MF parser's keys (D5 `backend/app/services/archive.py`), without `print_name`. `file_path` is a neutral relative path. |
| `file_404.json` | `GET /api/v1/library/files/999999999` | D4 `backend/app/api/routes/library.py:162-169` | – |
| `plates.json` | `GET /api/v1/library/files/5/plates` | D4 `backend/app/api/routes/library.py:3498-3787` (plate entries `:3760-3775`, filaments `:3680-3700`) | Plate `name` falls back to the first object name (`:3754-3758`); object names are placeholders. `tray_info_idx` `""` (the source's default when the 3MF has none). Preset names → `Preset`. |
| `plate_404.json` | `GET /api/v1/library/files/5/plate-thumbnail/99` | D4 `backend/app/api/routes/library.py:3790-3825` | – |
| `requirements.json` | `GET /api/v1/library/files/5/filament-requirements?plate_id=1` | D4 `backend/app/api/routes/library.py:3876-4071` (entries `:3958-3973`, answer `:4066-4071`) | `color` is passed through from the 3MF unchanged; written here as `#RRGGBB`, the form Bambu Studio's `slice_info.config` uses. No `nozzle_id`/`group_id` (single nozzle). |
| `spoolman_status.json` | `GET /api/v1/spoolman/status` in Spoolman mode | D5 `backend/app/api/routes/spoolman.py:59-66`, `:115-170` | – |
| `spools_spoolman.json` | `GET /api/v1/spoolman/inventory/spools` | D5 `backend/app/api/routes/spoolman_inventory.py:515-565`; `backend/app/api/routes/_spoolman_helpers.py:357-568` | `created_at`, `first_used`, `last_used` are Spoolman's own texts, passed through (`:474`); written here with `Z`, Spoolman's form is not in Bambuddy's source. `weight_used` = `label_weight` − Spoolman's `remaining_weight`. |
| `spools_builtin.json` | `GET /api/v1/inventory/spools` | D4 `backend/app/api/routes/inventory.py:1430-1442`; D5 `backend/app/schemas/spool.py:107-166`, `:286-318` | Built from the schema with two spools. `data_origin`, `tag_type` `null` for hand-made spools. |
| `queue_open.json` | `GET /api/v1/queue/?status=pending,printing` | D4 `backend/app/api/routes/print_queue.py:740-811`, `_enrich_response` `:437-643`; `backend/app/schemas/print_queue.py:215-325` | Sorted by `position`, then `id`. Entry 30 comes from an archive (reprint): label falls back to `archive_name`. |
| `queue_all.json` | `GET /api/v1/queue/` | as `queue_open.json`; `skipped` text `backend/app/services/print_scheduler.py:2249-2255` | All entries with the history, sorted by `position`, then `id`; finished entries keep their old position. |
| `queue_404.json` | `GET /api/v1/queue/999999999` | D4 `backend/app/api/routes/print_queue.py:2362-2398` | – |
| `batch1.request.json` | body of `POST /api/v1/queue/batches` | D4 `backend/app/schemas/print_queue.py:431-463` | Without `plates` (Plan B2, Decision 10). |
| `batch1.json` | `POST /api/v1/queue/batches` | D4 `backend/app/api/routes/print_queue.py:1821-1936`, `:2288-2359`; `backend/app/schemas/print_queue.py:526-567`; `backend/app/services/print_batch.py:245-333` | Empty batch: all counts 0, `plates` `[]`. |
| `batch1_again.json` | the same `POST` again | D4 `backend/app/api/routes/print_queue.py:1804`, `:1864-1872` | – |
| `batch1_find_empty.json` | `GET /api/v1/queue/batches?external_source=reprintbuddy&external_ref=…` while the batch is empty | D4 `backend/app/api/routes/print_queue.py:2150-2211` (empty batches left out `:2164-2178`) | – |
| `batch1_find.json` | the same search after `q4` was queued into the batch | as `batch1.json`; `backend/app/schemas/print_queue.py:501-524`; `backend/app/services/print_batch.py:104-158`, `:290-333` | One plate row derived from the pending entry (no targets): `quantity_target` 1, `remaining` 0, `can_dispatch` false. |
| `q4.request.json` | body of `POST /api/v1/queue/` | D4 `backend/app/schemas/print_queue.py:85-152`; Plan B2, Decision 11 | Queued into the batch from `batch1.json`. |
| `q4.json` | `POST /api/v1/queue/` | D4 `backend/app/api/routes/print_queue.py:1188-1584`; `backend/app/schemas/print_queue.py:215-325` | `library_file_name` is the file name (no `print_name`, see `files_root.json`). Print options at the schema defaults (`:112-127`). `required_filament_types` from the plate. |
| `q4_printing.json` | `GET /api/v1/queue/31` after the start | as `q4.json`; start `backend/app/services/print_scheduler.py:9627`, `:9675-9677` | `printer_id`/`printer_name` of the printer that took it, `archive_id` set (archive made at the start, schema `:223`), `target_model` kept. `started_at` has microseconds (`datetime.now()`). |
| `q4_completed.json` | `GET /api/v1/queue/31` after the end | as `q4_printing.json` | `completed_at` set. |
