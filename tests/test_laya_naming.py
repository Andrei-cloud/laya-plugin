"""T1.6 / D6 tests for the ONE alias resolver (server/naming.py).

Covers the D6 rows that live in the resolver: canonical-wins, alias chains,
empty-is-unset, diverge-warned-once, generic passthrough, model echo,
layered config dirs + per-tier merge, shared ladder state, doctor report
secrecy (API key/TOKEN never printed), key resolution chain, keystore
helper bounded + fail-open.
"""
import json
import logging
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from server import naming  # noqa: E402

# every env spelling this module understands — cleared per test for isolation
_ALL_ENV = [f"{ns}{n}" for n in naming.ALIAS_TABLE for ns in ("LAYA_", "JEV_", "TYPESAFE_")] + [
    "DASHBOARD_TOKEN", "LAYA_FOO", "JEV_FOO", "TYPESAFE_FOO",
    "HERMES_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "HOME",
]


@pytest.fixture(autouse=True)
def isolated_env(tmp_path, monkeypatch):
    """Fresh HOME + XDG dirs under tmp, all alias envs unset, resolver
    memoization reset — each test sees a hermetically empty world."""
    home = tmp_path / "home"
    home.mkdir()
    for var in _ALL_ENV:
        monkeypatch.delenv(var, raising=False)
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.setenv("XDG_CONFIG_HOME", str(home / ".config"))
    monkeypatch.setenv("XDG_CACHE_HOME", str(home / ".cache"))
    naming._reset_state()
    yield tmp_path
    naming._reset_state()


# ------------------------------------------------------------ env_alias ----

def test_canonical_laya_env_wins_over_alias():
    naming.os.environ["LAYA_MIN_CONFIDENCE"] = "0.65"
    naming.os.environ["JEV_MIN_CONFIDENCE"] = "0.60"
    assert naming.env_alias("MIN_CONFIDENCE") == "0.65"  # canonical wins
    assert naming.divergences  # and the divergence is recorded


def test_alias_used_when_canonical_absent():
    naming.os.environ["JEV_MIN_CONFIDENCE"] = "0.60"
    assert naming.env_alias("MIN_CONFIDENCE") == "0.60"
    assert not naming.divergences


def test_typesafe_legacy_spelling_keeps_working():
    naming.os.environ["TYPESAFE_API_KEY"] = "sk-legacy"
    assert naming.env_alias("API_KEY") == "sk-legacy"


def test_empty_string_counts_as_unset_and_falls_through():
    naming.os.environ["LAYA_MEMO"] = ""          # empty == unset
    naming.os.environ["JEV_MEMO"] = "on"
    assert naming.env_alias("MEMO") == "on"
    assert naming.env_alias("MEMO", default="shadow") == "on"
    naming.os.environ["JEV_MEMO"] = ""
    assert naming.env_alias("MEMO", default="shadow") == "shadow"  # default


def test_divergence_recorded_and_warned_once_never_raises():
    naming.os.environ["LAYA_MEMO"] = "on"
    naming.os.environ["JEV_MEMO"] = "off"
    for _ in range(5):                       # repeated reads never raise
        assert naming.env_alias("MEMO") == "on"
    assert len(naming.divergences) == 1          # ONCE per name
    rec = naming.divergences[0]
    assert rec["name"] == "MEMO" and rec["source"] == "LAYA_MEMO"
    assert rec["diverged_sources"] == ["JEV_MEMO"]
    # the record must be safe to print even for a secret key
    naming.os.environ["LAYA_API_KEY"] = "top-secret-a"
    naming.os.environ["JEV_API_KEY"] = "top-secret-b"
    naming.env_alias("API_KEY")
    assert "top-secret" not in json.dumps(naming.divergences)


def caplog_at():
    """Fail if the resolver logs at WARNING more than expected is not the
    point here — just ensure no raise; kept as a readable no-op context."""
    class _N:
        def __enter__(self): return None
        def __exit__(self, *a): return False
    return _N()


def test_warning_emitted_once_via_named_logger():
    calls = []

    class _H(logging.Handler):
        def emit(self, record):
            calls.append(record.getMessage())

    logger = logging.getLogger("laya.naming")
    h = _H()
    logger.addHandler(h)
    try:
        naming.os.environ["LAYA_PLAN_MODEL"] = "big"
        naming.os.environ["JEV_PLAN_MODEL"] = "small"
        for _ in range(3):
            naming.env_alias("PLAN_MODEL")
    finally:
        logger.removeHandler(h)
    assert len(calls) == 1
    assert "LAYA_PLAN_MODEL wins" in calls[0]


