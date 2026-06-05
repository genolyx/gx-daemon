"""Tests for carrier screening FASTQ path alias inside gx-daemon container."""

import os
from unittest.mock import patch

from app.services.carrier_screening.plugin import CarrierScreeningPlugin


R1_HOST = (
    "/home/ken/gx-exome/fastq/2606/"
    "Exome2Comp_NA12878_LPEF2_TwistCap_B10_S102_R1_001_downsampled.100bp_5prime.fq.gz"
)
R2_HOST = (
    "/home/ken/gx-exome/fastq/2606/"
    "Exome2Comp_NA12878_LPEF2_TwistCap_B10_S102_R2_001_downsampled.100bp_5prime.fq.gz"
)
R1_DATA = (
    "/data/gx-exome/fastq/2606/"
    "Exome2Comp_NA12878_LPEF2_TwistCap_B10_S102_R1_001_downsampled.100bp_5prime.fq.gz"
)
R2_DATA = (
    "/data/gx-exome/fastq/2606/"
    "Exome2Comp_NA12878_LPEF2_TwistCap_B10_S102_R2_001_downsampled.100bp_5prime.fq.gz"
)


def test_daemon_accessible_path_rewrites_host_prefix():
    with patch.object(os.path, "exists", return_value=True):
        alias = CarrierScreeningPlugin._daemon_accessible_path(R1_HOST)
    assert alias == R1_DATA


def test_daemon_accessible_path_passthrough_data_prefix():
    assert CarrierScreeningPlugin._daemon_accessible_path(R1_DATA) == R1_DATA


def test_fastq_readable_by_process_accepts_host_paths_when_data_alias_readable():
    def fake_access(path, mode):
        return path.startswith("/data/gx-exome/") and mode == os.R_OK

    def fake_isfile(path):
        return path in (R1_DATA, R2_DATA)

    with patch.object(os.path, "isfile", side_effect=fake_isfile), patch.object(
        os, "access", side_effect=fake_access
    ):
        err = CarrierScreeningPlugin._fastq_readable_by_process(R1_HOST, R2_HOST)
    assert err is None


def test_gx_exome_run_container_name_matches_run_analysis():
    from app.services.carrier_screening.layout_norm import gx_exome_run_container_name

    assert (
        gx_exome_run_container_name("2606", "Twist_NA12878_B10")
        == "gx-exome-2606-Twist_NA12878_B10"
    )


def test_run_analysis_data_dir_maps_container_layout_to_host():
    plugin = CarrierScreeningPlugin()
    with patch("app.services.carrier_screening.plugin.settings") as mock_settings:
        mock_settings.carrier_screening_script_data_dir = ""
        mock_settings.carrier_screening_host = "/home/ken/gx-exome"
        mock_settings.carrier_screening_layout_base = "/data/gx-exome"
        assert plugin._run_analysis_data_dir() == "/home/ken/gx-exome"
