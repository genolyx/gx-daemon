# gx-daemon ↔ gx-nipt integration

This document describes how `gx-daemon` drives the Nextflow-based `gx-nipt`
pipeline, including the directory contract, argument mapping, completion
semantics, and how to run an end-to-end smoke test.

## 1. Components

| Component | Role |
|-----------|------|
| `gx-daemon` (`app.services.nipt.NIPTPlugin`) | Plugin registered under `service_code = "nipt"` in `app/services/__init__.py`. Owns path normalisation, command construction, completion polling, result registration. |
| `gx-nipt/bin/run_nipt.sh` | Wrapper that the plugin invokes. Creates the host directory layout, seeds lab config, symlinks FASTQs, runs `nextflow run main.nf`, then packages outputs into `<order>.json` + `<order>.output.tar` for daemon pickup. |
| `gx-nipt/main.nf` | Nextflow DSL2 pipeline. Reads `params.root_dir`, `params.work_dir`, `params.sample_name`, etc. |
| `gx-nipt/nextflow.config` | Default Docker profile, `ssd_scratch` profile, configurable container tags. |

## 2. Routing

Platform submits to `POST /api/v3/orders` with `type` (e.g. `"NIPT"`,
`"SGNIPT"`, `"carrier_screening"`). `gx-daemon/app/main.py::_detect_service_code`
maps:

```
type in {"NIPT", "nipt"}                        → service_code = "nipt"   (gx-nipt)
type in {"SGNIPT","sgnipt","SG_NIPT","sg-nipt"} → service_code = "sgnipt" (legacy single-gene)
otherwise                                       → service_code = "carrier_screening"
```

Both `nipt` and `sgnipt` requests are enqueued via `_enqueue_nipt_order`
so Platform's FASTQ download completes before the job is queued.

## 3. Directory contract

All paths are rooted at `NIPT_ROOT_DIR` (`settings.nipt_root_dir`, default
`/home/ken/gx-nipt-data`). `apply_nipt_layout_directories(job)` normalises
job fields on enqueue/restore:

```
${NIPT_ROOT_DIR}/
├── fastq/<work_dir>/<order_id>/R1.fastq.gz
│                                 R2.fastq.gz
├── analysis/<work_dir>/<order_id>/…       # Nextflow publishDir target
├── log/<work_dir>/<order_id>/pipeline.log # run_nipt.sh tees here
│                             /pipeline_info/  # Nextflow trace/timeline
├── output/<work_dir>/<order_id>/
│        ├── <order_id>.json            # result summary (daemon upload)
│        ├── <order_id>.output.tar      # archive of Output_* dirs
│        ├── <order_id>_progress.txt    # streaming progress lines
│        ├── <order_id>.completed       # success marker
│        ├── <order_id>.failed          # failure marker (exclusive)
│        └── Output_*/                  # detailed artefacts (Report, QC…)
└── config/<labcode>/pipeline_config.json
```

This matches the on-disk contract that `nipt-daemon` used with `ken-nipt`,
so existing daemon upload logic (picks up `<order_id>.json` and
`<order_id>.output.tar`) keeps working. `analysis/` can be deleted after
success; only `output/<order>/` and `log/<order>/` need to survive.

## 4. Argument mapping

`NIPTPlugin.get_pipeline_command()` builds:

```
bash /home/ken/gx-nipt/bin/run_nipt.sh \
  --order-id   <order_id> \
  --work-dir   <work_dir> \
  --root-dir   <NIPT_ROOT_DIR> \
  --labcode    <labcode> \
  --fastq-r1   R1.fastq.gz \
  --fastq-r2   R2.fastq.gz \
  --age        <patient_age>
```

Optional flags are emitted when the Platform DTO `params` or `Settings`
set them:

| Source | Flag |
|--------|------|
| `params.use_ssd` / `settings.nipt_use_ssd` | `--use-ssd`, `--scratch-dir …`, `--ssd-max-usage-gb …` |
| `params.gxff_model` / `settings.nipt_gxff_model` | `--gxff-model <.pkl>` |
| `params.gxcnv_reference` / `settings.nipt_gxcnv_reference` | `--gxcnv-reference <.npz>` |
| `params.run_wcx == False` / `settings.nipt_run_wcx == False` | `--no-wcx` |
| `params.algorithm_only` / `params._algorithm_only` | `--algorithm-only` |
| `params.from_bam` | `--from-bam <path>` |
| `params._pipeline_fresh` | `--fresh` |
| `params._force` | `--force` |

SSD scratch selects the `ssd_scratch` profile in `nextflow.config`, so
intermediate BAMs live under `--scratch-dir` and only final artefacts
land in `analysis/…/Output_*`.

## 5. Completion semantics

