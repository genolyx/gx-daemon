# GX-Daemon

Headless genomics analysis daemon that integrates with the Genolyx Platform.

Combines:
- **Platform integration** from nipt-daemon (authentication, FASTQ download, result/report upload)
- **Carrier Screening analysis** from service-daemon (Nextflow pipeline, VCF annotation, review, reporting)

No embedded Portal UI — operates as an API-only service that receives orders from the Genolyx Platform.

## Quick Start

```bash
cp .env.example .env
# Edit .env with your platform credentials and paths

pip install -r requirements.txt
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

## Docker

```bash
docker compose --env-file .env.compose up -d --build
```

## API Endpoints

| Method | Path | Description |
|--------|------|-------------|
| `GET` | `/health` | Health check |
| `POST` | `/analysis/order/{order_id}/submit` | Submit order from Platform |
| `GET` | `/status/{order_id}` | Order status |
| `GET` | `/queue/summary` | Queue summary |
| `GET` | `/order/{order_id}/result` | Analysis result (result.json) |
| `POST` | `/order/{order_id}/report` | Generate PDF report |
| `POST` | `/analysis/order/{order_id}/report` | Generate + upload report to Platform |

## Architecture

```
Platform API ──POST submit──▶ gx-daemon ──▶ Carrier Screening Pipeline
                                   │
                                   ├── Download FASTQ (remote) or use local
                                   ├── Run Nextflow pipeline
                                   ├── VCF annotation + result.json
                                   ├── Upload TAR + notify Platform
                                   └── Review APIs for Portal integration
```