def test_generic_passthrough_for_unlisted_pair():
    naming.os.environ["LAYA_FOO"] = "l"
    naming.os.environ["JEV_FOO"] = "j"
    assert naming.env_alias("FOO") == "l"
    del naming.os.environ["LAYA_FOO"]
    assert naming.env_alias("FOO") == "j"


def test_d6_table_names_all_resolve_first_hit():
    for name, spellings in naming.ALIAS_TABLE.items():
        naming._reset_state()
        naming.os.environ[spellings[-1]] = f"v-{spellings[-1]}"
        assert naming.env_alias(name) == f"v-{spellings[-1]}"


def test_token_aliases_dashboard_token():
    naming.os.environ["DASHBOARD_TOKEN"] = "dash"
    assert naming.env_alias("TOKEN") == "dash"


# --------------------------------------------------------------- model id ----

def test_model_id_echoed_verbatim():
    for m in ["jev-latest", "~typesafe/jev-latest", "laya-r15-chain3",
              "anything-a-client-invented"]:
        assert naming.model_id(m) == m


def test_model_id_default_from_env_alias_then_laya_r15():
    assert naming.model_id(None) == "laya-r15"
    assert naming.model_id("") == "laya-r15"
    naming.os.environ["TYPESAFE_MODEL"] = "jev-latest"   # legacy alias
    assert naming.model_id(None) == "jev-latest"


# ---------------------------------------------------------- config chain ----

def test_config_dir_chain_laya_first_jev_fallback_respects_xdg():
    paths = naming.config_dir_chain("routing.json")
    cfg = Path(naming.os.environ["XDG_CONFIG_HOME"])
    assert paths == [cfg / "laya" / "routing.json", cfg / "jev" / "routing.json"]
    # cache kind (memo/) uses XDG_CACHE_HOME for both namespaces
    mpaths = naming.config_dir_chain("memo", kind="cache")
    cch = Path(naming.os.environ["XDG_CACHE_HOME"])
    assert mpaths == [cch / "laya" / "memo", cch / "jev" / "memo"]


def test_xdg_config_home_default_is_home_dot_config(monkeypatch):
    monkeypatch.delenv("XDG_CONFIG_HOME")
    paths = naming.config_dir_chain("routing.json")
    assert paths[0] == Path(naming.os.environ["HOME"]) / ".config" / "laya" / "routing.json"


def test_routing_config_chain_profile_layer_before_xdg():
    # ONE shared chain (D6): hermes_root profile layer FIRST, then the
    # XDG (pre-profile Jev-era) layer; laya wins over jev within each.
    monkey_home = Path(naming.os.environ["HOME"])
    naming.os.environ["HERMES_HOME"] = str(monkey_home / "prof")
    chain = naming.routing_config_chain()
    assert chain == [
        monkey_home / "prof" / "laya" / "routing.json",
        monkey_home / "prof" / "jev" / "routing.json",
        monkey_home / ".config" / "laya" / "routing.json",
        monkey_home / ".config" / "jev" / "routing.json",
    ]


def test_routing_config_chain_hermes_root_defaults_to_home(monkeypatch):
    naming.os.environ.pop("HERMES_HOME", None)
    home = Path(naming.os.environ["HOME"])
    chain = naming.routing_config_chain()
    # with no HERMES_HOME, hermes_root() == ~/.hermes (distinct from XDG)
    assert chain[0] == home / ".hermes" / "laya" / "routing.json"
    assert len(chain) == 4


def test_routing_config_reader_profile_file_wins_over_xdg(tmp_path, monkeypatch):
    """Behavioural: a profile routing.json beats the XDG one; with only
    the XDG file present it still answers (the plugin's once-broken case:
    profile had a config, the plugin read only XDG -> silent 'no routing
    config')."""
    home = Path(naming.os.environ["HOME"])
    prof = home / "prof"
    (prof / "laya").mkdir(parents=True)
    (home / ".config" / "laya").mkdir(parents=True)
    (home / ".config" / "laya" / "routing.json").write_text(
        json.dumps({"tiers": {"simple": {"pool": ["xdg-model"]}}}))
    naming.os.environ["HERMES_HOME"] = str(prof)

    def read():
        for f in naming.routing_config_chain():
            if f.is_file():
                return json.loads(f.read_text())
        return {}

    assert read()["tiers"]["simple"]["pool"] == ["xdg-model"]   # XDG alone
    (prof / "laya" / "routing.json").write_text(
        json.dumps({"tiers": {"simple": {"pool": ["profile-model"]}}}))
    assert read()["tiers"]["simple"]["pool"] == ["profile-model"]  # profile wins


