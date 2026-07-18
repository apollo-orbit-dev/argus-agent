"""Vision routing: incoming images map through the `vision` role — inline to a multimodal chat
model, captioned by a separate model, or a note when no vision is available."""
from config import Config
from engine.engine import Engine
from engine.loop import _strip_old_images
from engine.model_presets import ModelPresetStore

DATA_URL = "data:image/jpeg;base64,AAAA"


def _engine(tmp_path):
    cfg = Config(model_base_url="http://vllm/v1", model_name="main", telegram_bot_token="",
                 memory_scope="session")
    e = Engine(cfg)
    e.model_presets_store = ModelPresetStore(str(tmp_path / "mp.json"))   # isolate from repo file
    e._env_path = tmp_path / ".env"
    return e


def test_vision_target_inline_when_chat_vision_capable(tmp_path):
    e = _engine(tmp_path)
    e.model_presets_store.add("a1", "http://vllm/v1", "agents-a1", "vllm", None, capabilities=["chat", "vision"])
    e.model_presets_store.set_role("chat", "a1")
    assert e._vision_target() == ("inline", None)


def test_vision_target_none_when_chat_text_only(tmp_path):
    e = _engine(tmp_path)
    e.model_presets_store.add("ds", "https://openrouter.ai/api/v1", "deepseek", "auto", None, capabilities=["chat"])
    e.model_presets_store.set_role("chat", "ds")
    assert e._vision_target() == ("none", None)


def test_vision_target_caption_with_separate_model(tmp_path):
    e = _engine(tmp_path)
    e.model_presets_store.add("ds", "https://openrouter.ai/api/v1", "deepseek", "auto", None, capabilities=["chat"])
    e.model_presets_store.add("cap", "http://vllm/v1", "captioner", "vllm", None, capabilities=["vision"])
    e.model_presets_store.set_role("chat", "ds")
    e.model_presets_store.set_role("vision", "cap")
    mode, conn = e._vision_target()
    assert mode == "caption" and conn["label"] == "cap"


async def test_build_user_content_inline(tmp_path):
    e = _engine(tmp_path)
    e.model_presets_store.add("a1", "http://vllm/v1", "agents-a1", "vllm", None, capabilities=["chat", "vision"])
    e.model_presets_store.set_role("chat", "a1")
    content = await e._build_user_content("what is this?", [DATA_URL])
    assert isinstance(content, list)
    assert content[0] == {"type": "text", "text": "what is this?"}
    assert content[1]["type"] == "image_url" and content[1]["image_url"]["url"] == DATA_URL


async def test_build_user_content_caption(tmp_path):
    e = _engine(tmp_path)
    e.model_presets_store.add("ds", "https://openrouter.ai/api/v1", "deepseek", "auto", None, capabilities=["chat"])
    e.model_presets_store.add("cap", "http://vllm/v1", "captioner", "vllm", None, capabilities=["vision"])
    e.model_presets_store.set_role("chat", "ds")
    e.model_presets_store.set_role("vision", "cap")

    async def fake_caption(conn, url):
        return "a red bicycle"
    e._caption_image = fake_caption
    content = await e._build_user_content("describe", [DATA_URL])
    assert isinstance(content, str)
    assert "describe" in content and "a red bicycle" in content and "cap" in content


async def test_build_user_content_none_note(tmp_path):
    e = _engine(tmp_path)
    e.model_presets_store.add("ds", "https://openrouter.ai/api/v1", "deepseek", "auto", None, capabilities=["chat"])
    e.model_presets_store.set_role("chat", "ds")
    content = await e._build_user_content("hi", [DATA_URL])
    assert isinstance(content, str) and "no vision-capable model" in content


async def test_build_user_content_no_images(tmp_path):
    e = _engine(tmp_path)
    assert await e._build_user_content("hi", []) is None
    assert await e._build_user_content("hi", None) is None


def test_strip_old_images_keeps_only_latest():
    img = {"type": "image_url", "image_url": {"url": DATA_URL}}
    conv = [
        {"role": "user", "content": [{"type": "text", "text": "old"}, img]},
        {"role": "assistant", "content": "sure"},
        {"role": "user", "content": [{"type": "text", "text": "new"}, img]},
    ]
    out = _strip_old_images(conv)
    assert all(p.get("type") != "image_url" for p in out[0]["content"])          # old image stripped
    assert any("omitted" in p.get("text", "") for p in out[0]["content"])        # placeholder added
    assert any(p.get("type") == "image_url" for p in out[2]["content"])          # latest image kept
