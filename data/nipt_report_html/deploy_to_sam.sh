#!/bin/bash
# Sync NIPT report template + sample from gx-daemon to /home/sam/GX_Report_html/
set -euo pipefail
SRC="/home/ken/gx-daemon/data/nipt_report_html"
DST="/home/sam/GX_Report_html"
install -m 664 "$SRC/GX_Report_Template.html" "$DST/GX_Report_Template.html"
install -m 664 "$SRC/generate_report.py" "$DST/generate_report.py"
install -m 664 "$SRC/GX_Report_Sample.html" "$DST/GX_Report_Sample.html"
install -m 664 "$SRC/GX_Report_Sample.pdf" "$DST/GX_Report_Sample.pdf"
install -m 664 "$SRC/GX_Report_Sample.html" "$DST/GX_Report_Generated.html"
install -m 664 "$SRC/GX_Report_Sample.pdf" "$DST/GX_Report_Generated.pdf"
install -m 664 "$SRC/genolyx_logo.png" "$DST/genolyx_logo.png"
echo "Deployed NIPT template + sample to $DST"
