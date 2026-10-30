"""Message clair quand Ollama est injoignable (cas typique : installé sur l'hôte, pas dans Docker)."""
import pytest

from app import llm
from app.config import settings


def test_hint_for_localhost_and_host_install():
    assert "host.docker.internal" in llm.unreachable_hint("http://localhost:11434")
    assert "host.docker.internal" in llm.unreachable_hint("http://127.0.0.1:11434")
    assert "OLLAMA_HOST=0.0.0.0" in llm.unreachable_hint("http://10.10.1.10:11434")


def test_test_button_explains_unreachable_ollama(monkeypatch):
    monkeypatch.setattr(settings, "llm_url", "http://127.0.0.1:9")  # port fermé
    monkeypatch.setattr(settings, "llm_model", "tev1:4b")
    with pytest.raises(RuntimeError, match="Ollama injoignable.*host.docker.internal"):
        llm.test()