- **Live progress**: `run_nipt.sh progress()` appends stage lines to
  `<order>_progress.txt`. `NIPTPlugin.get_progress_stages()` maps the
  `STAGE` tokens (`PREPARED, RUN, BWA, HMMCOPY, WCX, REPORT,
  PIPELINE_DONE, COMPLETED`) to 0–100 values the daemon reports to the
  Platform.
- **Success**: wrapper writes `<order_id>.completed` and guarantees
  `<order_id>.json` + `<order_id>.output.tar` exist. `check_completion`
  requires `<order_id>.json` to be present and parseable.
- **Failure**: wrapper writes `<order_id>.failed` and exits non-zero.
  The runner surfaces the exit code to `OrderStatus.FAILED` and keeps
  the pipeline log at `log/<work>/<order>/pipeline.log`.
- **Restart recovery**: on daemon restart, `sync_is_complete` checks
  for `<order_id>.json` on disk and flips stuck jobs to
  `OrderStatus.REPORT_READY` automatically.

## 6. Daemon settings (`.env`)

The relevant keys (see `.env.example`) are:

```ini
ENABLED_SERVICES=carrier_screening,whole_exome,health_screening,sgnipt,nipt

NIPT_ROOT_DIR=/home/ken/gx-nipt-data
NIPT_PIPELINE_DIR=/home/ken/gx-nipt          # has main.nf + bin/run_nipt.sh
# NIPT_RUN_SCRIPT=/home/ken/gx-nipt/bin/run_nipt.sh   (override if needed)
NIPT_DEFAULT_LABCODE=CORDLIFE                # fallback when DTO omits labcode

# SSD scratch (optional; maps to run_nipt.sh --use-ssd)
NIPT_USE_SSD=false
# NIPT_SCRATCH_DIR=/tmp/nipt_scratch
# NIPT_SSD_MAX_USAGE_GB=200

# gx-FF / gx-cnv (optional)
# NIPT_GXFF_MODEL=/data/models/gxff.pkl
# NIPT_GXCNV_REFERENCE=/data/refs/gxcnv/ref.npz
# NIPT_RUN_WCX=true

# Reference data root (bind-mounted into the container, read-only)
NIPT_REF_DIR=/data/reference
```

Any of the NIPT settings may also be overridden per-order via the
Platform DTO's `params` block (e.g. `{"use_ssd": true, "scratch_dir":
"/fast/nvme", "ref_dir": "/mnt/nfs/reference"}`).

## 6b. Reference data layout

`gx-nipt` does **not** ship reference files in the repository or the
Docker image. Instead, the pipeline expects a host directory (default
`/data/reference`) populated with the following subtree, which is
bind-mounted read-only into every container at the same path via
`docker.runOptions` in `nextflow.config`:

```
${NIPT_REF_DIR}/
├── genomes/
│   └── hg19/
│       ├── hg19.fa
│       ├── hg19.fa.fai
│       ├── hg19.fa.0123          # bwa-mem2 index
│       ├── hg19.fa.amb
│       ├── hg19.fa.ann
│       ├── hg19.fa.bwt.2bit.64
│       └── hg19.fa.pac
├── hmmcopy/
│   ├── hg19.50kb.gc.wig
│   ├── hg19.50kb.map.wig
│   ├── hg19.10mb.gc.wig
│   └── hg19.10mb.map.wig
├── models/
│   └── seqff_model.pkl
└── labs/
    └── <labcode>/                # e.g. CORDLIFE, UCL, VN, ...
        ├── WC/<group>/<group>_200k.npz
        ├── WCX/<group>/{M,F}_200k.npz
        ├── EZD/<group>/...       # per-chromosome thresholds
        ├── PRIZM/<group>/...
        └── bed/                  # microdeletion panels
