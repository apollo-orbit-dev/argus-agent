"""Server-side model presets + the Telegram /model, /models, /reasoning command helpers."""
from backend.telegram_bot import model_command, models_text, reasoning_command
from engine.model_presets import ModelPresetStore

OPENROUTER = "https://openrouter.ai/api/v1"


# ---- store ----
def test_add_list_and_replace_by_label(tmp_path):
    s = ModelPresetStore(str(tmp_path / "mp.json"))
    s.add("local", "http://vllm/v1", "main", "vllm", None)
    s.add("glm", OPENROUTER, "z-ai/glm-5.2", "auto", 131072)
    assert [p["label"] for p in s.list()] == ["local", "glm"]
    s.add("glm", OPENROUTER, "z-ai/glm-5.2-turbo")          # same label -> replace
    assert len(s.list()) == 2 and s.resolve("glm")["model_name"] == "z-ai/glm-5.2-turbo"


def test_resolve_by_label_name_and_substring(tmp_path):
    s = ModelPresetStore(str(tmp_path / "mp.json"))
    s.add("local", "http://vllm/v1", "main", "vllm")
    s.add("glm", OPENROUTER, "z-ai/glm-5.2")
    assert s.resolve("local")["model_name"] == "main"       # label
    assert s.resolve("z-ai/glm-5.2")["label"] == "glm"      # exact model id
    assert s.resolve("glm-5")["label"] == "glm"             # unique substring
    assert s.resolve("nope") is None


def test_remove_and_persist_across_instances(tmp_path):
    p = str(tmp_path / "mp.json")
    ModelPresetStore(p).add("glm", OPENROUTER, "z-ai/glm-5.2")
    assert ModelPresetStore(p).resolve("glm")["model_name"] == "z-ai/glm-5.2"
    s = ModelPresetStore(p)
    assert s.remove("glm") == 1 and s.list() == []


# ---- fake engine exercising the exact methods the Telegram helpers call ----
class FakeEngine:
    def __init__(self, tmp_path):
        self._store = ModelPresetStore(str(tmp_path / "mp.json"))
        self._store.add("local", "http://vllm/v1", "main", "vllm", None)
        self._cfg = {"model_base_url": "http://vllm/v1", "model_name": "main",
                     "model_provider": "vllm", "model_reasoning": "auto",
                     "model_context_window": None}
        self.saved = 0

    def model_presets(self):
        c = self._cfg
        return {"presets": self._store.list(),
                "active": {"base_url": c["model_base_url"], "model_name": c["model_name"],
                           "provider": c["model_provider"], "reasoning": c["model_reasoning"],
                           "context_window": c["model_context_window"]}}

    def model_preset_add(self, model_name, base_url="", context_window=None, label="", provider="auto"):
        return self._store.add(label or model_name, base_url or OPENROUTER, model_name, provider, context_window)

    def model_preset_remove(self, arg):
        return self._store.remove(arg)

    def model_switch(self, arg, persist=True):
        p = self._store.resolve(arg)
        created = False
        if p is None:
            p = self.model_preset_add(arg)
            created = True
        self._cfg.update({"model_base_url": p["base_url"], "model_name": p["model_name"],
                          "model_provider": p.get("provider", "auto"),
                          "model_context_window": p.get("context_window")})
        if persist:
            self.saved += 1
        return {"switched_to": p, "created": created}

    def get_config(self):
        return dict(self._cfg)

    def patch_config(self, patch):
        self._cfg.update(patch)
        return dict(self._cfg)

    def save_config_to_env(self):
        self.saved += 1


# ---- /model, /models, /reasoning ----
def test_model_show_current(tmp_path):
    out = model_command(FakeEngine(tmp_path), [])
    assert "main" in out and "reasoning: auto" in out


def test_model_switch_new_id_adds_and_switches(tmp_path):
    e = FakeEngine(tmp_path)
    out = model_command(e, ["z-ai/glm-5.2"])              # not a preset yet
    assert "new preset added" in out
    assert e._cfg["model_name"] == "z-ai/glm-5.2"
    assert e._cfg["model_base_url"] == OPENROUTER          # bare id assumed OpenRouter
    assert e.saved == 1                                    # persisted to .env


def test_model_switch_back_to_local(tmp_path):
    e = FakeEngine(tmp_path)
    model_command(e, ["z-ai/glm-5.2"])
    out = model_command(e, ["local"])                      # switch back by label
    assert "Switched to main" in out and e._cfg["model_provider"] == "vllm"


def test_model_rm(tmp_path):
    e = FakeEngine(tmp_path)
    e.model_preset_add("z-ai/glm-5.2")
    assert "Removed 1" in model_command(e, ["rm", "z-ai/glm-5.2"])


def test_models_text_marks_current(tmp_path):
    e = FakeEngine(tmp_path)
    e.model_switch("z-ai/glm-5.2")
    out = models_text(e)
    assert "z-ai/glm-5.2" in out and "← current" in out


def test_reasoning_command(tmp_path):
    e = FakeEngine(tmp_path)
    assert "auto" in reasoning_command(e, "")
    assert "high" in reasoning_command(e, "high") and e._cfg["model_reasoning"] == "high"
    assert "Invalid" in reasoning_command(e, "bogus")


# ---- connections: keys, capabilities, roles ----
def test_add_preserves_key_and_caps_on_update(tmp_path):
    s = ModelPresetStore(str(tmp_path / "mp.json"))
    s.add("or", OPENROUTER, "deepseek", api_key="sk-or-1", capabilities=["chat"])
    s.add("or", OPENROUTER, "deepseek", context_window=131072)   # no key/caps passed → preserved
    got = s.resolve("or")
    assert got["api_key"] == "sk-or-1" and got["capabilities"] == ["chat"]
    assert got["context_window"] == 131072


