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

prod/dev는 nipt-daemon과 동일하게 **env 파일 + 포트**로 구분합니다. compose 파일은 `docker-compose.yml` 하나를 공유하고, dev Portal용 reload 개발만 `docker-compose-dev.yml`을 씁니다.

```bash
cp .env.example .env          # prod Portal (api.genolyx.com, host :8010)
cp .env.dev.example .env.dev  # dev Portal  (dev-api.genolyx.com, host :8011)

make run-prod    # prod-gx-daemon @ :8010, 로그 session/prod/
make run-dev     # dev-gx-daemon  @ :8011, 로그 session/dev/
make reload-dev  # dev + uvicorn --reload (로컬 코딩)
```

데몬 애플리케이션 로그 (날짜별):

```bash
tail -f session/prod/$(date +%Y%m%d).log   # prod
tail -f session/dev/$(date +%Y%m%d).log    # dev
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
