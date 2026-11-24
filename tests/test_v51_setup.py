"""
v51 — first-run setup for a brand-new shop (a second location, or a copy sold to someone else).

A new database no longer starts with an owner whose PIN is 1234. These tests run the real app in a separate process on
a throwaway SQLite file for each scenario (the shared test database already has its staff, so it can never be "new").
They cover the gate, the setup code, every refusal, the success path, and that an existing shop is never touched.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from app import setup as S

HELPER = Path(__file__).resolve().parent / "v51_setup_scenarios.py"
ROOT = Path(__file__).resolve().parent.parent


def run(scenario, extra_env=None, prepare=None):
    """Run one scenario on its own database. `prepare(db_file)` can build the database first (for 'existing shop')."""
    with tempfile.TemporaryDirectory() as tmp:
        db_file = str(Path(tmp) / "shop.db")
        env = {k: v for k, v in os.environ.items() if k not in ("SETUP_CODE", "TEST_DATABASE_URL", "DATABASE_URL")}
        env.update(extra_env or {})
        env["PYTHONPATH"] = str(ROOT)
        if prepare:
            prepare(db_file, env)
        proc = subprocess.run([sys.executable, str(HELPER), scenario, db_file], capture_output=True, text=True, env=env, timeout=240, cwd=tmp)
        lines = [ln for ln in proc.stdout.splitlines() if ln.startswith("RESULT:")]
        assert lines, f"scenario {scenario} produced no result.\nSTDOUT:\n{proc.stdout[-2000:]}\nSTDERR:\n{proc.stderr[-3000:]}"
        return json.loads(lines[-1][len("RESULT:"):]), proc.stdout


@pytest.fixture(scope="module")
def flow():
    result, stdout = run("flow")
    result["stdout"] = stdout
    return result


# ═══ the gate ═════════════════════════════════════════════════════════

def test_a_new_database_is_pending_and_has_a_random_setup_code(flow):
    assert flow["pending"] is True
    code = flow["code_shape"]
    assert len(code) == 14 and code.count("-") == 2, code           # AAAA-BBBB-CCCC
    assert not set(code.replace("-", "")) & set("01OI"), "look-alike characters would be hard to read off a log"


def test_the_code_is_printed_in_the_server_log_at_startup(flow):
    assert "SETUP CODE: " + flow["code_shape"] in flow["stdout"]


def test_every_page_but_setup_is_redirected_to_setup(flow):
    for url, (status, location) in flow["gate"].items():
        assert (status, location) == (303, "/setup"), url


def test_the_setup_screen_health_check_and_static_files_stay_reachable(flow):
    assert flow["open"] == {"healthz": 200, "static": 200, "setup": 200}
    assert flow["setup_page_has_code_box"] is True


def test_the_old_default_pin_does_not_work_on_a_new_shop(flow):
    assert flow["old_pin_login"] == [303, "/setup"]                    # never signed in, just sent to setup


def test_other_posts_are_blocked_too(flow):
    assert flow["post_other"] == 303


# ═══ refusals ═════════════════════════════════════════════════════════

def test_wrong_or_missing_code_is_refused(flow):
    assert flow["wrong_code"] == 400
    assert flow["no_code"] == 400


def test_weak_short_and_non_numeric_pins_are_refused(flow):
    assert flow["weak_pin"] == [400, 400]
    assert flow["short_pin"] == 400
    assert flow["letters_pin"] == 400


def test_mismatched_pins_and_missing_details_are_refused(flow):
    assert flow["mismatch"] == 400
    assert flow["bad_province"] == 400
    assert flow["no_shop_name"] == 400
    assert flow["no_owner_name"] == 400


def test_refusals_change_nothing_and_keep_what_was_typed(flow):
    assert flow["still_pending_after_failures"] is True
    assert flow["failed_form_keeps_answers"] is True


# ═══ success ══════════════════════════════════════════════════════════

def test_success_signs_the_owner_in_and_lands_on_help(flow):
    assert flow["success"] == [303, "/help?welcome=1"]
    assert flow["help_after"] == 200
    assert flow["home_after"] == 200


def test_the_code_is_forgiving_about_case_spaces_and_dashes(flow):
    assert flow["success"][0] == 303            # the scenario typed it in lower case with spaces


def test_setup_is_one_shot(flow):
    assert flow["setup_again"] == [303, "/login"]
    assert flow["post_again"] == [303, "/login"]
    assert flow["pending_after"] is False
    assert flow["settings"]["setup_pending"] is None and flow["settings"]["setup_code"] is None, "the code must be deleted"


def test_the_owners_own_pin_works_and_the_old_ones_do_not(flow):
    assert flow["new_pin_after"] == "/"
    assert flow["old_pin_after"] == "/login"
    assert flow["second_pin_after"] == "/login", "a second POST must not be able to change the PIN"


def test_shop_details_are_saved_and_the_owner_is_named(flow):
    s = flow["settings"]
    assert (s["shop_name"], s["province"], s["shop_phone"], s["shop_address"], s["shop_gst"]) == \
        ("TechPro+ Red Deer", "AB", "403-555-0100", "1 Main St", "123456789RT0001")
    assert flow["owner"] == [["Mo", "owner", 6]]


def test_sample_products_are_removed_by_default_and_setup_is_audited(flow):
    assert flow["samples_left"] == 0
    assert len(flow["audit"]) == 1 and "TechPro+ Red Deer" in flow["audit"][0] and "AB" in flow["audit"][0]


def test_sample_products_can_be_kept():
    result, _ = run("keep_samples")
    assert result["success"] == 303
    assert result["before"] > 0 and result["after"] == result["before"]


def test_a_sample_product_that_was_sold_is_never_deleted():
    result, _ = run("sold_sample")
    assert result["success"] == 303
    assert result["left"] == ["SCRN-IP13"], result


# ═══ choosing the code yourself ═══════════════════════════════════════

def test_setup_code_variable_replaces_the_random_one():
    result, stdout = run("env_code", {"SETUP_CODE": "MY-OWN-CODE-77"})
    assert result["pending"] is True
    assert result["code_row"] is None, "no random code should be stored when the variable is set"
    assert result["page_mentions_variable"] is True
    assert result["random_code_refused"] == 400
    assert result["right_code"] == 303
    assert "MYOWNCODE77" not in stdout.replace("-", "").upper(), "the chosen code must never be printed"


def test_a_too_short_setup_code_variable_is_ignored_for_a_random_one():
    result, _ = run("env_code_too_short", {"SETUP_CODE": "abc"})
    assert result["code_row"], "a random code is used instead"
    assert result["short_refused"] == 400


# ═══ an existing shop is never touched ═══════════════════════════════

def test_an_existing_shop_is_never_pending(owner_client):
    from app.database import SessionLocal
    db = SessionLocal()
    try:
        assert S.refresh(db) is False
        assert db.get(__import__("app.models", fromlist=["Setting"]).Setting, "setup_pending") is None
    finally:
        db.close()
    assert owner_client.get("/setup", follow_redirects=False).headers["location"] == "/login"
    assert owner_client.get("/").status_code == 200


def test_restarting_on_a_database_that_has_staff_does_not_start_setup():
    """Build a database the old way (v50-style: an owner with a real PIN), then start the app on it."""
    def prepare(db_file, env):
        code = (
            "import os, sys\n"
            f"os.environ['DATABASE_URL']='sqlite:///{db_file}'\n"
            "os.environ['SESSION_SECRET']='x'; os.environ['SETTINGS_ENCRYPTION_KEY']='kQqjXGz0Yb3g8h5m1nR7sT2vC4wA6dF9eH0iJ3kL5mN='\n"
            f"sys.path.insert(0, {str(ROOT)!r})\n"
            "import pytest\n"
            "from app.database import engine, Base, SessionLocal\n"
            "import app.models\n"
            "Base.metadata.create_all(engine)\n"
            "from app.models import Staff\n"
            "from app.auth import hash_pin\n"
            "db = SessionLocal(); db.add(Staff(id='s1', name='Existing Owner', role='owner', active=True, pin_hash=hash_pin('1234'))); db.commit()\n"
        )
        subprocess.run([sys.executable, "-c", code], check=True, env=env, capture_output=True, timeout=120)
    result, stdout = run("existing_shop", prepare=prepare)
    assert result["pending"] is False
    assert result["flag"] is None
    assert result["login_page"] == 200
    assert result["setup_redirect"] == "/login"
    assert result["pin_1234_still_works"] == 303
    assert "SETUP CODE" not in stdout


# ═══ the code helpers ═════════════════════════════════════════════════

def test_normalise_code():
    assert S.normalise_code(" ab-cd 12 ") == "ABCD12"
    assert S.normalise_code("") == "" and S.normalise_code(None) == ""


def test_random_codes_are_readable_and_different():
    codes = {S._new_code() for _ in range(50)}
    assert len(codes) == 50
    for code in codes:
        assert len(code) == 14 and not set(code.replace("-", "")) & set("01OI")