def test_store_roles_set_get_and_clear_on_remove(tmp_path):
    s = ModelPresetStore(str(tmp_path / "mp.json"))
    s.add("a", "http://x/v1", "m-a")
    s.set_role("chat", "a")
    assert s.get_role("chat") == "a" and s.roles() == {"chat": "a"}
    s.remove("a")
    assert s.get_role("chat") is None                            # dangling role dropped


def test_store_migrates_legacy_bare_list(tmp_path):
    import json
    p = str(tmp_path / "mp.json")
    json.dump([{"label": "main", "base_url": "http://x/v1", "model_name": "main"}], open(p, "w"))
    s = ModelPresetStore(p)
    assert [c["label"] for c in s.list()] == ["main"]           # legacy list → connections
    assert s.roles() == {}


# ---- engine role wiring (chat switch sets chat role; embedding role rebuilds the client) ----
def _real_engine(tmp_path):
    from config import Config
    from engine.engine import Engine
    cfg = Config(model_base_url="http://vllm/v1", model_name="main", telegram_bot_token="",
                 memory_scope="session", embedding_base_url="http://vllm/v1", embedding_model="embed")
    e = Engine(cfg)
    e.model_presets_store = ModelPresetStore(str(tmp_path / "mp.json"))   # isolate from repo file
    e._env_path = tmp_path / ".env"                                       # don't touch real .env
    return e


def test_model_roles_exposes_capabilities(tmp_path):
    r = _real_engine(tmp_path).model_roles()
    for cap in ("chat", "embedding", "vision", "tts", "stt", "image_gen", "video_gen"):
        assert cap in r["capabilities"]


def test_chat_switch_sets_chat_role(tmp_path):
    e = _real_engine(tmp_path)
    res = e.model_switch("z-ai/glm-5.2")                          # unknown → added + chat role
    assert res["switched_to"]["model_name"] == "z-ai/glm-5.2"
    assert e.config.model_name == "z-ai/glm-5.2"
    assert e.model_presets_store.get_role("chat") == "z-ai/glm-5.2"


def test_set_embedding_role_projects_and_rebuilds_live(tmp_path):
    e = _real_engine(tmp_path)
    e.model_presets_store.add("or-embed", OPENROUTER, "text-embedding-3", "auto", None,
                              api_key="sk-or-xyz", capabilities=["embedding"])
    before = e.memory.embedder
    res = e.set_role("embedding", "or-embed")
    assert res["connection"]["model_name"] == "text-embedding-3"
    assert e.config.embedding_base_url == OPENROUTER
    assert e.config.embedding_model == "text-embedding-3"
    assert e.config.embedding_api_key == "sk-or-xyz"
    assert e.memory.embedder is not before                       # rebuilt in place
    assert e.memory.embedder.api_key == "sk-or-xyz"
    assert e.knowledge.embedder is e.memory.embedder             # both point at the new client


def test_telegram_roles_and_role_commands(tmp_path):
    from backend.telegram_bot import roles_text, role_command
    e = _real_engine(tmp_path)
    e.model_presets_store.add("embed", "http://vllm/v1", "embed", "vllm", None, capabilities=["embedding"])
    out = roles_text(e)
    assert "chat" in out and "embedding" in out and "reserved" in out      # reserved caps shown
    r = role_command(e, ["embedding", "embed"])
    assert "embedding → embed" in r and "re-embed" in r                    # warns on embedding
    assert e.model_presets_store.get_role("embedding") == "embed"
    assert "Unknown capability" in role_command(e, ["bogus", "x"])
    assert "unset" in role_command(e, ["vision", "none"])


def test_add_rejects_model_id_pasted_as_key(tmp_path):
    s = ModelPresetStore(str(tmp_path / "mp.json"))
    s.add("ds", OPENROUTER, "deepseek/deepseek-v4-flash", api_key="deepseek/deepseek-v4-flash")
    assert (s.resolve("ds").get("api_key") or "") == ""      # key == model id → rejected
    s.add("x", OPENROUTER, "some/model", api_key="has/a/slash")
    assert (s.resolve("x").get("api_key") or "") == ""       # contains '/' → rejected
    s.add("ok", OPENROUTER, "a/b", api_key="sk-or-v1-abc")
    assert s.resolve("ok")["api_key"] == "sk-or-v1-abc"      # a real key is kept


def test_connection_key_inherits_per_provider(tmp_path):
    e = _real_engine(tmp_path)
    e.model_presets_store.add("or1", OPENROUTER, "model-a", "auto", None, api_key="sk-or-v1-REAL")
    e.model_presets_store.add("or2", OPENROUTER, "model-b", "auto", None)          # no key
    e.model_presets_store.add("local", "http://vllm/v1", "main", "vllm", None)     # no key
    assert e._connection_key({"base_url": OPENROUTER, "api_key": "sk-own"}) == "sk-own"       # own wins
    assert e._connection_key(e.model_presets_store.resolve("or2")) == "sk-or-v1-REAL"         # inherits
    assert e._connection_key(e.model_presets_store.resolve("local")) == "dummy"               # vLLM


def test_aux_model_client_uses_utility_role(tmp_path):
    e = _real_engine(tmp_path)
    e.model_presets_store.add("cheap", "http://cheap/v1", "small-model", "vllm", None)
    assert e._aux_model_client().model == e._model_client().model     # no utility role -> chat model
    e.model_presets_store.set_role("utility", "cheap")
    aux = e._aux_model_client()
    assert aux.model == "small-model" and aux.base_url == "http://cheap/v1"


def test_utility_and_new_roles_in_capabilities(tmp_path):
    r = _real_engine(tmp_path).model_roles()
    for cap in ("utility", "reasoning", "coding"):
        assert cap in r["capabilities"]
