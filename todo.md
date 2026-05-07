# TODO

## Two-stage progress label

Show meaningful stage labels in the "Aktuell fil" progress bar instead of a single "Bearbetar: filename" throughout.

### Background

Docling's pipeline runs these stages per page (all inside `_build_document`):
`preprocess → OCR → layout → table → per-page-assemble`

Then `_assemble_document` (fast, document-level assembly) and `_enrich_document` (usually disabled).

### Plan

1. Add a module-level `_stage_callback = None` variable.
2. Create `StagedPipeline(StandardPdfPipeline)` that overrides `_assemble_document` to call `_stage_callback()` before delegating to `super()`.
3. Update `_build_converter()` to pass `pipeline_cls=StagedPipeline` in `PdfFormatOption`.
4. In `WorkerThread.run()`, before each file conversion:
   - Set `_stage_callback` to a closure that puts `("stage_label", f"Sammanfogar: {filename}")` on `w2g`.
   - Also send `("stage_label", f"Kör OCR + layout: {filename}")` immediately when starting the file (replaces/augments the existing "Bearbetar" label).
   - Clear `_stage_callback = None` in a `finally` block.
5. In `App._poll_queue()`, handle `"stage_label"` messages by updating `self._file_label`.

### Labels

- Stage 1 (heavy, most of the time): `Kör OCR + layout: <filename>`
- Stage 2 (fast, document assembly): `Sammanfogar: <filename>`