def test_routing_config_chain_profile_layer_before_xdg(monkeypatch):
    # ONE shared chain (D6): hermes_root profile layer FIRST, then the
    # XDG (pre-profile Jev-era) layer; laya wins over jev within each.
    monkey_home = Path(naming.os.environ["HOME"])
    monkeypatch.setenv("HERMES_HOME", str(monkey_home / "prof"))
    chain = naming.routing_config_chain()
    assert chain == [
        monkey_home / "prof" / "laya" / "routing.json",
        monkey_home / "prof" / "jev" / "routing.json",
        monkey_home / ".config" / "laya" / "routing.json",
        monkey_home / ".config" / "jev" / "routing.json",
    ]


def test_routing_config_chain_hermes_root_defaults_to_home(monkeypatch):
    monkeypatch.delenv("HERMES_HOME", raising=False)
    home = Path(naming.os.environ["HOME"])
    chain = naming.routing_config_chain()
    # with no HERMES_HOME, hermes_root() == ~/.hermes (distinct from XDG)
    assert chain[0] == home / ".hermes" / "laya" / "routing.json"
    assert len(chain) == 4


def test_routing_config_reader_profile_file_wins_over_xdg(tmp_path, monkeypatch):
    """Behavioural: a profile routing.json beats the XDG one; with only
    the XDG file present it still answers (the plugin's once-broken case:
    profile had a config, the plugin read only XDG -> silent 'no routing
    config')."""
    home = Path(naming.os.environ["HOME"])
    prof = home / "prof"
    (prof / "laya").mkdir(parents=True)
    (home / ".config" / "laya").mkdir(parents=True)
    (home / ".config" / "laya" / "routing.json").write_text(
        json.dumps({"tiers": {"simple": {"pool": ["xdg-model"]}}}))
    monkeypatch.setenv("HERMES_HOME", str(prof))

    def read():
        for f in naming.routing_config_chain():
            if f.is_file():
                return json.loads(f.read_text())
        return {}

    assert read()["tiers"]["simple"]["pool"] == ["xdg-model"]   # XDG alone
    (prof / "laya" / "routing.json").write_text(
        json.dumps({"tiers": {"simple": {"pool": ["profile-model"]}}}))
    assert read()["tiers"]["simple"]["pool"] == ["profile-model"]  # profile wins


def test_merge_json_layers_laya_wins_fallback_fills():
    chain = naming.config_dir_chain("routing.json")
    chain[1].parent.mkdir(parents=True)           # jev (fallback) layer
    chain[1].write_text(json.dumps(
        {"ask_chars": 2500, "thresholds": {"min_confidence": 0.6, "keep": 1}}))
    chain[0].parent.mkdir(parents=True)           # laya (canonical) layer
    chain[0].write_text(json.dumps(
        {"ask_chars": 3000, "thresholds": {"min_confidence": 0.65}}))
    merged = naming.merge_json_layers("routing.json")
    assert merged["ask_chars"] == 3000                          # laya wins
    assert merged["thresholds"]["min_confidence"] == 0.65       # laya wins
    assert merged["thresholds"]["keep"] == 1                    # jev fills
    names = [d["name"] for d in naming.divergences]
    assert "config:ask_chars" in names and "config:min_confidence" in names


def test_merge_json_layers_missing_or_broken_files():
    assert naming.merge_json_layers("routing.json") == {}
    chain = naming.config_dir_chain("routing.json")
    chain[0].parent.mkdir(parents=True)
    chain[0].write_text("{not json")
    chain[1].parent.mkdir(parents=True)
    chain[1].write_text(json.dumps({"a": 1}))
    assert naming.merge_json_layers("routing.json") == {"a": 1}  # broken layer skipped


# --------------------------------------------------------- ladder state ----

def test_ladder_state_default_under_hermes_root_laya_dir():
    assert naming.ladder_state_path() == \
        Path(naming.os.environ["HOME"]) / ".hermes" / "laya" / "ladder.json"


def test_ladder_state_respects_hermes_home(monkeypatch):
    monkeypatch.setenv("HERMES_HOME", "/opt/hermes")
    assert naming.ladder_state_path() == Path("/opt/hermes/laya/ladder.json")