```

The wrapper (`bin/run_nipt.sh`) performs a preflight check before
launching Nextflow and aborts with a clear error if any of the
mandatory paths are missing:

- `genomes/hg19/hg19.fa`
- `hmmcopy/hg19.50kb.gc.wig`, `hg19.50kb.map.wig`
- `labs/<labcode>/`

Lab-specific bundles (EZD/PRIZM/WC/WCX/bed) are generated with the
helpers under `bin/scripts/utils/reference/`; see
`gx-nipt/docs/reference_generation.md` for the full workflow.

Switching reference roots per-run (e.g. to point at an NFS share) is
done by passing `--ref-dir` to `run_nipt.sh`, or equivalently by
including `params.ref_dir` in the Platform DTO or setting
`NIPT_REF_DIR` in `.env`.

## 6c. Deferred algorithms: gx-FF and gx-cnv

`gx-nipt` ships two enhanced analysers that are **optional at runtime**
and currently **deferred** — their training / reference-building
pipelines live in the companion repos and have not yet published
artefacts we can load:

| Feature | Enable flag | Host path (when ready) | Fallback when disabled |
|---------|-------------|------------------------|------------------------|
| gx-FF (LightGBM + DNN ensemble FF) | `--gxff-model <.pkl>` | e.g. `${NIPT_REF_DIR}/models/gxff_model.pkl` | seqFF-only FF estimation |
| gx-cnv (hybrid dual-track CNV)     | `--gxcnv-reference <.npz>` (alias: `--gxcnv-model`) | e.g. `${NIPT_REF_DIR}/gxcnv/hg19_panel.npz` | WisecondorX-only CNV calling |

The `NIPTPlugin` only emits the corresponding flag when a model/reference
path is present — either from the Platform DTO (`params.gxff_model`,
`params.gxcnv_reference`, or `params.gxcnv_model`) or from the daemon
settings (`NIPT_GXFF_MODEL`, `NIPT_GXCNV_REFERENCE`,
`NIPT_GXCNV_MODEL`). With no path configured, `main.nf` substitutes a
`NO_FILE` sentinel channel and the ensemble/compare steps short-circuit,
so the pipeline keeps running in legacy mode without errors. The
wrapper's reference preflight also **does not** check for either file,
so a fresh lab onboarding never blocks on them.

Ensemble behaviour once gx-FF is enabled (implemented in
`workflows/ff_gender.nf`):

| FF band | Decision |
|---------|----------|
| FF < 5 %  | use gx-FF alone (low-FF safety region) |
| FF ≥ 5 %  | `0.6 × gx-FF + 0.4 × seqFF` weighted ensemble |

Concordance for gx-cnv vs. WisecondorX (`GXCNV_COMPARE` process) is
logged to `Output_cnv_comparison/<sample>.concordance.tsv` each run.
When that report is stable for the lab's QC thresholds, flip
`NIPT_RUN_WCX=false` (or pass `--run_wcx false`) to drop WCX from the
critical path.

## 7. Local smoke test

1. **Build the gx-nipt image** (first time only):

   ```bash
   cd /home/ken/gx-nipt
   docker build -t gx-nipt:latest ./docker/
   ```

2. **Seed a lab config** (if you haven't already):

   ```bash
   mkdir -p /home/ken/gx-nipt-data/config/CORDLIFE
   cp /home/ken/gx-nipt/conf/labs/CORDLIFE/pipeline_config.json \
      /home/ken/gx-nipt-data/config/CORDLIFE/pipeline_config.json
   ```

   `run_nipt.sh` will auto-seed this on first run too.

3. **Put an R1/R2 pair somewhere the daemon can read**, e.g.
   `/data/fastq/GNMF26040049_R{1,2}.fastq.gz`.

4. **Submit via the local API** (bypassing the Platform round-trip):

   ```bash
   curl -s -X POST http://localhost:8000/order/nipt/submit \
     -H 'Content-Type: application/json' \
     -d '{
       "order_id":   "GNMF26040049",
       "sample_name":"GNMF26040049",
       "work_dir":   "2604",
       "fastq_r1_path":"/data/fastq/GNMF26040049_R1.fastq.gz",
       "fastq_r2_path":"/data/fastq/GNMF26040049_R2.fastq.gz",
       "params": { "labcode":"CORDLIFE", "patient_age": 34, "use_ssd": false }
     }'
   ```

5. **Tail the pipeline log**:

   ```bash
   tail -f /home/ken/gx-nipt-data/log/2604/GNMF26040049/pipeline.log
   ```

6. **Pickup** happens automatically: once
   `/home/ken/gx-nipt-data/output/2604/GNMF26040049/GNMF26040049.json`
   exists, the daemon uploads it + `.output.tar` to the Platform and
   marks the order `COMPLETED`.

## 8. Troubleshooting

| Symptom | Fix |
|---------|-----|
| `run_nipt.sh: command not found` | Check `NIPT_PIPELINE_DIR` / `NIPT_RUN_SCRIPT`; ensure the file is executable (`chmod +x`). |
| `ERROR: lab config not found` | Drop `pipeline_config.json` at `$NIPT_ROOT_DIR/config/<labcode>/`, or add it under `gx-nipt/conf/labs/<labcode>/` so the wrapper can seed it. |
| Nextflow tasks fail with `docker: /bin/bash: No such file or directory` | Confirm `nextflow.config` has `runOptions = '--entrypoint "" -e TZ=Asia/Seoul --user $(id -u):$(id -g)'`. The `gx-nipt` Dockerfile ships with a Python `ENTRYPOINT` that must be overridden. |
| Job stays at `RUNNING` forever | Check `<output>/<order>_progress.txt`; if the last stage is `BWA`/`HMMCOPY`, inspect `pipeline.log` and Nextflow's `log/pipeline_info/trace.txt`. |
| Daemon never flips to `COMPLETED` | The plugin only flips when `<order_id>.json` exists and parses. Verify the wrapper's "Copied result JSON ->" line is present in `pipeline.log`. |
| Restart recovery | On daemon restart the queue manager calls `sync_is_complete`; any job with `<order_id>.json` on disk is marked `REPORT_READY` and uploaded without re-running Nextflow. |
