# Recorded evidence / Evidencia de referencia

Captured 2026-11-12, 02:37:01–02:37:30 UTC with this staged `lab.py`.
AWS Emulator 1.0.322 (commit 7753983c6e495ec3612695b1336bf4936fc7b7fd),
Linux aarch64 / Amazon Linux 2023, Python 3.9, Boto3 1.40.75.
Validated in a Docker Linux container from macOS, with `--network none` and a
2 GiB memory limit. This does not claim a native macOS binary or a tested
standalone Linux host installation.

- `events.jsonl`: unmodified JSON events saved by the runner. `lambda_log`
  records contain original messages from the emulator's Logs API.
- `handler.log`: the `message` field of every `lambda_log`, in recorded order,
  without the enclosing JSON metadata. No messages were invented or rewritten.
- The environment event records the exact runner and handler SHA-256 hashes.
- Six `error` records are intentional stage-guard tests: repair before prepare,
  consume before send, duplicate send, redrive before repair, repair without
  changing consumer.json, and verify before redrive. All returned nonzero.
- A second diagnose returned its saved result without receiving the DLQ again.
- The validation edited consumer.json programmatically; learners use nano.
- Three failed handler deliveries, DLQ inspection count 4, recovery count 5,
  PASS, and resource cleanup were observed. Preserved identity/count after
  redrive is an emulator fidelity gap; AWS documents new message IDs.
- The raw startup log is deliberately excluded because it contains generated
  emulator credentials. No credentials are in these evidence files.

Estos archivos provienen de una ejecución real del código por etapas contra
el emulador local. No son salidas de AWS ni resultados de la sesión del lector.
Los seis errores comprueban restricciones de orden deliberadamente; la
recuperación, la verificación y la limpieza terminaron correctamente.