def test_ladder_state_env_alias_laya_over_jev():
    naming.os.environ["JEV_LADDER_STATE"] = "/tmp/jev-ladder.json"
    assert naming.ladder_state_path() == Path("/tmp/jev-ladder.json")
    naming.os.environ["LAYA_LADDER_STATE"] = "/tmp/laya-ladder.json"
    assert naming.ladder_state_path() == Path("/tmp/laya-ladder.json")


# ------------------------------------------------------------ key chain ----

def test_resolve_api_key_env_precedence_order():
    naming.os.environ["TYPESAFE_API_KEY"] = "k-typesafe"
    naming.os.environ["JEV_API_KEY"] = "k-jev"
    assert naming.resolve_api_key() == "k-jev"        # JEV beats TYPESAFE
    naming.os.environ["LAYA_API_KEY"] = "k-laya"
    assert naming.resolve_api_key() == "k-laya"       # LAYA beats all


def test_resolve_api_key_credentials_files_fallback():
    chain = naming.config_dir_chain("credentials")
    chain[1].parent.mkdir(parents=True)               # ~/.config/jev
    chain[1].write_text("LAYA_API_KEY=k-jev-file\n")
    assert naming.resolve_api_key() == "k-jev-file"
    chain[0].parent.mkdir(parents=True)               # ~/.config/laya wins
    chain[0].write_text(json.dumps({"LAYA_API_KEY": "k-laya-file"}))
    assert naming.resolve_api_key() == "k-laya-file"


def test_resolve_api_key_none_when_nothing_configured():
    assert naming.resolve_api_key() is None


def test_keychain_secret_bounded_and_none_on_missing_item():
    # a service that does not exist must yield None quickly, never raise;
    # the helper's subprocess timeout is 5 s (bounded by construction).
    import time
    t0 = time.monotonic()
    assert naming.keychain_secret("No Such Service Laya Test", "NO_SUCH_ACCT") is None
    assert time.monotonic() - t0 < 10                 # bounded, not hanging


# ---------------------------------------------------------- doctor report ----

def test_doctor_report_never_leaks_secret_values():
    naming.os.environ["LAYA_API_KEY"] = "sk-SUPER-SECRET-VALUE"
    naming.os.environ["LAYA_TOKEN"] = "tok-SECRET-TOKEN"
    naming.os.environ["LAYA_MEMO"] = "on"
    rep = naming.doctor_alias_report()
    blob = json.dumps(rep)
    assert "sk-SUPER-SECRET-VALUE" not in blob
    assert "tok-SECRET-TOKEN" not in blob
    api = rep["resolved"]["API_KEY"]
    assert api["present"] is True and api["source"] == "LAYA_API_KEY"
    assert api["length"] == len("sk-SUPER-SECRET-VALUE")   # describe(): length only
    assert "provider" in api
    tok = rep["resolved"]["TOKEN"]
    assert tok == {"present": True, "source": "LAYA_TOKEN"}
    # non-secret keys report the value normally
    assert rep["resolved"]["MEMO"]["value"] == "on"


def test_doctor_report_lists_divergences():
    naming.os.environ["LAYA_MEMO"] = "on"
    naming.os.environ["JEV_MEMO"] = "off"
    naming.env_alias("MEMO")
    rep = naming.doctor_alias_report()
    assert any(d["name"] == "MEMO" for d in rep["divergences"])
    assert "values" not in json.dumps(rep["divergences"])


def test_describe_api_key_absent_shape():
    d = naming.describe_api_key()
    assert d == {"present": False, "provider": None, "source": None, "length": 0}


def test_log_dir_honours_hermes_home(tmp_path, monkeypatch):
    """REGRESSION (handoff review): laya_decisions.log_dir() used
    env_alias("HERMES_HOME") — HERMES_HOME is host-addressing, not an
    alias key, so the resolver silently returned None and a host
    multiplexed via HERMES_HOME logged into the REAL home's
    ~/.hermes/logs — the wrong profile's audit trail. log_dir must use
    naming.hermes_root() (direct env read)."""
    import importlib
    monkeypatch.delenv("LAYA_LOG_DIR", raising=False)
    monkeypatch.delenv("JEV_LOG_DIR", raising=False)
    alt = tmp_path / "alt-home"
    alt.mkdir()
    monkeypatch.setenv("HERMES_HOME", str(alt))
    from server import laya_decisions
    importlib.reload(laya_decisions)
    try:
        assert laya_decisions.log_dir() == alt / "logs"
    finally:
        importlib.reload(laya_decisions)   # env restored by monkeypatch next
