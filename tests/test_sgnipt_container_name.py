"""sgNIPT Docker container name sanitization."""

from app.services.sgnipt import sgnipt_run_container_name


def test_sgnipt_run_container_name_sanitizes_parentheses():
    assert (
        sgnipt_run_container_name("sgNIPT_baseline(B10)")
        == "sgNIPT_baseline-B10-"
    )


def test_sgnipt_run_container_name_passthrough_simple():
    assert sgnipt_run_container_name("NA12878_Twist_Exome") == "NA12878_Twist_Exome"
